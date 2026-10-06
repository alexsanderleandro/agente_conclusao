"""
Agente de avaliação de atendimentos.
A cada N minutos (config.ini): busca atendimentos candidatos no SQL Server,
lê as interações, pede ao Claude se dá pra concluir e grava a SUGESTÃO num SQLite local.
Não escreve nada no banco do ERP.

Uso:
    python agente.py            # loop contínuo (rodar via NSSM)
    python agente.py --uma-vez  # roda um ciclo e sai (teste)
"""
import argparse
import hashlib
import configparser
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pyodbc

import cripto
import llm
from audios import (Transcritor, audios_na_pasta, caminhos_no_texto, mapear_unidade,
                    pasta_cliente_pelos_links)
from fichas import LocalizadorFichas, ler_ficha
from privacidade import LeitorOCR, Pseudonimizador, extrair_pngs_rtf, mascarar_pii

try:
    from striprtf.striprtf import rtf_to_text
except ImportError:  # pip install striprtf
    rtf_to_text = None

BASE = Path(__file__).parent
CFG_PATH = BASE / "config.ini"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(BASE / "agente.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("agente")


# ---------- config (relida a cada ciclo: mudou o ini, vale no próximo ciclo) ----------
def ler_cfg():
    c = configparser.ConfigParser(interpolation=None)
    c.BOOLEAN_STATES = {**c.BOOLEAN_STATES, "sim": True, "s": True, "nao": False, "não": False, "n": False}
    if not c.read(CFG_PATH, encoding="utf-8"):
        raise FileNotFoundError(f"config.ini não encontrado em {CFG_PATH}")
    return c


# ---------- SQLite local (resultados) ----------
def init_sqlite(path):
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE IF NOT EXISTS avaliacoes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            atendimento_id TEXT, cliente TEXT, assunto TEXT, analista TEXT,
            qtd_interacoes INTEGER, pode_concluir INTEGER, confianca REAL,
            justificativa TEXT, pendencias TEXT, resumo TEXT,
            status TEXT DEFAULT 'pendente',
            avaliado_em TEXT, modelo TEXT)"""
    )
    try:  # banco criado antes do campo resumo
        con.execute("ALTER TABLE avaliacoes ADD COLUMN resumo TEXT")
    except sqlite3.OperationalError:
        pass
    for col in ("cqs_cod TEXT", "cqs_nome TEXT", "cqs_descricao TEXT", "cqs_pontos TEXT",
                "cqs_ok INTEGER DEFAULT 0", "motivo TEXT", "decidido_em TEXT",
                "fichas TEXT", "qtd_fichas INTEGER DEFAULT 0", "audios TEXT", "qtd_audios INTEGER DEFAULT 0",
                "na_fila INTEGER DEFAULT 1", "cqs_versao TEXT", "cqs_secundarios TEXT",
                "cqs_sec_ok INTEGER DEFAULT 0"):
        try:  # bancos criados antes do recurso CQS
            con.execute(f"ALTER TABLE avaliacoes ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    con.execute("CREATE INDEX IF NOT EXISTS ix_at ON avaliacoes(atendimento_id)")
    con.execute(
        """CREATE TABLE IF NOT EXISTS execucoes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inicio TEXT, fim TEXT, candidatos INTEGER, avaliados INTEGER,
            erros INTEGER, tokens_in INTEGER, tokens_out INTEGER)"""
    )
    con.commit()
    return con


# ---------- SQL Server (somente leitura) ----------
def consultar(conn, sql, params=()):
    if not re.match(r"^\s*(select|with)\b", sql, re.I):
        raise ValueError("Só consultas SELECT/WITH são permitidas no config.ini")
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def limpar(txt):
    if not txt:
        return ""
    if isinstance(txt, (bytes, bytearray)):
        txt = txt.decode("cp1252", errors="ignore")
    txt = str(txt)
    if txt.lstrip().startswith("{\\rtf"):         # RichText do VB6 -> texto puro
        if rtf_to_text:
            try:
                txt = rtf_to_text(txt, errors="ignore")
            except Exception:
                txt = re.sub(r"\\'[0-9a-f]{2}|\\[a-z]+-?\d* ?|[{}]", " ", txt)
        else:
            txt = re.sub(r"\\'[0-9a-f]{2}|\\[a-z]+-?\d* ?|[{}]", " ", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)            # tira HTML, se houver
    return re.sub(r"\s+", " ", txt).strip()


class Protecao:
    """Aplica as camadas de LGPD em tudo que vai para a IA (config.ini, seção [privacidade])."""

    def __init__(self, cfg, usuarios=(), ocr=None):
        self.mascarar = cfg.getboolean("privacidade", "mascarar_dados", fallback=True)
        self.pseudo_on = cfg.getboolean("privacidade", "pseudonimizar_nomes", fallback=True)
        self.max_imgs = cfg.getint("privacidade", "max_imagens_por_atendimento", fallback=5)
        self.usuarios = list(usuarios)
        self.ocr = ocr
        self.imagens_lidas = 0
        self.pseudo = Pseudonimizador(self.usuarios if self.pseudo_on else ())
        self.transcritor = None          # áudios (definido no ciclo, se [audios] transcrever = sim)
        self.mapa_unidade = {}
        self.max_audios = cfg.getint("audios", "max_audios_por_atendimento", fallback=5)
        self.audios_lidos = []

    def para_atendimento(self, cliente):
        p = Protecao.__new__(Protecao)
        p.__dict__.update(self.__dict__)
        p.imagens_lidas = 0
        p.audios_lidos = []
        p.pseudo = Pseudonimizador(self.usuarios, cliente) if self.pseudo_on else Pseudonimizador()
        return p

    def proteger(self, txt):
        txt = "" if txt is None else str(txt)
        if self.mascarar:
            txt = mascarar_pii(txt)
        return self.pseudo.ocultar(txt)

    def texto_imagens(self, texto_bruto):
        if not (self.ocr and self.ocr.ok):
            return ""
        partes = []
        for png in extrair_pngs_rtf(texto_bruto):
            if self.imagens_lidas >= self.max_imgs:
                break
            lido = self.ocr.ler(png)
            if lido:
                self.imagens_lidas += 1
                partes.append(f" [Imagem {self.imagens_lidas} (texto lido por OCR): {lido}]")
        return "".join(partes)


    def transcrever_audio(self, caminho):
        """Transcreve 1 áudio (se ainda não lido e dentro do limite). Retorna (nº, texto) ou None."""
        if not (self.transcritor and self.transcritor.ok) or len(self.audios_lidos) >= self.max_audios:
            return None
        real = mapear_unidade(caminho, self.mapa_unidade)
        k = os.path.normcase(os.path.abspath(real))
        if k in (os.path.normcase(os.path.abspath(a)) for a in self.audios_lidos):
            return None
        texto = self.transcritor.transcrever(real)
        self.audios_lidos.append(real)
        return len(self.audios_lidos), (texto or "(não foi possível transcrever)")

    def texto_audios(self, texto_bruto, texto_limpo):
        partes = []
        for c in caminhos_no_texto(texto_bruto, texto_limpo):
            r = self.transcrever_audio(c)
            if r:
                partes.append(f" [Áudio {r[0]} (transcrição automática): {r[1]}]")
        return "".join(partes)


_RX_ATUALIZACAO = re.compile(r"(?i)atualiza[cç][aã]o\s*:\s*(?P<tipo>[^\s:]+)")
_RX_MODALIDADE = re.compile(r"(?i)\b(remot[oa]|in\s*loco|presencial|no\s+cliente|na\s+empresa|local)\b")


def extrair_atualizacoes(rows):
    """Registros de atualização no texto das iterações, ex.:
        Atualização: Completa / Data: 02/10/2026 / Analista: Alexandre / Remoto
    Retorna [{id, tipo, data, analista, modalidade}] com o nome REAL do analista (fica só local)."""
    achados = []
    for r in rows:
        txt = limpar(r.get("texto"))
        inicios = [m.start() for m in _RX_ATUALIZACAO.finditer(txt)]
        for i, ini in enumerate(inicios):
            bloco = txt[ini: inicios[i + 1] if i + 1 < len(inicios) else ini + 300]
            m = _RX_ATUALIZACAO.match(bloco)
            data = re.search(r"(?i)data\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})", bloco)
            mod = _RX_MODALIDADE.search(bloco)
            ana = re.search(r"(?i)analista\s*:\s*(.+?)(?=\s+(?:remot\w*|in\s*loco|presencial|no\s+cliente|"
                            r"na\s+empresa|local|data\s*:|atualiza\w*\s*:)|[.;,\n]|$)", bloco)
            achados.append({
                "id": f"U{len(achados) + 1}",
                "tipo": m.group("tipo").strip(".,;") if m else "",
                "data": data.group(1) if data else "",
                "analista": ana.group(1).strip() if ana else "",
                "modalidade": mod.group(1).lower().replace("  ", " ") if mod else "",
            })
    return achados


def montar_historico(rows, max_chars, prot=None):
    linhas = []
    for r in rows:
        d = r.get("data")
        d = d.strftime("%d/%m/%Y %H:%M") if hasattr(d, "strftime") else str(d)
        autor = r.get("autor", "")
        texto = limpar(r.get("texto"))
        if prot:
            texto += prot.texto_imagens(r.get("texto"))
            texto += prot.texto_audios(r.get("texto"), texto)
            autor, texto = prot.proteger(autor), prot.proteger(texto)
        linhas.append(f"[{d}] tipo {r.get('tipo', '')} - {autor}: {texto}")
    txt = "\n".join(linhas)
    if len(txt) > max_chars:  # mantém começo (contexto) e fim (situação atual)
        ini = int(max_chars * 0.2)
        txt = txt[:ini] + "\n[... trecho omitido ...]\n" + txt[-(max_chars - ini):]
    return txt


# ---------- Claude ----------
SYSTEM = """Você avalia atendimentos de suporte de um ERP para varejo/distribuição.
Leia o histórico e decida se o atendimento já pode ser CONCLUÍDO.
O atendimento chegou até você porque sua última iteração é do tipo "enviado para revisão"
(código 26): alguém pediu para revisar se ele pode ser concluído. Essa última iteração
é o pedido de revisão, não uma resposta ao cliente. Avalie o que veio antes dela.

Critérios para concluir (configuráveis):
{criterio}

Seja conservador: na dúvida, pode_concluir=false.

REGRA PRINCIPAL - PENDÊNCIAS EM QUALQUER FONTE IMPEDEM A CONCLUSÃO:
analise TODAS as fontes do atendimento, não só o texto das iterações:
  1) texto das iterações;
  2) imagens/prints (trechos "[Imagem N ...]");
  3) áudios de WhatsApp (trechos "[Áudio N ...]" e a seção "ÁUDIOS DO ATENDIMENTO");
  4) fichas de visita (seção "FICHAS DE VISITA").
Se em QUALQUER uma delas houver assunto pendente que o histórico não mostre como resolvido depois,
pode_concluir=false. Pendência pode ser explícita (alguém se comprometeu a fazer algo: "vou enviar",
"retorno amanhã", "aguardando o cliente...") ou implícita (o cliente pediu algo e não há registro de
que foi atendido; um erro mostrado num print sem solução registrada).
Liste CADA pendência em "pendencias", em linhas separadas, começando pela fonte:
"Texto: ...", "Imagem: ...", "Áudio: ...", "Ficha de visita: ...".
Se não houver nenhuma pendência, "pendencias" deve ser vazio.
Responda SOMENTE com JSON, sem texto fora dele:
{{"pode_concluir": true|false, "confianca": 0.0-1.0, "justificativa": "até 3 frases", "pendencias": "o que falta, ou vazio", "resumo": "...", "cqs": codtiporegistro ou null, "cqs_atualizacoes": [{{"id": "U1", "cqs": codtiporegistro}}]}}

O campo "resumo" é o texto que o analista vai registrar no atendimento, em português, objetivo, até 6 frases:
- Se pode concluir: o que o cliente solicitou, o que foi feito e o resultado/confirmação.
- Se não pode: o que foi entendido do histórico até aqui e o motivo de não concluir (o que ainda falta).
Baseie-se só no que está no histórico; não invente fatos.

Privacidade: alguns dados foram ocultados e aparecem como marcadores entre colchetes, por exemplo
[CPF], [CNPJ], [SENHA], [EMAIL], [TELEFONE], [CLIENTE], [USUARIO_12]. Não tente adivinhar o conteúdo.
Ao citar essas pessoas ou dados no resumo, use exatamente o mesmo marcador.
Trechos "[Imagem N (texto lido por OCR): ...]" são o texto extraído de prints anexados ao atendimento;
podem conter erros de leitura. Uma mensagem de erro, aviso ou solicitação visível num print, sem
solução registrada depois, é pendência.

Áudios: trechos "[Áudio N (transcrição automática): ...]" e a seção "ÁUDIOS DO ATENDIMENTO" são
áudios de WhatsApp transcritos automaticamente (podem ter erros e não indicam quem falou; deduza pelo
contexto). Trate o conteúdo como mensagens do atendimento: um pedido do cliente feito em áudio e não
atendido depois é pendência.

Fichas de visita: quando houver a seção "FICHAS DE VISITA", ela traz o texto de fichas de visita ao
cliente feitas no período do atendimento (PDF ou foto/escaneado lido por OCR, pode ter erros de leitura).
Procure pendências na ficha, explícitas ou implícitas:
- explícita: algo que alguém se comprometeu a fazer e ainda não foi feito (ex.: "vou montar o layout e
  enviar para aprovação", "retornar para treinamento", "aguardando cliente enviar...");
- implícita: uma solicitação do cliente registrada na ficha sem conclusão definida (ex.: "cliente
  solicitou recurso X", "cliente pediu novo relatório") - se não diz que foi resolvida, é pendência.
Se o histórico mostrar que a pendência já foi resolvida depois da ficha, ela não impede a conclusão.

{cqs_bloco}

CQS SECUNDÁRIO DE ATUALIZAÇÃO: quando houver a seção "REGISTROS DE ATUALIZAÇÃO" (registros do tipo
"Atualização: Completa / Data / Analista / Remoto"), cada registro gera um CQS SECUNDÁRIO para o
analista daquele registro, além do CQS principal do atendimento. Para cada registro (U1, U2...), escolha
na lista de CQS o código que corresponde àquela atualização, de acordo com a modalidade (remota, in loco
etc.) e o tipo (completa, parcial etc.), e devolva em "cqs_atualizacoes". O CQS principal continua sendo
o do atendimento (o que foi feito para o cliente), não o da atualização. Se não houver registros,
devolva "cqs_atualizacoes": []. Se pode_concluir=false, devolva "cqs_atualizacoes": [].

{exemplos_bloco}"""


def codigos_cqs(cfg, opcao):
    """Conjunto de códigos permitidos ([cqs] principal / secundario no config.ini). Vazio = todos."""
    v = cfg.get("cqs", opcao, fallback="").strip()
    if not v and opcao == "secundario":          # sem lista própria: usa a mesma do principal
        v = cfg.get("cqs", "principal", fallback="").strip()
    return {c.strip() for c in v.split(",") if c.strip()} or None


def montar_bloco_cqs(catalogo, principais=None, secundarios=None):
    """Só os tipos que podem ser escolhidos vão para a IA (tipos administrativos/negativos ficam de fora).
    P = pode ser CQS principal do atendimento; S = pode ser CQS secundário de atualização."""
    if not catalogo:
        return 'Campo "cqs": responda sempre null.'
    linhas = []
    for c in catalogo:
        cod = str(c.get("codtiporegistro"))
        uso = ("P" if principais is None or cod in principais else "") + \
              ("S" if secundarios is None or cod in secundarios else "")
        if not uso:
            continue
        desc = limpar(c.get("descricaotiporegistro"))[:1500]
        linhas.append(f"{cod} | uso: {uso} | {limpar(c.get('nometiporegistro'))} | {desc} | pontos: {c.get('pontos')}")
    return (
        'Campo "cqs": SOMENTE se pode_concluir=true, escolha o codtiporegistro da lista abaixo que melhor '
        'classifica este atendimento, com base no histórico. Se nenhum servir, ou se pode_concluir=false, '
        'use null. Use apenas códigos da lista.\n'
        'Para o CQS principal ("cqs") use SÓ códigos com "P" no uso; para os registros de atualização '
        '("cqs_atualizacoes") use SÓ códigos com "S".\n'
        'A DESCRIÇÃO de cada tipo é a regra de classificação: é uma lista de situações típicas daquele nível. '
        'Leia todas antes de escolher e escolha o tipo cuja lista contém o que o analista DE FATO fez no '
        'atendimento; se o atendimento tiver situações de níveis diferentes, use o nível mais alto que de fato '
        'ocorreu (ex.: se o analista orientou o usuário, não é o nível "sem complexidade"). Esta lista é a '
        'versão atual; ignore qualquer classificação diferente que apareça nos exemplos antigos.\n'
        'Lista (codtiporegistro | uso | nome | descrição | pontos):\n' + "\n".join(linhas)
    )


def montar_exemplos(lite, cfg, prot=None, cqs_versao=None):
    """Decisões recentes do analista (aceitou/rejeitou) para a IA calibrar o julgamento."""
    n = cfg.getint("agente", "exemplos_por_tipo", fallback=3)
    if n <= 0:
        return ""
    linhas = []
    for status, rotulo in (("rejeitada", "DISCORDOU"), ("aceita", "CONCORDOU")):
        rows = lite.execute(
            "SELECT assunto, pode_concluir, cqs_cod, cqs_nome, resumo, motivo, cliente, cqs_versao FROM avaliacoes "
            "WHERE status=? ORDER BY COALESCE(decidido_em, avaliado_em) DESC, id DESC LIMIT ?",
            (status, n),
        ).fetchall()
        for assunto, pode, cqs_cod, cqs_nome, resumo, motivo, cliente, versao in rows:
            # CQS escolhido com o catálogo antigo não serve de exemplo para o catálogo novo
            cqs = f" (CQS {cqs_cod} - {cqs_nome})" if cqs_cod and versao == cqs_versao else ""
            if prot:
                p = prot.para_atendimento(cliente)
                assunto, resumo, motivo = p.proteger(assunto), p.proteger(resumo), p.proteger(motivo)
            linhas.append(
                f'- Assunto "{limpar(assunto)[:80]}" | IA respondeu: {"Sim" if pode else "Não"}{cqs} | '
                f'O analista {rotulo}. Motivo: {limpar(motivo) or "(sem motivo informado)"} | '
                f'Resumo da IA: {limpar(resumo)[:300]}'
            )
    if not linhas:
        return ""
    return (
        "Exemplos reais de decisões do analista humano sobre avaliações suas anteriores. Use-os para "
        "calibrar seu julgamento: onde ele DISCORDOU, evite repetir o mesmo erro; onde CONCORDOU, "
        "mantenha o critério.\n" + "\n".join(linhas)
    )


def criar_client(cfg, usuario=None):
    """Credencial da IA. Ordem: chave do usuário (cadastrada no painel, cifrada no config.ini) ->
    [agente] usuario_servico (para rodar como serviço) -> variável de ambiente (modo antigo)."""
    usuario = usuario or cfg.get("agente", "usuario_servico", fallback="").strip() or None
    if usuario:
        cred = cripto.carregar(cfg, usuario)
        if not cred:
            raise RuntimeError(f"Usuário '{usuario}' não tem chave de IA cadastrada no painel")
        prov, modelo, chave = cred["provedor"], cred["modelo"], cred["chave"]
    else:
        prov = cfg["agente"].get("provider", "openai").strip().lower()
        modelo = cfg["agente"]["modelo"]
        chave = os.environ[cfg["agente"]["api_key_env"]]
    cfg["agente"]["provider"], cfg["agente"]["modelo"] = prov, modelo  # usado no ciclo e gravado no painel
    return llm.criar_client(prov, chave)


def avaliar(client, cfg, atendimento, historico, catalogo=(), exemplos="", prot=None, fichas_txt="", audios_txt="",
            atualizacoes=()):
    system = SYSTEM.format(criterio=cfg["agente"]["criterio_conclusao"],
                           cqs_bloco=montar_bloco_cqs(catalogo, codigos_cqs(cfg, "principal"),
                                                     codigos_cqs(cfg, "secundario")),
                           exemplos_bloco=exemplos)
    assunto, cliente = atendimento.get("assunto", ""), atendimento.get("cliente", "")
    if prot:
        assunto, cliente = prot.proteger(assunto), prot.proteger(cliente)
    user = (
        f"Assunto: {assunto}\n"
        f"Cliente: {cliente}\n\n"
        f"HISTÓRICO:\n{historico}"
    )
    if fichas_txt:
        user += f"\n\nFICHAS DE VISITA (período do atendimento):\n{fichas_txt}"
    if audios_txt:
        user += f"\n\nÁUDIOS DO ATENDIMENTO (WhatsApp, transcrição automática):\n{audios_txt}"
    if atualizacoes:
        _p = prot.proteger if prot else (lambda x: x)
        user += "\n\nREGISTROS DE ATUALIZAÇÃO (cada um gera um CQS secundário):\n" + "\n".join(
            f"{u['id']}: tipo={u['tipo'] or '?'} | data={u['data'] or '?'} | analista={_p(u['analista']) or '?'}"
            f" | modalidade={u['modalidade'] or 'não informada'}" for u in atualizacoes)
    max_tok = cfg.getint("agente", "max_tokens_resposta", fallback=800)
    texto, tin, tout = llm.chamar(cfg["agente"]["provider"], client, cfg["agente"]["modelo"],
                                  system, user, max_tok)
    m = re.search(r"\{.*\}", texto, re.S)
    if not m:
        raise ValueError(f"Resposta sem JSON: {texto[:200]}")
    res = json.loads(m.group(0))
    if prot:  # devolve os nomes reais só aqui, localmente
        for k in ("resumo", "justificativa", "pendencias"):
            res[k] = prot.pseudo.reidentificar(res.get(k))
    return res, tin, tout


# ---------- ciclo ----------
def ciclo(cfg, lite, client):
    inicio = datetime.now().isoformat(timespec="seconds")
    avaliados = erros = tin = tout = 0
    max_chars = cfg.getint("agente", "max_chars_historico", fallback=12000)
    max_ciclo = cfg.getint("agente", "max_por_ciclo", fallback=20)

    conn = pyodbc.connect(cfg["banco"]["conn_string"], readonly=True, timeout=30)
    try:
        candidatos = consultar(conn, cfg["queries"]["candidatos"])
        log.info("%d candidatos encontrados", len(candidatos))
        # quem saiu da fila no ERP (concluído, ou recebeu iteração depois da 26) some do painel
        _chave = [c.strip().lower() for c in cfg["queries"]["chave_colunas"].split(",")]
        _ids = ["-".join(str(a[c]) for c in _chave) for a in candidatos]
        lite.execute("UPDATE avaliacoes SET na_fila = 0")
        lite.executemany("UPDATE avaliacoes SET na_fila = 1 WHERE atendimento_id = ?", [(i,) for i in _ids])
        lite.commit()
        catalogo = []
        if cfg.has_option("queries", "cqs_tipos"):
            try:
                catalogo = consultar(conn, cfg["queries"]["cqs_tipos"])
                log.info("%d tipos de CQS carregados", len(catalogo))
            except Exception:
                log.exception("Falha ao carregar CQS; seguindo sem classificação")
        por_cod = {str(c["codtiporegistro"]): c for c in catalogo}
        cqs_versao = None
        if catalogo:
            _assin = json.dumps([[str(c.get(k)) for k in ("codtiporegistro", "nometiporegistro",
                                                           "descricaotiporegistro", "pontos")]
                                 for c in sorted(catalogo, key=lambda c: str(c["codtiporegistro"]))])
            # a versão muda se a tabela mudar OU se as listas [cqs] do config.ini mudarem
            _assin += "|P:" + ",".join(sorted(codigos_cqs(cfg, "principal") or [])) \
                      + "|S:" + ",".join(sorted(codigos_cqs(cfg, "secundario") or []))
            cqs_versao = hashlib.sha1(_assin.encode("utf-8")).hexdigest()[:12]
            # painel sempre mostra nome/descrição/pontos ATUAIS de cada código
            lite.executemany("UPDATE avaliacoes SET cqs_nome=?, cqs_descricao=?, cqs_pontos=? WHERE cqs_cod=?",
                             [(limpar(c.get("nometiporegistro")), limpar(c.get("descricaotiporegistro")),
                               str(c.get("pontos")), str(c["codtiporegistro"])) for c in catalogo])
            lite.commit()

        # troca códigos de usuário por nome nas linhas já gravadas (analista)
        usuarios = []
        if cfg.has_option("queries", "usuarios"):
            try:
                usuarios = consultar(conn, cfg["queries"]["usuarios"])
                for u in usuarios:
                    if u.get("nomeusuario"):
                        lite.execute("UPDATE avaliacoes SET analista=? WHERE analista=?",
                                     (u["nomeusuario"], str(u["codusuario"])))
                lite.commit()
            except Exception:
                log.exception("Falha ao atualizar nomes de analistas")

        # LGPD: mascaramento + pseudônimos (camada 1) e OCR local das imagens (camada 2)
        ocr = None
        if cfg.getboolean("privacidade", "ler_imagens", fallback=False):
            ocr = LeitorOCR(cfg.get("privacidade", "tesseract_cmd", fallback=""),
                            cfg.get("privacidade", "ocr_idioma", fallback="por+eng"))
        prot_base = Protecao(cfg, usuarios, ocr)

        # fichas de visita na pasta do cliente (rede)
        localizador = ocr_fichas = None
        fichas_on = cfg.getboolean("fichas", "verificar", fallback=False)
        audios_on = cfg.getboolean("audios", "transcrever", fallback=False)
        if fichas_on or audios_on:   # a pasta do cliente serve às fichas e aos áudios
            mapa = dict(cfg.items("pastas_clientes")) if cfg.has_section("pastas_clientes") else {}
            localizador = LocalizadorFichas(cfg.get("fichas", "raiz", fallback="M:\\"),
                                            cfg.get("fichas", "subpasta", fallback="FichaVisita"), mapa)
        if audios_on:
            prot_base.transcritor = Transcritor(cfg.get("audios", "modelo_whisper", fallback="small"),
                                                cfg.get("audios", "dispositivo", fallback="cpu"), lite,
                                                cfg.getint("audios", "max_minutos_por_audio", fallback=10))
            _m = cfg.get("audios", "mapear_unidade", fallback="").strip()   # ex.: M:\=\\srv\dados\
            if "=" in _m:
                _de, _para = _m.split("=", 1)
                prot_base.mapa_unidade = {_de.strip(): _para.strip()}
        if fichas_on:
            ocr_fichas = ocr or LeitorOCR(cfg.get("privacidade", "tesseract_cmd", fallback=""),
                                          cfg.get("privacidade", "ocr_idioma", fallback="por+eng"))
        exemplos = montar_exemplos(lite, cfg, prot_base, cqs_versao)  # decisões do analista, 1x por ciclo

        for at in candidatos:
            if avaliados >= max_ciclo:
                break
            # chave composta: empresa + número + desdobramento (mesmo número pode ter vários desdobramentos)
            chave = [c.strip().lower() for c in cfg["queries"]["chave_colunas"].split(",")]
            aid = "-".join(str(at[c]) for c in chave)
            try:
                inter = consultar(conn, cfg["queries"]["interacoes"], tuple(at[c] for c in chave))
                # só reavalia se chegou interação nova desde a última avaliação
                ult = lite.execute(
                    "SELECT qtd_interacoes, pode_concluir, cqs_ok, COALESCE(qtd_fichas,0), COALESCE(qtd_audios,0), "
                    "cqs_versao, status, COALESCE(cqs_sec_ok,0) FROM avaliacoes "
                    "WHERE atendimento_id=? ORDER BY id DESC LIMIT 1", (aid,)
                ).fetchone()
                # fichas de visita do período (da 1ª iteração até hoje)
                arquivos = []
                prot = prot_base.para_atendimento(at.get("cliente"))
                # áudios na subpasta do cliente com o nº do atendimento no nome
                audios_pasta = []
                _pc = None
                if localizador:
                    # 1º pelos links das iterações (caminho absoluto, sempre certo); 2º pelo nome do cliente
                    _pc = (pasta_cliente_pelos_links([r.get("texto") for r in inter], localizador.raiz,
                                                     prot_base.mapa_unidade)
                           or localizador.pasta_cliente(at.get("codcliente"), at.get("cliente")))
                    if _pc:
                        prot.pseudo.adicionar(os.path.basename(os.path.normpath(_pc)))   # "GET" também vira [CLIENTE]
                    else:
                        log.info("Atend %s: pasta do cliente não encontrada (sem link nas iterações e nome "
                                 "'%s' não bate com nenhuma pasta; use [pastas_clientes])", aid, at.get("cliente"))
                if audios_on and _pc:
                    audios_pasta = audios_na_pasta(
                        os.path.join(_pc, cfg.get("audios", "subpasta", fallback="Audios WhatsApp")),
                        at.get("numatendimento"))
                if fichas_on and localizador and inter:
                    datas = [r["data"] for r in inter if hasattr(r.get("data"), "date")]
                    if datas:
                        desde = min(datas).date() - timedelta(days=1)
                        arquivos = localizador.fichas(at.get("codcliente"), at.get("cliente"), desde, _pc)
                        arquivos = arquivos[: cfg.getint("fichas", "max_fichas_por_atendimento", fallback=3)]
                # reavalia se chegou interação nova, ficha nova, ou se era "Sim" avaliado antes do recurso CQS
                precisa_cqs = bool(catalogo) and ult and ult[1] == 1 and not ult[2]
                # catálogo de CQS mudou: reclassifica os "Sim" ainda pendentes (os já decididos ficam como estão)
                if (cqs_versao and ult and ult[1] == 1 and ult[5] != cqs_versao and ult[6] == "pendente"
                        and cfg.getboolean("agente", "reavaliar_quando_cqs_mudar", fallback=True)):
                    precisa_cqs = True
                # tem registro de atualização e foi avaliado antes do CQS secundário existir
                atualizacoes = extrair_atualizacoes(inter)
                if atualizacoes and ult and ult[1] == 1 and not ult[7] and ult[6] == "pendente":
                    precisa_cqs = True
                if (ult and ult[0] == len(inter) and ult[3] == len(arquivos) and ult[4] == len(audios_pasta)
                        and not precisa_cqs):
                    # sem interação nova: não reavalia, mas atualiza cliente/analista/assunto
                    lite.execute(
                        "UPDATE avaliacoes SET cliente=?, analista=?, assunto=? WHERE atendimento_id=?",
                        (at.get("cliente"), at.get("analista"), at.get("assunto"), aid),
                    )
                    lite.commit()
                    continue
                if not inter:
                    continue

                historico = montar_historico(inter, max_chars, prot)
                fichas_txt, nomes_fichas = [], []
                max_ficha = cfg.getint("fichas", "max_chars_ficha", fallback=6000)
                for caminho, d in arquivos:
                    try:
                        txt = ler_ficha(caminho, ocr_fichas,
                                        cfg.getint("fichas", "max_paginas", fallback=5))
                    except Exception as e:
                        log.warning("Ficha %s ilegível: %s", caminho, e)
                        txt = ""
                    nome_arq = os.path.basename(caminho)
                    nomes_fichas.append(nome_arq)
                    # o nome do arquivo não vai para a IA (costuma ter o nome do cliente)
                    fichas_txt.append(f"--- Ficha {len(nomes_fichas)} de {d:%d/%m/%Y} ---\n"
                                      + (prot.proteger(txt)[:max_ficha] or "(não foi possível ler o conteúdo)"))
                # áudios da pasta que não foram citados nas iterações
                audios_txt = []
                for c in audios_pasta:
                    r_ = prot.transcrever_audio(c)
                    if r_:
                        audios_txt.append(f"--- Áudio {r_[0]} ---\n{prot.proteger(r_[1])}")
                nomes_audios = [os.path.basename(a) for a in prot.audios_lidos]
                res, i, o = avaliar(client, cfg, at, historico, catalogo, exemplos, prot,
                                    "\n".join(fichas_txt), "\n".join(audios_txt), atualizacoes)
                tin, tout = tin + i, tout + o
                avaliados += 1
                conf = float(res.get("confianca", 0))
                pode = bool(res.get("pode_concluir"))
                pend = str(res.get("pendencias") or "").strip()
                if pode and pend and not re.fullmatch(r"(?i)(nenhuma?|n/?a|-|vazio|sem pend[eê]ncias?\.?)", pend):
                    # trava: se a IA listou pendência, não pode concluir (mesmo que ela tenha dito "Sim")
                    log.warning("Atend %s: IA disse concluir mas listou pendências; marcado como Não", aid)
                    pode = False
                    res["justificativa"] = ("[Bloqueado: há pendências listadas] "
                                            + str(res.get("justificativa") or "")).strip()
                cqs = por_cod.get(str(res.get("cqs"))) if pode and res.get("cqs") is not None else None
                _pri, _sec = codigos_cqs(cfg, "principal"), codigos_cqs(cfg, "secundario")
                if cqs and _pri is not None and str(cqs["codtiporegistro"]) not in _pri:
                    log.warning("Atend %s: IA sugeriu CQS %s, fora dos permitidos como principal; descartado",
                                aid, cqs["codtiporegistro"])
                    cqs = None
                # CQS secundários: o analista vem da extração local (nome real), o código vem da IA
                secundarios = []
                if pode and atualizacoes:
                    escolhas = {str(x.get("id")): x.get("cqs") for x in (res.get("cqs_atualizacoes") or [])
                                if isinstance(x, dict)}
                    for u in atualizacoes:
                        c2 = por_cod.get(str(escolhas.get(u["id"])))
                        if c2 and _sec is not None and str(c2["codtiporegistro"]) not in _sec:
                            c2 = None   # fora dos permitidos como secundário
                        secundarios.append({**{k: u[k] for k in ("analista", "data", "tipo", "modalidade")},
                                            "cqs": str(c2["codtiporegistro"]) if c2 else None,
                                            "cqs_nome": limpar(c2.get("nometiporegistro")) if c2 else None,
                                            "cqs_pontos": str(c2.get("pontos")) if c2 else None})
                    if any(s["cqs"] is None for s in secundarios):
                        log.warning("Atend %s: IA não classificou todos os registros de atualização", aid)
                lite.execute(
                    """INSERT INTO avaliacoes(atendimento_id,cliente,assunto,analista,
                       qtd_interacoes,pode_concluir,confianca,justificativa,pendencias,
                       resumo,avaliado_em,modelo,cqs_cod,cqs_nome,cqs_descricao,cqs_pontos,cqs_ok,
                       fichas,qtd_fichas,audios,qtd_audios,cqs_versao,cqs_secundarios,cqs_sec_ok)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,1)""",
                    (aid, at.get("cliente"), at.get("assunto"), at.get("analista"),
                     len(inter), int(pode), conf, res.get("justificativa"),
                     res.get("pendencias"), res.get("resumo"), datetime.now().isoformat(timespec="seconds"),
                     cfg["agente"]["modelo"],
                     str(cqs["codtiporegistro"]) if cqs else None,
                     limpar(cqs.get("nometiporegistro")) if cqs else None,
                     limpar(cqs.get("descricaotiporegistro")) if cqs else None,
                     str(cqs.get("pontos")) if cqs else None,
                     "\n".join(nomes_fichas) or None, len(arquivos),
                     "\n".join(nomes_audios) or None, len(audios_pasta), cqs_versao,
                     json.dumps(secundarios, ensure_ascii=False) if secundarios else None),
                )
                lite.commit()
                log.info("Atend %s -> concluir=%s conf=%.2f cqs=%s imagens_ocr=%d", aid, pode, conf,
                         cqs["codtiporegistro"] if cqs else "-", prot.imagens_lidas)
                if arquivos:
                    log.info("Atend %s: %d ficha(s) de visita lida(s)", aid, len(arquivos))
                if prot.audios_lidos:
                    log.info("Atend %s: %d áudio(s) transcrito(s)", aid, len(prot.audios_lidos))
            except Exception as e:
                erros += 1
                log.exception("Erro no atendimento %s: %s", aid, e)
    finally:
        conn.close()

    lite.execute(
        "INSERT INTO execucoes(inicio,fim,candidatos,avaliados,erros,tokens_in,tokens_out) "
        "VALUES (?,?,?,?,?,?,?)",
        (inicio, datetime.now().isoformat(timespec="seconds"),
         len(candidatos), avaliados, erros, tin, tout),
    )
    lite.commit()
    log.info("Ciclo ok: %d avaliados, %d erros, %d/%d tokens", avaliados, erros, tin, tout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uma-vez", action="store_true")
    ap.add_argument("--max", type=int, default=None, help="sobrescreve max_por_ciclo nesta execução")
    ap.add_argument("--usuario", default=None, help="usa a chave de IA cadastrada por este usuário")
    args = ap.parse_args()

    while True:
        espera = 15 * 60
        try:
            cfg = ler_cfg()
            if args.max is not None:
                cfg["agente"]["max_por_ciclo"] = str(args.max)
            espera = cfg.getint("agente", "intervalo_minutos", fallback=15) * 60
            lite = init_sqlite(BASE / cfg["saida"]["sqlite"])
            client = criar_client(cfg, args.usuario)
            ciclo(cfg, lite, client)
            lite.close()
        except Exception:
            log.exception("Falha no ciclo")
        if args.uma_vez:
            break
        time.sleep(espera)


if __name__ == "__main__":
    main()
