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
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import pyodbc

import cripto
import historico as hist
import migracoes
import motivos
import rdm
import similares
import llm
from audios import (Transcritor, audios_na_pasta, caminhos_no_texto, mapear_unidade,
                    pasta_cliente_pelos_links)
from agendamento import NOMES, proxima_execucao
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
                "cqs_sec_ok INTEGER DEFAULT 0", "ciclo_inicio TEXT", "qtd_imagens INTEGER DEFAULT 0",
                "status_cqs TEXT", "cqs_correto TEXT", "motivo_cqs TEXT", "decidido_por TEXT", "motivo_cat TEXT",
                "rdms TEXT", "continuidade TEXT", "so_atualizacao INTEGER", "versao_prompt TEXT"):
        try:  # bancos criados antes do recurso CQS
            con.execute(f"ALTER TABLE avaliacoes ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    migracoes.migrar(con)   # revisões antigas -> análise e CQS separados
    con.execute("CREATE INDEX IF NOT EXISTS ix_at ON avaliacoes(atendimento_id)")
    con.execute(
        """CREATE TABLE IF NOT EXISTS execucoes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inicio TEXT, fim TEXT, candidatos INTEGER, avaliados INTEGER,
            erros INTEGER, tokens_in INTEGER, tokens_out INTEGER)"""
    )
    try:  # tokens de entrada lidos do cache do provedor (custam bem menos)
        con.execute("ALTER TABLE execucoes ADD COLUMN tokens_cache INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
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


def separar_falas(cfg):
    """[privacidade] separar_falas_whatsapp = sim (padrão): print de conversa do WhatsApp sai com as falas separadas
    (CLIENTE / ANALISTA); nao = OCR comum, como antes (o testar.py --ocr-simples usa para comparar)."""
    return cfg.getboolean("privacidade", "separar_falas_whatsapp", fallback=True)


class Protecao:
    """Aplica as camadas de LGPD em tudo que vai para a IA (config.ini, seção [privacidade])."""

    def __init__(self, cfg, usuarios=(), ocr=None):
        self.mascarar = cfg.getboolean("privacidade", "mascarar_dados", fallback=True)
        self.pseudo_on = cfg.getboolean("privacidade", "pseudonimizar_nomes", fallback=True)
        self.max_imgs = cfg.getint("privacidade", "max_imagens_por_atendimento", fallback=5)
        self.separar_falas = separar_falas(cfg)
        self.usuarios = list(usuarios)
        self.ocr = ocr
        self.imagens_lidas = 0
        self.pular_imagens = 0           # as primeiras ficam de fora quando passam do limite (ver montar_historico)
        self.print_cliente = False       # a última chamada de texto_imagens achou print de conversa em que o cliente
        self.prints_cliente = set()      # confirma o resultado ou agradece; índices dessas interações (montar_historico)
        self.pseudo = Pseudonimizador(self.usuarios if self.pseudo_on else ())
        self.transcritor = None          # áudios (definido no ciclo, se [audios] transcrever = sim)
        self.mapa_unidade = {}
        self.max_audios = cfg.getint("audios", "max_audios_por_atendimento", fallback=5)
        self.audios_lidos = []

    def para_atendimento(self, cliente):
        p = Protecao.__new__(Protecao)
        p.__dict__.update(self.__dict__)
        p.imagens_lidas = 0
        p.pular_imagens = 0
        p.print_cliente = False
        p.prints_cliente = set()
        p.audios_lidos = []
        p.pseudo = Pseudonimizador(self.usuarios, cliente) if self.pseudo_on else Pseudonimizador()
        return p

    def proteger(self, txt):
        txt = "" if txt is None else str(txt)
        if self.mascarar:
            txt = mascarar_pii(txt)
        return self.pseudo.ocultar(txt)

    def texto_imagens(self, texto_bruto):
        self.print_cliente = False
        if not (self.ocr and self.ocr.ok):
            return ""
        partes = []
        for png in extrair_pngs_rtf(texto_bruto):
            if self.imagens_lidas >= self.max_imgs:
                break
            if self.pular_imagens > 0:
                self.pular_imagens -= 1
                continue
            # print de conversa do WhatsApp sai com as falas separadas (CLIENTE / ANALISTA); o resto, OCR comum
            r = (self.ocr.ler_print(png) if self.separar_falas and hasattr(self.ocr, "ler_print")
                 else {"conversa": False, "texto": self.ocr.ler(png), "cliente": False, "confirma": False})
            if r["texto"]:
                self.imagens_lidas += 1
                if r["conversa"]:
                    self.print_cliente = self.print_cliente or r.get("confirma", False)
                    partes.append(f" [Imagem {self.imagens_lidas} (conversa de WhatsApp lida por OCR): {r['texto']}]")
                else:
                    partes.append(f" [Imagem {self.imagens_lidas} (texto lido por OCR): {r['texto']}]")
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


# ---------- registro de atualização ----------
# Definição da empresa: é registro de atualização o texto com os 4 dados obrigatórios, em qualquer ordem e formato:
# atualização (completa ou parcial), data da atualização, analista que fez e tipo (remota ou presencial). Ex.:
#   "Ticket 1587449 - Atualização: Completa - Data: 14/10/2026 - Analista: Alexandre - Remoto."
#   "Ticket 594403 - Atualização Completa - Remota - Data 07/08/2026-Analista Angela"
_RX_ATUAL_TIPO = re.compile(r"(?i)\batualiza[cç][aã]o\s*[:\-–]?\s*(?P<tipo>complet\w*|parcia\w*)")
_RX_ATUAL_DATA = re.compile(r"(?i)(?:\bdata\b\s*(?:da\s+atualiza[cç][aã]o\s*)?:?\s*)?"
                            r"(?<![\d/])(?P<data>\d{1,2}/\d{1,2}/?\d{2,4})(?:[^\W\d_](?![^\W\d_]))?(?![\d/])")
_RX_ATUAL_MODAL = re.compile(r"(?i)\b(?P<mod>remot\w*|presencia\w*|in\s*loco)\b")
_NAO_NOME = r"(?!(?i:remot|presencia|in\s*loco\b|data\b|atendimento\b|atualiza|ticket\b|obs\b|e\b))"
_CAP = r"[A-ZÁÀÂÃÉÊÍÓÔÕÚÜÇ][\w']*(?:\.[^\W\d_][\w']*)*"
# 1º nome em qualquer caixa ("Alexandre", "richardy", "Alexandre.Amorim"); sobrenome e os nomes depois de
# "/", ",", "e" só com maiúscula ("Alexandre / Heberty Richardy"), para não engolir o texto que vem depois
_NOME = rf"{_NAO_NOME}[^\W\d_][\w']*(?:\.[^\W\d_][\w']*)*(?:[ \t]+{_NAO_NOME}{_CAP}){{0,2}}"
_NOME_CAP = rf"{_NAO_NOME}{_CAP}(?:[ \t]+{_NAO_NOME}{_CAP}){{0,2}}"
_RX_ATUAL_ANALISTA = re.compile(rf"(?i:\b(?:pel[oa]\s+)?analista)\s*:?\s*"
                                rf"(?P<nomes>{_NOME}(?:\s*(?:/|,|&|\be\b)\s*{_NOME_CAP})*)")
# o que costuma acompanhar os 4 dados sem ser "texto a mais"
_RX_ATUAL_ACOMPANHA = re.compile(
    r"(?i)\bticket\b\s*:?\s*(?:n[º°o]\.?\s*)?\d+|\bcontrole\s+de\s+atualiza[cç][aã]o\b"
    r"|\batendimento\s+(?:finalizado|conclu[ií]do|enviado\s+para\s+"
    r"(?:revis[aã]o|conclus[aã]o))|\b(?:d[oa]\s+|n[oa]\s+)?sistema\b|\b(?:tipo|modalidade)\b\s*:?"
    r"|\b(?:foi|realizad[ao]|feit[ao]|efetuad[ao]|em|pel[oa]|de\s+forma|a|o)\b")


def _ultimo(rx, txt):
    m = None
    for m in rx.finditer(txt):
        pass
    return m


def registros_atualizacao(txt):
    """Registros de atualização num texto, só os que têm os 4 dados obrigatórios (os dados são procurados depois
    de "Atualização Completa/Parcial" e, se faltar, logo antes). [{tipo, data, analista, modalidade}] com o nome
    REAL do analista (fica só local)."""
    marcas = list(_RX_ATUAL_TIPO.finditer(txt))
    achados = []
    for i, m in enumerate(marcas):
        depois = txt[m.end(): marcas[i + 1].start() if i + 1 < len(marcas) else m.start() + 300]
        antes = txt[max(marcas[i - 1].end() if i else 0, m.start() - 150): m.start()]
        d, a, md = ((rx.search(depois) or _ultimo(rx, antes))
                    for rx in (_RX_ATUAL_DATA, _RX_ATUAL_ANALISTA, _RX_ATUAL_MODAL))
        if d and a and md:
            achados.append({
                "tipo": "Completa" if m.group("tipo").lower().startswith("complet") else "Parcial",
                "data": d.group("data"),
                "analista": a.group("nomes").strip(" -–/,"),
                "modalidade": "remoto" if md.group("mod").lower().startswith("remot") else "presencial",
            })
    return achados


def extrair_atualizacoes(rows):
    """Registros de atualização nas iterações (cada um gera um CQS secundário para o analista do registro).
    Retorna [{id, tipo, data, analista, modalidade}]. O mesmo registro repetido (o pedido de revisão costuma
    copiar o texto) conta uma vez só."""
    achados, vistos = [], set()
    for r in rows:
        for item in registros_atualizacao(limpar(r.get("texto"))):
            chave = (item["tipo"], item["data"], _norm(item["analista"]), item["modalidade"])
            if chave not in vistos:
                vistos.add(chave)
                achados.append({"id": f"U{len(achados) + 1}", **item})
    return achados


def texto_alem_do_registro(txt):
    """O que sobra tirando os dados do registro de atualização e o que costuma acompanhá-los ("Ticket N",
    "do sistema", "Atendimento finalizado"...). Vazio = o texto é só o registro."""
    for rx in (_RX_ATUAL_TIPO, _RX_ATUAL_DATA, _RX_ATUAL_ANALISTA, _RX_ATUAL_MODAL, _RX_ATUAL_ACOMPANHA):
        txt = rx.sub(" ", txt)
    return re.sub(r"[\W_]+", " ", txt).strip()


def classificar_so_atualizacao(rows):
    """Atendimento aberto só para documentar atualização: todas as interações antes do pedido de revisão (tipo 26;
    sem nenhuma, o próprio pedido) têm registro de atualização com os 4 dados obrigatórios.
    'puro' = nada além do registro; 'extra' = alguma tem texto a mais (ex.: OBS) ou imagem; None = não é."""
    if not rows:
        return None
    antes = [r for r in rows if str(r.get("codtipo")) != "26"] if "codtipo" in rows[-1] else rows[:-1]
    nivel = "puro"
    for r in antes or rows:
        txt = limpar(r.get("texto"))
        if not registros_atualizacao(txt):
            return None
        if texto_alem_do_registro(txt) or extrair_pngs_rtf(r.get("texto")):
            nivel = "extra"
    return nivel


def cqs_combina_modalidade(cqs, modalidade):
    """CQS de atualização "remota" não serve para registro presencial/in loco, nem CQS de visita/in loco para remoto."""
    nome, mod = _norm(cqs.get("nometiporegistro")), _norm(modalidade)
    if mod.startswith("remot"):
        return "in loco" not in nome and "visita" not in nome
    if mod in ("presencial", "in loco", "no cliente"):
        return "remot" not in nome
    return True


def cqs_so_atualizacao(cfg, modalidade, local):
    """CQS do atendimento só de atualização, sem IA: [cqs] so_atualizacao = remoto:65, presencial_jf:60,
    presencial_fora:59. Presencial sem cidade do cliente = None (o revisor escolhe)."""
    m = {}
    for p in cfg.get("cqs", "so_atualizacao", fallback="").split(","):
        if ":" in p:
            k, v = p.split(":", 1)
            m[k.strip().lower()] = v.strip()
    if str(modalidade or "").startswith("remot"):
        return m.get("remoto")
    return {"dentro": m.get("presencial_jf"), "fora": m.get("presencial_fora")}.get(local)


def resumo_atualizacao(u):
    """Resumo do atendimento só de atualização, montado do próprio registro (nome real; fica só local)."""
    txt = "Registro de atualização" + (f" {u['tipo'].lower()}" if u.get("tipo") else "") + " do sistema"
    txt += (f", realizada em {u['data']}" if u.get("data") else "") + \
           (f" ({u['modalidade']})" if u.get("modalidade") else "") + \
           (f" pelo analista {u['analista']}" if u.get("analista") else "")
    return txt + ". Atendimento aberto apenas para documentar a atualização."


_CQS_SO_ATUALIZACAO = (
    "Classifique o registro em \"cqs_atualizacoes\" e use o MESMO código em \"cqs\"; o código precisa corresponder à "
    "modalidade (atualização presencial/in loco NUNCA usa o CQS de atualização remota). No resumo, descreva a "
    "atualização realizada (tipo, data, modalidade).")
SO_ATUALIZACAO_IA = {
    "puro": "ATENDIMENTO SÓ DE REGISTRO DE ATUALIZAÇÃO: este atendimento foi aberto apenas para documentar a "
            "atualização acima (registro com os dados obrigatórios: atualização completa/parcial, data, analista e "
            "modalidade) e gerar o CQS do analista. Pela regra da empresa ele deve ser CONCLUÍDO: pode_concluir=true "
            "e pendencias vazio (não exija confirmação do cliente nem mais detalhe). " + _CQS_SO_ATUALIZACAO,
    "extra": "ATENDIMENTO SÓ DE REGISTRO DE ATUALIZAÇÃO, COM TEXTO A MAIS: as interações são registros de atualização "
             "(atualização completa/parcial, data, analista e modalidade) com algum texto além desses dados (ex.: uma "
             "observação). Pela regra da empresa, o registro em si NÃO precisa de confirmação do cliente, de contato "
             "nem de mais detalhe. Avalie SÓ o texto a mais: se ele registrar algo ainda pendente (ex.: \"aguardando "
             "ficha de visita para concluir\", \"falta atualizar o servidor\"), pode_concluir=false e liste em "
             "\"pendencias\"; se for só informativo, pode_concluir=true. " + _CQS_SO_ATUALIZACAO,
}


# ---------- contato da interação (AtendimentoIteracao.NomeContato) ----------
def _norm(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower().strip()


def _partes(s):
    return [t for t in re.split(r"[\s._\-]+", _norm(s)) if t]


def classificar_contato(contato, proximos=(), internos=frozenset()):
    """Com quem o analista falou: 'analista' (o autor da interação ou o analista do atendimento, pelo login
    "Vinicius.Souza" ou só pelo nome "Vinícius"), 'interno' (login de outro usuário), 'nao_informado' ou 'cliente'.
    Só a classificação vai para a IA, nunca o nome do contato (LGPD)."""
    ct = _partes(contato)
    if not ct:
        return "nao_informado"
    for p in proximos:
        pt = _partes(p)
        if pt and ct[0] == pt[0] and (len(ct) == 1 or (len(pt) > 1 and pt[1].startswith(ct[1]))):
            return "analista"
    # login de uma palavra só ("ramon", "daniel") coincide com o nome de contatos do cliente: só conta como interno
    # o login com ponto ("Fulano.Sobrenome") ou com nome completo
    n = _norm(contato)
    return "interno" if n in internos and (re.search(r"[._]", n) or len(ct) > 1) else "cliente"


ROTULO_CONTATO = {"cliente": "contato: cliente", "analista": "contato: o próprio analista, não é o cliente",
                  "interno": "contato: usuário interno, não é o cliente", "nao_informado": "contato: não informado"}
_QUEM_CONTATO = {"analista": "o próprio analista como contato", "interno": "um usuário interno como contato",
                 "nao_informado": "o contato em branco"}


_SO_PEDIDO_REVISAO = re.compile(r"(?i)^\W*(atendimento\s+)?enviado\s+para\s+revis\w*\W*$")
# fecho padrão do pedido de revisão: não conta como texto próprio da conclusão
_FECHO_PEDIDO = re.compile(r"(?i)-?\s*atendimento\s+(enviado\s+para\s+revis\w*|finalizado|conclu[ií]do|encerrado)\W*")


def resumo_no_pedido(r):
    """O pedido de revisão (tipo 26) traz o resumo da conclusão escrito pelo analista (texto além do fecho padrão)?"""
    return len(re.findall(r"[^\W\d_]{3,}", _FECHO_PEDIDO.sub(" ", limpar(r.get("texto"))))) >= 6


def _indice_pedido(rows):
    """Índice do pedido de revisão (a última interação, tipo 26) em rows, ou None."""
    if not rows:
        return None
    return len(rows) - 1 if "codtipo" not in rows[-1] or str(rows[-1].get("codtipo")) == "26" else None


def _indice_conclusao(rows, analista=None, internos=frozenset()):
    """Índice da interação de conclusão: a última antes do pedido de revisão que não seja vazia (sem texto e sem
    imagem), nem só "enviado para revisão", nem nota interna de OUTRO usuário com ele mesmo ou outro interno como
    contato (ex.: a devolução do revisor). None se não houver."""
    tem_tipo = bool(rows) and "codtipo" in rows[-1]
    for i in range(len(rows) - 1, -1, -1):
        r = rows[i]
        if (str(r.get("codtipo")) == "26") if tem_tipo else i == len(rows) - 1:
            continue
        autor, txt = r.get("autor"), limpar(r.get("texto"))
        if (not re.search(r"[^\W\d_]{3,}", txt) and not extrair_pngs_rtf(r.get("texto"))) or _SO_PEDIDO_REVISAO.match(txt):
            continue
        if (analista and autor and classificar_contato(autor, (analista,), internos) != "analista"
                and classificar_contato(r.get("contato"), (autor,), internos) in ("analista", "interno")):
            continue
        return i
    return None


def contato_conclusao(rows, analista=None, internos=frozenset()):
    """Classificação do contato da conclusão. 'cliente' quando o pedido de revisão (tipo 26) traz o resumo da
    conclusão com contato do cliente (os analistas costumam escrever a conclusão nele); senão, o contato da
    interação de conclusão (_indice_conclusao). None quando a query [queries] interacoes não traz o NomeContato ou
    não há interação assim. Print de conversa com fala do cliente na conclusão: ver print_do_cliente_na_conclusao."""
    if not rows or "contato" not in rows[-1]:
        return None
    p = _indice_pedido(rows)
    if (p is not None and resumo_no_pedido(rows[p]) and
            classificar_contato(rows[p].get("contato"), (rows[p].get("autor"), analista), internos) == "cliente"):
        return "cliente"
    i = _indice_conclusao(rows, analista, internos)
    return None if i is None else classificar_contato(rows[i].get("contato"), (rows[i].get("autor"), analista),
                                                      internos)


def print_do_cliente_na_conclusao(rows, prot, analista=None, internos=frozenset()):
    """O analista anexou na conclusão (o pedido de revisão ou a interação de conclusão) um print de conversa do
    WhatsApp em que o cliente confirma o resultado ou agradece (privacidade.confirma_resultado; um "ok" solto não
    basta): o print comprova que a conclusão foi com o cliente, seja qual for o contato registrado. Depende de
    montar_historico já ter lido as imagens (prot.prints_cliente)."""
    achados = getattr(prot, "prints_cliente", None)
    if not achados:
        return False
    return bool(achados & ({_indice_pedido(rows), _indice_conclusao(rows, analista, internos)} - {None}))


def pendencia_contato(classe):
    return (f"Contato: a interação de conclusão foi registrada com {_QUEM_CONTATO[classe]}; não comprova que falou "
            "com o cliente. Registrar a conclusão com o aceite do cliente e o nome do contato dele.")


# ---------- continuidade em outro atendimento ----------
_CITA_ATEND = re.compile(r"(?i)atend(?:imento)?s?\.?\s*(?:n[º°o]\.?\s*|de\s+n[º°o]\.?\s*|n[úu]mero\s*)?:?\s*(\d{5,9})")
SQL_ATEND_EXISTE = "SELECT COUNT(*) AS n FROM Atendimento WITH (NOLOCK) WHERE CodEmpresa = ? AND NumAtendimento = ?"


def _texto_interacoes(inter):
    return "\n".join(limpar(r.get("texto")) for r in inter)


def citados_outros(inter, numero):
    """Números de OUTROS atendimentos citados nas interações ("Atend. 1584469/0", "atendimento nº 2415094")."""
    return {m.group(1) for m in _CITA_ATEND.finditer(_texto_interacoes(inter))} - {str(numero)}


def _existe_atendimento(conn, cfg, empresa, num):
    sql = cfg.get("queries", "atendimento_existe", fallback=SQL_ATEND_EXISTE)
    return bool(consultar(conn, sql, (empresa, int(num)))[0].get("n"))


def texto_citados(conn, cfg, at, inter):
    """Seção para a IA com os OUTROS atendimentos citados no histórico que existem no ERP (vazia se nenhum)."""
    nums = sorted(n for n in citados_outros(inter, at.get("numatendimento"))
                  if _existe_atendimento(conn, cfg, at.get("codempresa"), n))
    if not nums:
        return ""
    return ("OUTROS ATENDIMENTOS CITADOS NO HISTÓRICO (conferidos, existem no sistema): " + ", ".join(nums) + ".\n"
            "Se o assunto que falta segue em um deles, aplique a exceção \"CONTINUIDADE EM OUTRO ATENDIMENTO\" (o número "
            "ESTÁ informado) e devolva-o em \"atendimento_continuidade\".")


def validar_continuidade(conn, cfg, res, at, inter):
    """Confere o atendimento que dará andamento informado pela IA: precisa ser outro número, estar escrito no
    histórico e existir no ERP. Retorna (número, None) se vale, (número, motivo) se não vale, (None, None) sem continuidade."""
    num = re.sub(r"\D", "", str(res.get("atendimento_continuidade") or "").split("/")[0])
    if not num:
        return None, None
    if num == str(at.get("numatendimento")):
        return num, "é o próprio atendimento analisado"
    if not re.search(rf"(?<!\d){num}(?!\d)", _texto_interacoes(inter)):
        return num, "não aparece no histórico"
    if not _existe_atendimento(conn, cfg, at.get("codempresa"), num):
        return num, "não existe no sistema"
    return num, None


def montar_historico(rows, max_chars, prot=None, analista=None, internos=frozenset()):
    linhas = []
    if prot and prot.ocr and prot.ocr.ok:
        # passou do limite de imagens: lê as ÚLTIMAS (a confirmação do cliente costuma estar nos últimos prints)
        total = sum(len(extrair_pngs_rtf(r.get("texto"))) for r in rows)
        prot.pular_imagens = max(0, total - prot.max_imgs)
    for i, r in enumerate(rows):
        d = r.get("data")
        d = d.strftime("%d/%m/%Y %H:%M") if hasattr(d, "strftime") else str(d)
        autor = r.get("autor", "")
        texto = limpar(r.get("texto"))
        rot = ""
        if "contato" in r:   # a query traz o NomeContato: classifica com o nome real, antes do pseudônimo
            rot = f" ({ROTULO_CONTATO[classificar_contato(r.get('contato'), (autor, analista), internos)]})"
        if prot:
            texto += prot.texto_imagens(r.get("texto"))
            if prot.print_cliente:
                prot.prints_cliente.add(i)
            texto += prot.texto_audios(r.get("texto"), texto)
            autor, texto = prot.proteger(autor), prot.proteger(texto)
        linhas.append(f"[{d}] tipo {r.get('tipo', '')} - {autor}{rot}: {texto}")
    txt = "\n".join(linhas)
    if len(txt) > max_chars:  # mantém começo (contexto) e fim (situação atual)
        ini = int(max_chars * 0.2)
        txt = txt[:ini] + "\n[... trecho omitido ...]\n" + txt[-(max_chars - ini):]
    return txt


# ---------- Claude ----------
POSTURA_CONSERVADORA = "Seja conservador: na dúvida, pode_concluir=false."

CRITERIO_REGISTRO = """    - O registro precisa deixar claro O QUE o cliente pediu e O QUE foi feito ou orientado
      (qual ajuste, qual consulta/relatório, qual caminho no sistema). Registro vago ou genérico
      ("ajustado", "orientado", "verificado") sem dizer o quê NÃO pode ser concluído.
    - Os registros precisam ser coerentes entre si (mesmo assunto). Se divergirem, NÃO concluir.
    - Se o analista citou apurações/verificações a fazer, o resultado delas precisa estar registrado.
    - Nesses casos, explique em 'pendencias' o que falta no registro (ex.: "Registro: não informa qual consulta foi usada")."""


_CONF_VALE = ("CONTA COMO CONFIRMAÇÃO (qualquer uma basta; o gerente aceita todas): "
              "(a) o cliente disse, em texto, áudio ou PRINT (trecho de imagem lido por OCR), que resolveu, funcionou, "
              "conseguiu ou deu certo, ou respondeu a orientação com OK/agradecimento (\"ok, obrigado\", \"deu tudo "
              "certo\"); (b) o analista registrou que o CLIENTE obteve o resultado, fez, testou ou conseguiu (\"o cliente "
              "realizou um teste e o sistema iniciou normalmente\", \"seguindo essa sequência a nota fechou no valor "
              "desejado\", \"a cliente conseguiu emitir a nota\", \"testado junto com o cliente e funcionou\", \"cliente "
              "acessou normalmente\", \"cliente confirmou\"), ou o resultado conferido no sistema do cliente (\"nota "
              "gravada e transmitida com sucesso\", \"integração efetuada sem erros\"); (c) o cliente informou que "
              "resolveu sozinho ou que não precisa mais do atendimento (\"já encontrou a diferença\", \"não precisa mais "
              "de suporte\"); (d) DÚVIDA pura (pergunta sobre como o sistema funciona, sem erro para corrigir): a "
              "resposta registrada de forma clara e específica, com \"a dúvida do cliente foi esclarecida\" ou "
              "equivalente (menos o PEDIDO NEGADO, abaixo). Procure em TODO o histórico. NÃO conta: só a AÇÃO do "
              "analista, mesmo \"com êxito\" ou \"com "
              "sucesso\" (\"cadastro realizado com êxito\", \"demonstrado\", \"orientado\", \"ajustado\", \"corrigido\"), "
              "nem resultado que não diga quem verificou (\"passou a funcionar\", \"passou a ser exibido corretamente\") "
              "quando o atendimento trata de um PROBLEMA do cliente.")

_CONF_ONDE = (" A confirmação pode estar em QUALQUER interação, inclusive no resumo do pedido de revisão, e não precisa ser "
              "uma frase do próprio cliente quando o analista registrou o resultado obtido pelo cliente.")

# motivo do gerente no caso 437 (22/10): "não consta o aceite do cliente, nem digitado, nem no print"
_CONF_PEDIDO_NEGADO = (" PEDIDO NEGADO: se o cliente pediu algo (alterar, excluir, um recurso, um ajuste), mesmo em forma "
                       "de pergunta, e a resposta foi que NÃO é possível ou que terá de ser feito de outro jeito "
                       "(manualmente, por outro caminho, com RDM de orçamento), informar o cliente NÃO basta: só conclua "
                       "com o ACEITE do cliente DEPOIS dessa resposta (fala dele no texto, em áudio ou como CLIENTE num "
                       "print, concordando, ou o analista registrar que o cliente aceitou ou confirmou, inclusive em "
                       "ligação ou reunião). Ex.: pediu para apagar um e-mail do cadastro, o analista respondeu \"não é "
                       "possível, teria que ser manual\" e o print só tem a fala do ANALISTA = pode_concluir=false "
                       "(pendência: \"Confirmar com o cliente o aceite da resposta\").")

CONFIRMACAO = {
    "sim": ("CONFIRMAÇÃO DO CLIENTE É OBRIGATÓRIA: só conclua se o histórico mostrar que o problema foi resolvido "
            "ou que o cliente conseguiu executar o que foi orientado. " + _CONF_VALE + _CONF_ONDE + _CONF_PEDIDO_NEGADO
            + " Sem nenhuma dessas "
            "evidências, NÃO conclua (pendência: \"Confirmar com o cliente se ...\"). Esta regra prevalece sobre "
            "qualquer critério acima que aceite concluir sem retorno do cliente."),
    "erros": ("CONFIRMAÇÃO DO CLIENTE: quando o atendimento trata de ERRO, FALHA ou PROBLEMA no sistema/ambiente, só conclua "
              "com confirmação de que foi resolvido. " + _CONF_VALE + _CONF_ONDE + _CONF_PEDIDO_NEGADO
              + " Quando é DÚVIDA ou ORIENTAÇÃO, basta "
              "a orientação estar registrada de forma clara e específica (não precisa de confirmação)."),
}


def modo_confirmacao(cfg):
    """[agente] exigir_confirmacao = sim (padrão) | erros | nao"""
    v = cfg.get("agente", "exigir_confirmacao", fallback="sim").strip().lower()
    return {"s": "sim", "true": "sim", "1": "sim", "erro": "erros", "não": "nao", "n": "nao", "false": "nao",
            "0": "nao"}.get(v, v if v in ("sim", "erros", "nao") else "sim")


def texto_confirmacao(cfg):
    m = modo_confirmacao(cfg)
    if m not in CONFIRMACAO:
        return ""
    excecao = (" EXCEÇÃO: o assunto que segue em outro atendimento com o número informado não precisa de confirmação "
               "(veja \"CONTINUIDADE EM OUTRO ATENDIMENTO\")." if texto_continuidade(cfg) else "")
    return "\n" + CONFIRMACAO[m] + excecao + "\n"


CONTATO = ("CONTATO DE CADA INTERAÇÃO: cada linha do histórico traz \"(contato: ...)\", com quem o analista falou naquela "
           "interação. \"contato: cliente\" é uma pessoa do cliente; \"o próprio analista\" ou \"usuário interno\" quer dizer "
           "que ninguém do cliente participou (registro interno). Confirmação ou aceite DITO pelo cliente só vale em "
           "interação com contato do cliente ou numa fala CLIENTE de print de conversa do WhatsApp; o resultado "
           "registrado pelo analista (inclusive no resumo do pedido de revisão) vale mesmo que o contato daquela "
           "linha seja o próprio analista.")
CONTATO_CONCLUSAO = (" A conclusão precisa ter sido tratada com o cliente: o pedido de revisão (quando traz o resumo da "
                     "conclusão) OU a última interação antes dele precisa ter contato do cliente ou um print de "
                     "conversa com fala do CLIENTE; se nenhum dos dois tiver (contato = o próprio analista, usuário "
                     "interno ou não informado, sem esse print), NÃO conclua (pendência: \"Contato: a interação de "
                     "conclusão não foi com o cliente\").")


CONTINUIDADE = """EXCEÇÃO - CONTINUIDADE EM OUTRO ATENDIMENTO: se o histórico mostra que o assunto que falta (pendência,
monitoramento, correção, atualização, demanda nova) vai seguir em OUTRO atendimento e informa o NÚMERO dele (ex.: "dado
continuidade no Atend. 1584469/0", "assunto será tratado no atendimento 2415094"), o que foi transferido NÃO é pendência
deste atendimento e ele pode ser concluído. Para o assunto transferido não exija confirmação do cliente nem contato do
cliente na interação de conclusão, e as regras de "manter em monitoramento" não se aplicam: o acompanhamento segue no
outro atendimento. Continuam valendo: o registro precisa dizer com clareza o que foi feito aqui até a transferência;
pendência que NÃO foi transferida continua impedindo; RDM vinculada a ESTE atendimento ainda pendente continua impedindo.
Informe o número em "atendimento_continuidade" (só os dígitos, sem o desdobramento) e escolha o CQS pelo que foi feito
neste atendimento até a transferência. Se o histórico diz que outro atendimento dará andamento mas NÃO informa o número,
é pendência ("Texto: informar o número do atendimento que dará andamento"). Sem continuidade, use null."""


def texto_continuidade(cfg):
    """[agente] continuidade_outro_atendimento = sim (padrão) | nao"""
    return CONTINUIDADE if cfg.getboolean("agente", "continuidade_outro_atendimento", fallback=True) else ""


def texto_contato(cfg):
    """[agente] exigir_contato_cliente = sim (padrão) | nao"""
    exigir = cfg.getboolean("agente", "exigir_contato_cliente", fallback=True)
    excecao = (" Exceção: continuidade em outro atendimento com o número informado (veja adiante)."
               if exigir and texto_continuidade(cfg) else "")
    return "\n" + CONTATO + (CONTATO_CONCLUSAO + excecao if exigir else "") + "\n"


ARQ_REGRAS = BASE / "regras_aprovadas.txt"
SECAO_PODE_CONCLUIR = re.compile(r"(?im)^[ \t]*QUANDO PODE CONCLUIR\b.*$")


def regras_revisao(cfg=None, arquivo=None):
    """Regras de revisão aprovadas pelo gerente (regras_aprovadas.txt, gerado a partir de regras_sugeridas.txt).
    'arquivo' troca o arquivo de regras (o testar.py usa para comparar uma proposta sem gravar nas aprovadas)."""
    try:
        txt = "\n".join(l for l in Path(arquivo or ARQ_REGRAS).read_text(encoding="utf-8").splitlines()
                        if not l.lstrip().startswith("#")).strip()
    except OSError:
        return ""
    if not txt:
        return ""
    # a seção "QUANDO PODE CONCLUIR" (situações que o gerente aceita) vai separada, com instrução própria
    m = SECAO_PODE_CONCLUIR.search(txt)
    neg, pos = (txt[:m.start()].strip(), txt[m.end():].strip()) if m else (txt, "")
    excecao = (" Regras de confirmação ou de manter em monitoramento NÃO se aplicam ao assunto que segue em outro "
               "atendimento com o número informado." if cfg is not None and texto_continuidade(cfg) else "")
    out = ""
    if neg:
        out += ("\nREGRAS DE REVISÃO DA EMPRESA (aprendidas das devoluções reais e aprovadas pelo gerente). "
                "Se o atendimento se enquadrar em alguma, NÃO conclua e cite a regra em 'pendencias'." + excecao + "\n"
                + neg + "\n")
    if pos:
        out += ("\nSITUAÇÕES EM QUE O GERENTE ACEITA CONCLUIR (aprendidas das revisões dele): nelas o atendimento PODE "
                "ser concluído; não as trate como pendência, desde que não haja outra pendência concreta.\n" + pos + "\n")
    return out


def postura_texto(cfg, lite):
    """[agente] postura = conservadora (padrão) | calibrada.
    Conservadora: na dúvida não conclui (o revisor humano às vezes conclui com pendência; a IA não deve copiar isso).
    Calibrada: usa a taxa real de conclusão do ERP para negar menos."""
    if cfg.get("agente", "postura", fallback="conservadora").strip().lower() != "calibrada":
        return POSTURA_CONSERVADORA
    try:
        tot, conc = lite.execute("SELECT COUNT(*), SUM(desfecho='concluido') FROM historico_revisoes").fetchone()
    except sqlite3.OperationalError:
        tot = 0
    taxa = f"Na empresa, {conc / tot:.0%} dos pedidos de revisão são concluídos pelo revisor humano. " if tot else ""
    return (taxa + "Não negue por excesso de cautela: a falta de confirmação explícita do cliente NÃO impede concluir "
            "quando o analista entregou a solução ou a orientação e o cliente não trouxe nova demanda. "
            "Negue (pode_concluir=false) somente quando houver uma pendência CONCRETA, que você deve citar em "
            "'pendencias' (algo prometido e não feito, pergunta do cliente sem resposta, problema ainda ocorrendo, "
            "pendência em imagem, áudio ou ficha).")


def distribuicao_cqs(cfg, lite):
    """Frequência real dos CQS lançados na conclusão (histórico do ERP, só códigos permitidos)."""
    try:
        rows = lite.execute("SELECT cqs FROM historico_revisoes WHERE desfecho='concluido' AND COALESCE(cqs,'')<>''").fetchall()
        ini = lite.execute("SELECT MIN(data) FROM historico_revisoes WHERE COALESCE(cqs,'')<>''").fetchone()[0]
    except sqlite3.OperationalError:
        return ""
    perm = codigos_cqs(cfg, "principal")
    freq = {}
    for (t,) in rows:
        for c in t.split(","):
            if c and (not perm or c in perm):
                freq[c] = freq.get(c, 0) + 1
    tot = sum(freq.values())
    if tot < 20:
        return ""
    lista = ", ".join(f"{c} ({n / tot:.0%})" for c, n in sorted(freq.items(), key=lambda x: -x[1]))
    return (f"Frequência REAL dos CQS principais lançados pelo revisor nas conclusões desde {str(ini)[:10]}: {lista}. "
            "Use como referência: só fuja dos mais comuns quando o caso claramente pedir outro tipo.")


SYSTEM = """Você avalia atendimentos de suporte de um ERP para varejo/distribuição.
Leia o histórico e decida se o atendimento já pode ser CONCLUÍDO.
O atendimento chegou até você porque sua última iteração é do tipo "enviado para revisão"
(código 26, "Revisão da conclusão"): o analista pediu para revisar se ele pode ser concluído.
Nessa última iteração o analista costuma escrever o RESUMO DA CONCLUSÃO (o que o cliente pediu,
o que foi feito e o resultado): leia-a junto com o resto do histórico; o resultado registrado
nela vale como qualquer outro registro (veja CONTA COMO CONFIRMAÇÃO).

Critérios para concluir (configuráveis):
{criterio}
{confirmacao}{contato}
QUALIDADE DO REGISTRO (o revisor devolve quando o registro não se sustenta sozinho):
{criterio_registro}
{regras_revisao}
{postura}

REGRA PRINCIPAL - PENDÊNCIAS EM QUALQUER FONTE IMPEDEM A CONCLUSÃO:
analise TODAS as fontes do atendimento, não só o texto das iterações:
  1) texto das iterações;
  2) imagens/prints (trechos "[Imagem N ...]");
  3) áudios de WhatsApp (trechos "[Áudio N ...]" e a seção "ÁUDIOS DO ATENDIMENTO");
  4) fichas de visita (seção "FICHAS DE VISITA");
  5) RDMs vinculadas (seção "RDMs VINCULADAS A ESTE ATENDIMENTO").
Se em QUALQUER uma delas houver assunto pendente que o histórico não mostre como resolvido depois,
pode_concluir=false. Pendência pode ser explícita (alguém se comprometeu a fazer algo: "vou enviar",
"retorno amanhã", "aguardando o cliente...") ou implícita (o cliente pediu algo e não há registro de
que foi atendido; um erro mostrado num print sem solução registrada).
Liste CADA pendência em "pendencias", em linhas separadas, começando pela fonte:
"Texto: ...", "Imagem: ...", "Áudio: ...", "Ficha de visita: ...", "RDM: ...".

RDMs: quando houver a seção "RDMs VINCULADAS A ESTE ATENDIMENTO", ela traz a situação ATUAL de cada RDM no
sistema e vale mais do que o histórico diz sobre elas (o histórico pode estar desatualizado). O atendimento
só pode ser concluído quando TODAS as RDMs vinculadas estiverem Concluídas ou Canceladas: RDM cancelada NÃO
é pendência. Se alguma estiver PENDENTE, pode_concluir=false. Se todas estiverem concluídas ou canceladas,
a RDM não é motivo para não concluir (os demais critérios continuam valendo, inclusive o de abrir RDM
para falha do sistema ou solução de contorno).
{continuidade}
Se não houver nenhuma pendência, "pendencias" deve ser vazio.
Responda SOMENTE com JSON, sem texto fora dele:
{{"pode_concluir": true|false, "confianca": 0.0-1.0, "justificativa": "até 3 frases", "pendencias": "o que falta, ou vazio", "resumo": "...", "cqs": codtiporegistro ou null, "cqs_atualizacoes": [{{"id": "U1", "cqs": codtiporegistro}}], "atendimento_continuidade": número do atendimento que dará andamento ou null}}

O campo "resumo" é o texto que o analista vai registrar no atendimento, em português, objetivo, até 6 frases:
- Se pode concluir: o que o cliente solicitou, o que foi feito e o resultado/confirmação (ou, na continuidade
  em outro atendimento, o que foi feito aqui e em qual atendimento o assunto segue).
- Se não pode: o que foi entendido do histórico até aqui e o motivo de não concluir (o que ainda falta).
Baseie-se só no que está no histórico; não invente fatos.

Privacidade: alguns dados foram ocultados e aparecem como marcadores entre colchetes, por exemplo
[CPF], [CNPJ], [SENHA], [EMAIL], [TELEFONE], [CLIENTE], [USUARIO_12]. Não tente adivinhar o conteúdo.
Ao citar essas pessoas ou dados no resumo, use exatamente o mesmo marcador.
Trechos "[Imagem N (texto lido por OCR): ...]" são o texto extraído de prints anexados ao atendimento;
podem conter erros de leitura. Uma mensagem de erro, aviso ou solicitação visível num print, sem
solução registrada depois, é pendência. A ordem importa: erro num print seguido do registro da correção
NÃO é pendência.
Trechos "[Imagem N (conversa de WhatsApp lida por OCR): ...]" são prints da conversa com o cliente, já
separados por quem falou e na ordem da conversa: CLIENTE = mensagens do cliente (balões à esquerda);
ANALISTA = mensagens da empresa (balões à direita). O analista costuma anexar esse print como prova do
aceite. VALE como confirmação (mesmo que o contato daquela interação seja o próprio analista) a fala do
CLIENTE que RESPONDE à solução ou à resposta dada: confirma o resultado ("deu certo", "funcionou",
"resolveu", "consegui", "sim" respondendo se deu certo) ou, logo depois da solução ou da resposta
COMPLETA, concorda ou agradece ("ok", "entendi", "obrigado"). NÃO vale: "ok" ou "obrigado" que só
responde a um aviso ("o analista vai verificar", "aguarde"), a uma informação parcial ou a um pedido de
teste ainda sem resultado ("tenta colocar", "vê se vai"); "aguardando"; pergunta do ANALISTA ("deu
certo?") sem resposta do CLIENTE; print só com falas do ANALISTA. Pedido, problema ou reação negativa do
CLIENTE no fim da conversa, sem resposta depois, é pendência. A fala do cliente não muda o CQS: conta o
que o analista fez (agradecer ou dizer que deu certo depois da orientação NÃO é ter resolvido sozinho).

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

CQS SECUNDÁRIO DE ATUALIZAÇÃO: quando houver a seção "REGISTROS DE ATUALIZAÇÃO" (registros com os dados
obrigatórios: atualização completa/parcial, data, analista e modalidade remota/presencial, ex.: "Atualização:
Completa - Data: 14/10/2026 - Analista: X - Remoto"), cada registro gera um CQS SECUNDÁRIO para o
analista daquele registro, além do CQS principal do atendimento. Para cada registro (U1, U2...), escolha
na lista de CQS o código que corresponde àquela atualização, de acordo com a modalidade (remota ou
presencial/in loco) e o tipo (completa ou parcial), e devolva em "cqs_atualizacoes". O CQS principal continua sendo
o do atendimento (o que foi feito para o cliente), não o da atualização. Se não houver registros,
devolva "cqs_atualizacoes": []. Se pode_concluir=false, devolva "cqs_atualizacoes": [].
O registro de atualização (com esses dados) É a documentação da atualização: por si só não é pendência, não precisa
de mais detalhe nem de confirmação do cliente. Os DEMAIS assuntos do atendimento seguem os critérios normais.

{exemplos_bloco}"""


def codigos_cqs(cfg, opcao):
    """Conjunto de códigos permitidos ([cqs] principal / secundario no config.ini). Vazio = todos."""
    v = cfg.get("cqs", opcao, fallback="").strip()
    if not v and opcao == "secundario":          # sem lista própria: usa a mesma do principal
        v = cfg.get("cqs", "principal", fallback="").strip()
    return {c.strip() for c in v.split(",") if c.strip()} or None


def versao_cqs(cfg, catalogo):
    """Assinatura do catálogo de CQS: muda se a tabela mudar OU se as listas [cqs] do config.ini mudarem."""
    if not catalogo:
        return None
    _assin = json.dumps([[str(c.get(k)) for k in ("codtiporegistro", "nometiporegistro", "descricaotiporegistro",
                                                   "pontos")]
                         for c in sorted(catalogo, key=lambda c: str(c["codtiporegistro"]))])
    _assin += "|P:" + ",".join(sorted(codigos_cqs(cfg, "principal") or [])) \
              + "|S:" + ",".join(sorted(codigos_cqs(cfg, "secundario") or []))
    return hashlib.sha1(_assin.encode("utf-8")).hexdigest()[:12]


def codigos_vigentes(cfg, catalogo):
    """Códigos de CQS que existem no catálogo atual e podem ser usados (principal ou secundário)."""
    perm = (codigos_cqs(cfg, "principal") or set()) | (codigos_cqs(cfg, "secundario") or set())
    return {str(c["codtiporegistro"]) for c in catalogo if not perm or str(c["codtiporegistro"]) in perm}


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


def indice_aprendizado(lite, max_hist=5000):
    """Índice de similaridade com tudo que serve de exemplo: revisões do painel ("av") e histórico do ERP ("h")."""
    docs = []
    for id_, assunto, resumo, just in lite.execute(
            "SELECT id, assunto, resumo, justificativa FROM avaliacoes WHERE id IN "
            "(SELECT MAX(id) FROM avaliacoes GROUP BY atendimento_id) "
            "AND (status IN ('aceita','rejeitada') OR status_cqs IN ('aceita','rejeitada'))"):
        docs.append((("av", id_), f"{assunto or ''} {assunto or ''} {resumo or ''} {just or ''}"))
    try:
        for id_, assunto, caso, motivo in lite.execute(
                "SELECT id, assunto, texto_caso, motivo FROM historico_revisoes ORDER BY data DESC LIMIT ?", (max_hist,)):
            docs.append((("h", id_), f"{assunto or ''} {assunto or ''} {caso or ''} {motivo or ''}"))
    except sqlite3.OperationalError:
        pass   # tabela ainda não existe
    return similares.Indice(docs)


def texto_consulta(atendimento, inter, max_chars=3000):
    """Texto do atendimento atual usado para achar casos parecidos (assunto + últimos registros)."""
    ult = [limpar(r.get("texto")) for r in inter
           if not re.search(r"26|revis", str(r.get("tipo", "")), re.I)][-4:]
    return (f"{atendimento.get('assunto') or ''} {atendimento.get('assunto') or ''} " + " ".join(ult))[-max_chars:]


def montar_exemplos(lite, cfg, prot=None, cqs_versao=None, consulta=None, indice=None, excluir_aid=None, codigos=None):
    """Exemplos de aprendizado para a IA:
    1) revisões do gerente sobre a decisão de concluir; 2) revisões do CQS; 3) decisões reais do revisor no ERP.
    Com consulta+índice, escolhe os casos MAIS PARECIDOS com o atendimento atual; só sem parecidos usa os mais
    recentes (genéricos, iguais para todos). 'codigos' = CQS vigentes no catálogo: revisão de CQS vale como exemplo
    se os códigos dela ainda existem (sem 'codigos', vale só a do mesmo catálogo, pela cqs_versao)."""
    n = cfg.getint("agente", "exemplos_por_tipo", fallback=3)
    k_sim = cfg.getint("aprendizado", "exemplos_similares", fallback=4)
    k_hist = cfg.getint("aprendizado", "exemplos_historico", fallback=4)
    if n <= 0 and k_hist <= 0:
        return ""

    def _p(cliente, *txts):
        if not prot:
            return txts
        pr = prot.para_atendimento(cliente)
        return tuple(pr.proteger(t) for t in txts)

    # no teste com casos do histórico, o próprio atendimento não pode aparecer como exemplo (seria "cola")
    _fora_av = set(r[0] for r in lite.execute("SELECT id FROM avaliacoes WHERE atendimento_id=?", (excluir_aid,))) \
        if excluir_aid else set()
    try:
        _fora_h = set(r[0] for r in lite.execute("SELECT id FROM historico_revisoes WHERE atendimento_id=?",
                                                  (excluir_aid,))) if excluir_aid else set()
    except sqlite3.OperationalError:
        _fora_h = set()

    def _parecidos(fonte, ids_validos, k):
        ids_validos = set(ids_validos) - (_fora_av if fonte == "av" else _fora_h)
        if not (indice and consulta and k > 0 and ids_validos):
            return []
        return [c[1] for _, c in indice.busca(consulta, k, lambda c: c[0] == fonte and c[1] in ids_validos)]

    def _ids(sql, *par):
        fora = _fora_h if "historico_revisoes" in sql else _fora_av
        return [r[0] for r in lite.execute(sql, par) if r[0] not in fora]

    ultimos = "id IN (SELECT MAX(id) FROM avaliacoes GROUP BY atendimento_id)"
    blocos = []
    # 1) análise (concluir sim/não)
    if n > 0:
        validos = set(_ids(f"SELECT id FROM avaliacoes WHERE {ultimos} AND status IN ('aceita','rejeitada')"))
        escolhidos = _parecidos("av", validos, k_sim)
        if not escolhidos:   # sem parecidos: as revisões mais recentes
            escolhidos = (_ids("SELECT id FROM avaliacoes WHERE status=? ORDER BY COALESCE(decidido_em, avaliado_em) "
                               "DESC, id DESC LIMIT ?", "rejeitada", n)
                          + _ids("SELECT id FROM avaliacoes WHERE status=? ORDER BY COALESCE(decidido_em, avaliado_em) "
                                 "DESC, id DESC LIMIT ?", "aceita", n))
        linhas = []
        for id_ in escolhidos:
            r = lite.execute("SELECT assunto, pode_concluir, cqs_cod, cqs_nome, resumo, motivo, cliente, cqs_versao, "
                             "status_cqs, status, motivo_cat FROM avaliacoes WHERE id=?", (id_,)).fetchone()
            if not r:
                continue
            assunto, pode, cqs_cod, cqs_nome, resumo, motivo, cliente, versao, st_cqs, status, cats = r
            motivo = motivos.para_exemplo(cats, motivo)   # categorias marcadas no painel + detalhe livre
            rotulo = "DISCORDOU da decisão" if status == "rejeitada" else "CONCORDOU com a decisão"
            # revisão antiga (antes da separação) julgava tudo junto: mostra o CQS para dar contexto
            cqs = (f" (CQS {cqs_cod} - {cqs_nome})" if cqs_cod and versao == cqs_versao and st_cqs is None
                   and status == "rejeitada" else "")
            assunto, resumo, motivo = _p(cliente, assunto, resumo, motivo)
            linhas.append(
                f'- Assunto "{limpar(assunto)[:80]}" | IA respondeu: {"Sim" if pode else "Não"}{cqs} | '
                f'O analista {rotulo}. Motivo: {limpar(motivo) or "(sem motivo informado)"} | '
                f'Resumo da IA: {limpar(resumo)[:300]}')
        if linhas:
            blocos.append(
                "Revisões do analista humano sobre a DECISÃO DE CONCLUIR (sim/não) em avaliações suas anteriores"
                + (", escolhidas por serem parecidas com este atendimento" if consulta and indice else "")
                + ". Onde ele DISCORDOU, evite repetir o erro; onde CONCORDOU, mantenha o critério.\n" + "\n".join(linhas))
    # 2) CQS (só do catálogo atual)
    if n > 0:
        def _vale(cod, certo, versao):
            if codigos is not None:   # os códigos da revisão ainda existem no catálogo
                return str(cod) in codigos and (not certo or str(certo) in codigos)
            return cqs_versao is None or versao == cqs_versao
        validos = {r[0] for r in lite.execute(f"SELECT id, cqs_cod, cqs_correto, cqs_versao FROM avaliacoes WHERE "
                                              f"{ultimos} AND status_cqs IN ('aceita','rejeitada')")
                   if r[0] not in _fora_av and _vale(r[1], r[2], r[3])}
        escolhidos = _parecidos("av", validos, k_sim)
        if not escolhidos:   # sem parecidos: as revisões mais recentes
            _rec = lambda st: [r[0] for r in lite.execute(
                "SELECT id FROM avaliacoes WHERE status_cqs=? ORDER BY COALESCE(decidido_em, avaliado_em) DESC, id DESC",
                (st,)) if r[0] in validos][:n]
            escolhidos = _rec("rejeitada") + _rec("aceita")
        linhas = []
        for id_ in escolhidos:
            r = lite.execute("SELECT assunto, cqs_cod, cqs_nome, cqs_correto, motivo_cqs, resumo, cliente, status_cqs "
                             "FROM avaliacoes WHERE id=?", (id_,)).fetchone()
            if not r:
                continue
            assunto, cod, nome, certo, motivo, resumo, cliente, st = r
            assunto, motivo, resumo = _p(cliente, assunto, motivo, resumo)
            extra = ""
            if st == "rejeitada":
                extra = (f" O correto era CQS {certo}." if certo else "") + (f" Motivo: {limpar(motivo)}" if motivo else "")
            linhas.append(f'- Assunto "{limpar(assunto)[:80]}" | IA sugeriu CQS {cod} - {nome} | '
                          f'O analista disse: {"ERRADO" if st == "rejeitada" else "CORRETO"}.{extra} | '
                          f'Resumo da IA: {limpar(resumo)[:200]}')
        if linhas:
            blocos.append(
                "Revisões do analista humano sobre o CQS que você sugeriu (avaliado separado da decisão de concluir). "
                "Use-as para escolher o CQS: repita os acertos e corrija os erros.\n" + "\n".join(linhas))
    # 3) decisões reais do revisor no ERP (histórico)
    if k_hist > 0:
        try:
            validos = set(_ids("SELECT id FROM historico_revisoes"))
        except sqlite3.OperationalError:
            validos = set()
        # a base é desbalanceada (quase tudo é concluído): busca parecidos separando os dois desfechos
        dev = set(_ids("SELECT id FROM historico_revisoes WHERE desfecho='devolvido'")) if validos else set()
        _k_conc = max(1, k_hist // 3)          # concluídos pesam menos: o revisor às vezes conclui com pendência
        escolhidos = _parecidos("h", dev, k_hist - _k_conc) + _parecidos("h", validos - dev, _k_conc)
        if not escolhidos and validos:
            escolhidos = (_ids("SELECT id FROM historico_revisoes WHERE desfecho='devolvido' ORDER BY data DESC LIMIT ?",
                               (k_hist + 1) // 2)
                          + _ids("SELECT id FROM historico_revisoes WHERE desfecho='concluido' ORDER BY data DESC LIMIT ?",
                                 k_hist // 2))
        linhas = []
        for id_ in escolhidos:
            r = lite.execute("SELECT assunto, cliente, texto_caso, desfecho, motivo, cqs FROM historico_revisoes WHERE id=?",
                             (id_,)).fetchone()
            if not r:
                continue
            assunto, cliente, caso, desf, motivo, cqs = r
            assunto, caso, motivo = _p(cliente, assunto, caso, motivo)
            if desf == "concluido":
                res = "concluído pelo revisor" + (f", CQS lançado: {cqs}" if cqs else "")
            else:
                res = f'DEVOLVIDO ao analista. O revisor escreveu: "{limpar(motivo)[:300]}"'
            linhas.append(f'- Assunto "{limpar(assunto)[:80]}" | Último registro antes da revisão: '
                          f'"{limpar(caso)[-350:]}" | Desfecho real: {res}')
        if linhas:
            blocos.append(
                "Decisões REAIS do revisor humano no ERP em pedidos de revisão anteriores"
                + (" parecidos com este" if consulta and indice else "")
                + ". Os DEVOLVIDOS mostram o que a empresa considera pendência: use como critério. Os CONCLUÍDOS "
                "servem de referência para o CQS, mas o revisor às vezes concluiu com pendência: eles NÃO autorizam "
                "ignorar uma pendência que você encontrar, nem concluir sem confirmação do cliente (o revisor manual "
                "costumava deixar isso passar; a regra é exigir).\n" + "\n".join(linhas))
    return "\n\n".join(blocos)


def _usuario_padrao(cfg):
    """Sem --usuario: usa [agente] usuario_servico; senão o último que logou no painel;
    senão, se só um usuário tiver chave cadastrada, esse."""
    u = cfg.get("agente", "usuario_servico", fallback="").strip()
    if u:
        return u
    try:
        u = (BASE / "ultimo_usuario.txt").read_text(encoding="utf-8").strip()
        if u and cripto.carregar(cfg, u):
            return u
    except Exception:
        pass
    cadastrados = cfg.options(cripto.SECAO) if cfg.has_section(cripto.SECAO) else []
    return cadastrados[0] if len(cadastrados) == 1 else None


def criar_client(cfg, usuario=None):
    """Credencial da IA: chave do usuário cadastrada no painel (cifrada no config.ini).
    Usuário: --usuario > usuario_servico > último login no painel > único cadastrado.
    Só se nada disso existir, cai no modo antigo (variável de ambiente)."""
    usuario = usuario or _usuario_padrao(cfg)
    if usuario:
        cred = cripto.carregar(cfg, usuario)
        if not cred:
            raise RuntimeError(f"Usuário '{usuario}' não tem chave de IA cadastrada no painel")
        prov, modelo, chave = cred["provedor"], cred["modelo"], cred["chave"]
        log.info("Usando a chave de IA de '%s' (%s / %s)", usuario, prov, modelo)
    else:
        prov = cfg["agente"].get("provider", "openai").strip().lower()
        modelo = cfg["agente"]["modelo"]
        var = cfg["agente"].get("api_key_env", "OPENAI_API_KEY")
        chave = os.environ.get(var)
        if not chave:
            cad = cfg.options(cripto.SECAO) if cfg.has_section(cripto.SECAO) else []
            raise RuntimeError(
                "Não sei qual chave de IA usar. Rode com --usuario NOME"
                + (f" (cadastrados: {', '.join(cad)})" if cad else " (cadastre a chave no painel)")
                + f", ou preencha usuario_servico no config.ini, ou defina a variável {var}.")
    cfg["agente"]["provider"], cfg["agente"]["modelo"] = prov, modelo  # usado no ciclo e gravado no painel
    cfg["agente"]["usuario_execucao"] = usuario or ""
    return llm.criar_client(prov, chave)


def bloco_cqs_completo(cfg, catalogo=()):
    """Catálogo de CQS permitido + regras da empresa para escolher o código ([cqs] regras)."""
    _bloco = montar_bloco_cqs(catalogo, codigos_cqs(cfg, "principal"), codigos_cqs(cfg, "secundario"))
    _regras = cfg.get("cqs", "regras", fallback="").strip()
    if _bloco and _regras:
        _bloco += "\n\nREGRAS DA EMPRESA PARA ESCOLHER O CQS (têm prioridade sobre as descrições):\n" + _regras
    return _bloco


def montar_system(cfg, catalogo=(), exemplos=""):
    """Prompt de sistema da avaliação. Tudo antes dos exemplos é igual em todas as chamadas do ciclo
    (o provedor aproveita do cache); os exemplos, que mudam por atendimento, ficam no fim."""
    _bloco = bloco_cqs_completo(cfg, catalogo)
    return SYSTEM.format(criterio=cfg["agente"]["criterio_conclusao"],
                         criterio_registro=cfg.get("agente", "criterio_registro", fallback="").strip()
                         or CRITERIO_REGISTRO,
                         regras_revisao=cfg.get("agente", "_regras_revisao", fallback=""),
                         confirmacao=texto_confirmacao(cfg),
                         contato=texto_contato(cfg),
                         continuidade=texto_continuidade(cfg),
                         cqs_bloco=_bloco,
                         exemplos_bloco=exemplos,
                         postura=cfg.get("agente", "_postura_texto", fallback=POSTURA_CONSERVADORA))


def temperatura(cfg):
    """[agente] temperatura: 0 = respostas estáveis (o mesmo atendimento dá a mesma decisão); vazio = padrão do
    provedor (varia a cada chamada)."""
    v = cfg.get("agente", "temperatura", fallback="0").strip()
    return float(v) if v else None


def versao_prompt(cfg, catalogo=()):
    """Identifica a versão do prompt (texto fixo + seções do código + modelo + temperatura + leitura das conversas
    nos prints), para medir o acerto por versão: mudou regra, critério, catálogo, modelo, temperatura ou o jeito de
    ler os prints, muda a versão."""
    base = "|".join([montar_system(cfg, catalogo), cfg["agente"].get("modelo", ""), str(temperatura(cfg)),
                     json.dumps(SO_ATUALIZACAO_IA, sort_keys=True, ensure_ascii=False),
                     "falas" if separar_falas(cfg) else ""])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]


def local_cliente(cfg, at):
    """'dentro' ou 'fora' da cidade da empresa ([cqs] cidade_local = Cidade/UF), pelo cadastro do cliente
    (query candidatos: cidade, uf). None sem cidade. Só essa classificação vai para a IA, nunca a cidade."""
    cidade = _norm(at.get("cidade"))
    if not cidade:
        return None
    c_ref, _, uf_ref = cfg.get("cqs", "cidade_local", fallback="Juiz de Fora/MG").partition("/")
    uf = _norm(at.get("uf"))
    return "dentro" if cidade == _norm(c_ref) and (not uf or not uf_ref.strip() or uf == _norm(uf_ref)) else "fora"


def avaliar(client, cfg, atendimento, historico, catalogo=(), exemplos="", prot=None, fichas_txt="", audios_txt="",
            atualizacoes=(), rdms_txt="", citados_txt="", so_atualizacao_txt=""):
    system = montar_system(cfg, catalogo, exemplos)
    assunto, cliente = atendimento.get("assunto", ""), atendimento.get("cliente", "")
    if prot:
        assunto, cliente = prot.proteger(assunto), prot.proteger(cliente)
    local = local_cliente(cfg, atendimento)
    cidade_ref = cfg.get("cqs", "cidade_local", fallback="Juiz de Fora/MG").partition("/")[0].strip()
    user = (
        f"Assunto: {assunto}\n"
        f"Cliente: {cliente}\n"
        + (f"Local do cliente: {local} de {cidade_ref}\n" if local else "")
        + f"\nHISTÓRICO:\n{historico}"
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
    if rdms_txt:
        user += f"\n\n{rdms_txt}"
    if citados_txt:
        user += f"\n\n{citados_txt}"
    if so_atualizacao_txt:
        user += f"\n\n{so_atualizacao_txt}"
    max_tok = cfg.getint("agente", "max_tokens_resposta", fallback=800)
    texto, tin, tout = llm.chamar(cfg["agente"]["provider"], client, cfg["agente"]["modelo"],
                                  system, user, max_tok, temperatura=temperatura(cfg))
    m = re.search(r"\{.*\}", texto, re.S)
    if not m:
        raise ValueError(f"Resposta sem JSON: {texto[:200]}")
    res = json.loads(m.group(0))
    if prot:  # devolve os nomes reais só aqui, localmente
        for k in ("resumo", "justificativa", "pendencias"):
            res[k] = prot.pseudo.reidentificar(res.get(k))
    return res, tin, tout


SYSTEM_REVISOR = """Você é o REVISOR de qualidade do suporte de um ERP. Um assistente já sugeriu CONCLUIR este
atendimento. Confira se há um motivo CONCRETO para devolvê-lo ao analista, como o revisor humano faria.

O QUE NÃO É MOTIVO PARA DEVOLVER:
- falta de passo a passo minucioso, quando o registro já permite a outro analista entender o problema e
  o que foi feito (qual ajuste, tela, configuração, consulta ou correção);
- detalhes cosméticos de redação.

O QUE É MOTIVO (devolva só se estiver claro no histórico):
1) Registro que não permite entender o que foi feito ("ajustado", "orientado", "verificado" sem dizer o quê),
   registros incoerentes entre si ou de outro atendimento, conclusão copiada que não bate com o histórico:
{criterio_registro}
2) Regras de revisão da empresa:
{regras}
3) Pendência concreta não resolvida em qualquer fonte (texto, imagens, áudios, fichas).
{confirmacao}
RDM: a seção "RDMs VINCULADAS A ESTE ATENDIMENTO" traz a situação atual das RDMs. RDM Concluída ou Cancelada
NÃO é motivo para devolver, mesmo que o histórico diga que ela estava em andamento.
Assunto que segue em OUTRO atendimento com o número informado não é motivo para devolver: o que falta continua lá.
Na dúvida, NÃO devolva: a primeira avaliação já foi conservadora, e devolver sem motivo atrasa o atendimento.
Responda SOMENTE com JSON:
{{"devolver": true|false, "certeza": 0.0-1.0, "motivos": ["motivo objetivo, como o revisor escreveria ao analista"]}}
Exemplos de motivos reais do revisor: "Detalhar o que foi feito.", "Necessário documentar com os nomes dos arquivos.",
"Abrir RDM para corrigir.", "Interação anterior não pertence a esse atendimento."
"""


def segunda_revisao(client, cfg, atendimento, historico, prot=None, fichas_txt="", audios_txt="", rdms_txt=""):
    """2ª passada só para os "Sim": checagem focada nas regras (prompt curto, sem exemplos).
    Retorna (devolver, motivos, tokens_in, tokens_out). [agente] segunda_revisao = sim|nao, modelo_revisao = (opcional)"""
    regras = cfg.get("agente", "_regras_revisao", fallback="").strip() or "(nenhuma regra extra aprovada)"
    system = SYSTEM_REVISOR.format(
        criterio_registro=cfg.get("agente", "criterio_registro", fallback="").strip() or CRITERIO_REGISTRO,
        regras=regras,
        confirmacao="(A confirmação do cliente já foi checada na primeira avaliação: NÃO use falta de confirmação como motivo.)")
    assunto = prot.proteger(atendimento.get("assunto", "")) if prot else atendimento.get("assunto", "")
    user = f"Assunto: {assunto}\n\nHISTÓRICO:\n{historico}"
    if fichas_txt:
        user += f"\n\nFICHAS DE VISITA:\n{fichas_txt}"
    if audios_txt:
        user += f"\n\nÁUDIOS:\n{audios_txt}"
    if rdms_txt:
        user += f"\n\n{rdms_txt}"
    modelo = cfg.get("agente", "modelo_revisao", fallback="").strip() or cfg["agente"]["modelo"]
    texto, tin, tout = llm.chamar(cfg["agente"]["provider"], client, modelo, system, user, 600,
                                  temperatura=temperatura(cfg))
    m = re.search(r"\{.*\}", texto, re.S)
    r = json.loads(m.group(0)) if m else {}
    motivos = [str(x).strip() for x in (r.get("motivos") or []) if str(x).strip()]
    if prot:
        motivos = [prot.pseudo.reidentificar(x) for x in motivos]
    try:
        certeza = float(r.get("certeza", 1))
    except (TypeError, ValueError):
        certeza = 1.0
    limite = cfg.getfloat("agente", "limite_revisao", fallback=0.75)
    # a confirmação do cliente é checada na 1ª avaliação; aqui, motivo que é só falta de confirmação não conta
    motivos = [x for x in motivos if not re.search(
        r"(?i)confirm\w* (expl[ií]cit\w* )?(do|de retorno do|por parte do|com o|junto ao) cliente|retorno do cliente"
        r"|valida[çc][ãa]o (expl[ií]cita )?(do|por parte do) cliente", x)]
    return bool(r.get("devolver")) and bool(motivos) and certeza >= limite, motivos, tin, tout


def aplicar_segunda_revisao(res, client, cfg, atendimento, historico, prot=None, fichas_txt="", audios_txt="",
                            rdms_txt=""):
    """Se a 1ª avaliação disse Sim (sem pendência), roda a 2ª revisão; se ela devolver, vira Não. Retorna tokens."""
    if not cfg.getboolean("agente", "segunda_revisao", fallback=True) or not res.get("pode_concluir"):
        return 0, 0
    pend = str(res.get("pendencias") or "").strip()
    if pend and not re.fullmatch(r"(?i)(nenhuma?|n/?a|-|vazio|sem pend[eê]ncias?\.?)", pend):
        return 0, 0
    devolver, motivos, tin, tout = segunda_revisao(client, cfg, atendimento, historico, prot, fichas_txt, audios_txt,
                                                   rdms_txt)
    if devolver:
        res["pode_concluir"] = False
        res["pendencias"] = "Revisão: " + " | ".join(motivos)
        res["justificativa"] = ("[Devolvido na 2ª revisão] " + "; ".join(motivos) + " — "
                                + str(res.get("justificativa") or "")).strip()
    return tin, tout


def travar(res, novas, rotulo):
    """Pendências apuradas pelo código (RDM, contato) entram em 'pendencias' e forçam "Não".
    Devolve True se a IA tinha dito "Sim"."""
    p = str(res.get("pendencias") or "").strip()
    if re.fullmatch(r"(?i)(nenhuma?|n/?a|-|vazio|sem pend[eê]ncias?\.?)", p):
        p = ""
    res["pendencias"] = "\n".join(([p] if p else []) + list(novas))
    if not res.get("pode_concluir"):
        return False
    res["pode_concluir"] = False
    res["justificativa"] = (f"[Bloqueado: {rotulo}] " + str(res.get("justificativa") or "")).strip()
    return True


_SEM_PEND = r"(?i)(nenhuma?|n/?a|-|vazio|sem pend[eê]ncias?\.?)"


def decidir(client, cfg, conn, at, inter, historico, catalogo, por_cod, exemplos="", prot=None, fichas_txt="",
            audios_txt="", atualizacoes=(), rdms_assin=None, rdm_fin=None, exclusivo=None, contato_concl=None,
            bloq_contato=False, continuidade_on=True, aid=""):
    """Decisão completa de um atendimento: IA + regras do código (só registro de atualização, continuidade, RDM,
    contato, 2ª revisão, salvaguarda de pendências) + escolha do CQS. Usada pelo ciclo e pelo testar.py, para o
    teste medir exatamente o que o agente faz. Retorna dict: res, pode, conf, cqs (linha do catálogo ou None),
    secundarios, continuidade, tin, tout."""
    tin = tout = 0
    rdms_txt = rdm.texto_ia(rdms_assin, rdm_fin)
    if exclusivo == "puro" and cfg.get("cqs", "so_atualizacao", fallback="").strip():
        # só o registro de atualização, sem texto a mais: decide sem chamar a IA (CQS pela modalidade e cidade)
        _u = atualizacoes[0] if atualizacoes else {}
        _cod = cqs_so_atualizacao(cfg, _u.get("modalidade"), local_cliente(cfg, at))
        res = {"pode_concluir": True, "confianca": 0.95, "justificativa": "", "pendencias": "",
               "resumo": resumo_atualizacao(_u), "cqs": _cod,
               "cqs_atualizacoes": [{"id": _u.get("id", "U1"), "cqs": _cod}] if _cod else []}
    else:
        citados_txt = texto_citados(conn, cfg, at, inter) if continuidade_on else ""
        res, i, o = avaliar(client, cfg, at, historico, catalogo, exemplos, prot, fichas_txt, audios_txt, atualizacoes,
                            rdms_txt, citados_txt, SO_ATUALIZACAO_IA.get(exclusivo, ""))
        tin, tout = tin + i, tout + o
    if exclusivo:
        # só registro de atualização: conclui pela regra da empresa. Falta de confirmação do cliente nunca
        # conta; com texto a mais (ex.: OBS), pendência real que a IA achou nesse texto impede
        _u = atualizacoes[0] if atualizacoes else {}
        _obs = [p.strip() for p in str(res.get("pendencias") or "").splitlines()
                if p.strip() and not re.fullmatch(_SEM_PEND, p.strip()) and not hist.so_confirmacao(p)]
        if exclusivo == "extra" and _obs:
            res["pode_concluir"], res["pendencias"] = False, "\n".join(_obs)
            res["justificativa"] = ("[Só registro de atualização, com pendência no texto a mais] "
                                    + str(res.get("justificativa") or "")).strip()
        else:
            if not res.get("pode_concluir"):
                log.info("Atend %s: só registro de atualização; a IA disse Não, concluído pela regra", aid)
                res["resumo"] = resumo_atualizacao(_u)
            res["justificativa"] = (
                "[Só registro de atualização] Atendimento aberto apenas para documentar a atualização: pela "
                "regra pode ser concluído, e o CQS da atualização vai para o analista do registro"
                + (f" ({_u['analista']})" if _u.get("analista") else "") + "."
                + (f" Observação da IA: {' | '.join(_obs)}" if _obs else ""))
            res["pode_concluir"], res["pendencias"] = True, ""
            try:   # observação da IA (ex.: ficha de visita) baixa a confiança para chamar a atenção no painel
                res["confianca"] = 0.7 if _obs else max(float(res.get("confianca") or 0), 0.95)
            except (TypeError, ValueError):
                res["confianca"] = 0.95
    # continuidade em outro atendimento: o número que a IA indicou precisa valer
    continuidade, motivo_cont = (validar_continuidade(conn, cfg, res, at, inter)
                                 if continuidade_on and not exclusivo else (None, None))
    if motivo_cont:
        if travar(res, [f"Texto: o atendimento {continuidade} indicado para dar andamento {motivo_cont}"],
                  "continuidade não confirmada"):
            log.warning("Atend %s: continuidade no atendimento %s não vale (%s); marcado como Não",
                        aid, continuidade, motivo_cont)
        continuidade = None
    # travas do código (a 2ª revisão nem roda): RDM vinculada que não está concluída nem cancelada
    # (vale mesmo com continuidade), e interação de conclusão cujo contato não é o cliente
    # (dispensada quando o assunto segue em outro atendimento com o número informado)
    pend_rdm = rdm.pendentes(rdms_assin, rdm_fin)
    if pend_rdm:
        _p = str(res.get("pendencias") or "")
        if travar(res, [f"RDM: {x}" for x in pend_rdm if x.split()[1] not in _p], "RDM vinculada pendente"):
            log.warning("Atend %s: IA disse concluir mas há RDM vinculada pendente; marcado como Não", aid)
    if bloq_contato and not continuidade:
        _p = str(res.get("pendencias") or "").lower()
        if travar(res, [] if "contato:" in _p else [pendencia_contato(contato_concl)],
                  "conclusão sem contato com o cliente"):
            log.warning("Atend %s: IA disse concluir mas a interação de conclusão não foi com o cliente "
                        "(contato: %s); marcado como Não", aid, contato_concl)
    if not exclusivo:   # só registro de atualização: a regra já decidiu, não tem o que revisar
        i, o = aplicar_segunda_revisao(res, client, cfg, at, historico, prot, fichas_txt, audios_txt, rdms_txt)
        tin, tout = tin + i, tout + o
    conf = float(res.get("confianca", 0))
    pode = bool(res.get("pode_concluir"))
    pend = str(res.get("pendencias") or "").strip()
    if pode and pend and not re.fullmatch(_SEM_PEND, pend):
        # trava: se a IA listou pendência, não pode concluir (mesmo que ela tenha dito "Sim")
        log.warning("Atend %s: IA disse concluir mas listou pendências; marcado como Não", aid)
        pode = False
        res["justificativa"] = ("[Bloqueado: há pendências listadas] " + str(res.get("justificativa") or "")).strip()
    cqs, secundarios = escolher_cqs(cfg, res, por_cod, pode, exclusivo, atualizacoes, aid)
    if exclusivo and pode and not cqs:
        _mod = atualizacoes[0]["modalidade"] if atualizacoes else ""
        res["justificativa"] += " CQS não definido: o revisor escolhe o da atualização" + (f" ({_mod})." if _mod else ".")
    return {"res": res, "pode": pode, "conf": conf, "cqs": cqs, "secundarios": secundarios,
            "continuidade": continuidade, "tin": tin, "tout": tout}


SYSTEM_CQS = """Você escolhe o CQS (pontuação de qualidade) de um atendimento de suporte de um ERP que JÁ foi avaliado
como pronto para concluir: não reavalie a conclusão. Leia o histórico e o resumo e escolha o código pelo que o
analista DE FATO fez.

{cqs_bloco}

CQS SECUNDÁRIO DE ATUALIZAÇÃO: para cada registro da seção "REGISTROS DE ATUALIZAÇÃO" (U1, U2...), escolha o código
que corresponde àquela atualização (modalidade e tipo) e devolva em "cqs_atualizacoes"; sem registros, devolva [].
Privacidade: dados ocultados aparecem como [CPF], [CLIENTE], [USUARIO_12]...; não tente adivinhar.
Responda SOMENTE com JSON: {{"cqs": codtiporegistro ou null, "cqs_atualizacoes": [{{"id": "U1", "cqs": codtiporegistro}}]}}"""


def reclassificar_cqs(client, cfg, at, historico, catalogo, atualizacoes=(), prot=None, resumo=""):
    """Só o CQS, com prompt curto (catálogo + regras + histórico), quando a análise não precisa ser refeita
    (o catálogo de CQS mudou, ou "Sim" antigo sem CQS). Retorna (resposta, tokens_entrada, tokens_saida)."""
    _p = prot.proteger if prot else (lambda x: x)
    local = local_cliente(cfg, at)
    cidade_ref = cfg.get("cqs", "cidade_local", fallback="Juiz de Fora/MG").partition("/")[0].strip()
    user = (f"Assunto: {_p(at.get('assunto') or '')}\n"
            + (f"Local do cliente: {local} de {cidade_ref}\n" if local else "")
            + f"\nHISTÓRICO:\n{historico}"
            + (f"\n\nRESUMO DA AVALIAÇÃO:\n{_p(resumo)}" if resumo else ""))
    if atualizacoes:
        user += "\n\nREGISTROS DE ATUALIZAÇÃO:\n" + "\n".join(
            f"{u['id']}: tipo={u['tipo'] or '?'} | data={u['data'] or '?'} | analista={_p(u['analista']) or '?'}"
            f" | modalidade={u['modalidade'] or 'não informada'}" for u in atualizacoes)
    texto, tin, tout = llm.chamar(cfg["agente"]["provider"], client, cfg["agente"]["modelo"],
                                  SYSTEM_CQS.format(cqs_bloco=bloco_cqs_completo(cfg, catalogo)), user, 300,
                                  temperatura=temperatura(cfg))
    m = re.search(r"\{.*\}", texto, re.S)
    if not m:
        raise ValueError(f"Resposta sem JSON: {texto[:200]}")
    return json.loads(m.group(0)), tin, tout


def refazer_so_cqs(lite, cfg, client, at, aid, historico, catalogo, por_cod, atualizacoes, prot, exclusivo,
                   cqs_versao, versao, inicio):
    """Nova linha copiando a última avaliação (a análise não muda) só com o CQS refeito. A revisão do CQS que o
    gerente já tenha feito só é mantida se o código continuar o mesmo. Retorna (tokens_entrada, tokens_saida)."""
    cur = lite.execute("SELECT * FROM avaliacoes WHERE atendimento_id=? ORDER BY id DESC LIMIT 1", (aid,))
    ant = dict(zip([d[0] for d in cur.description], cur.fetchone()))
    r, tin, tout = reclassificar_cqs(client, cfg, at, historico, catalogo, atualizacoes, prot, ant.get("resumo") or "")
    cqs, secundarios = escolher_cqs(cfg, r, por_cod, True, exclusivo, atualizacoes, aid)
    novo_cod, cod_ant = (str(cqs["codtiporegistro"]) if cqs else None), ant.get("cqs_cod")
    mesmo = novo_cod == cod_ant
    ant.pop("id", None)
    ant.update({
        "cliente": at.get("cliente"), "assunto": at.get("assunto"), "analista": at.get("analista"),
        "cqs_cod": novo_cod, "cqs_nome": limpar(cqs.get("nometiporegistro")) if cqs else None,
        "cqs_descricao": limpar(cqs.get("descricaotiporegistro")) if cqs else None,
        "cqs_pontos": str(cqs.get("pontos")) if cqs else None, "cqs_ok": 1,
        "cqs_secundarios": json.dumps(secundarios, ensure_ascii=False) if secundarios else None, "cqs_sec_ok": 1,
        "cqs_versao": cqs_versao, "versao_prompt": versao, "ciclo_inicio": inicio,
        "avaliado_em": datetime.now().isoformat(timespec="seconds"), "modelo": cfg["agente"]["modelo"],
    })
    if not mesmo:
        ant.update({"status_cqs": None, "cqs_correto": None, "motivo_cqs": None})
    cols = list(ant)
    lite.execute(f"INSERT INTO avaliacoes({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 [ant[c] for c in cols])
    lite.commit()
    log.info("Atend %s: só o CQS foi refeito (análise mantida): %s -> %s", aid, cod_ant or "sem CQS",
             novo_cod or "sem CQS")
    return tin, tout


def escolher_cqs(cfg, res, por_cod, pode, exclusivo=None, atualizacoes=(), aid=""):
    """CQS principal (linha do catálogo ou None) e secundários a partir da resposta da IA, com as travas: código
    permitido, existente no catálogo e que combine com a modalidade da atualização. Só registro de atualização:
    o CQS dela é o principal, sem secundário repetido. Retorna (cqs, secundarios)."""
    _pri, _sec = codigos_cqs(cfg, "principal"), codigos_cqs(cfg, "secundario")
    _cands = [res.get("cqs")]
    if exclusivo:   # o CQS da atualização é o do atendimento (vem antes do "cqs" da IA)
        _cands = [x.get("cqs") for x in (res.get("cqs_atualizacoes") or []) if isinstance(x, dict)][:1] + _cands
        _cands = list(dict.fromkeys(str(c_) for c_ in _cands if c_ is not None))   # sem repetir
    _mod = (atualizacoes[0]["modalidade"] if exclusivo and atualizacoes else "")
    cqs = None
    for _c in (_cands if pode else []):
        _c = por_cod.get(str(_c)) if _c is not None else None
        if _c and _pri is not None and str(_c["codtiporegistro"]) not in _pri:
            log.warning("Atend %s: IA sugeriu CQS %s, fora dos permitidos como principal; descartado",
                        aid, _c["codtiporegistro"])
        elif _c and not cqs_combina_modalidade(_c, _mod):
            log.warning("Atend %s: IA sugeriu CQS %s para atualização %s; descartado (o revisor escolhe)",
                        aid, _c["codtiporegistro"], _mod)
        elif _c:
            cqs = _c
            break
    # CQS secundários: o analista vem da extração local (nome real), o código vem da IA.
    # Só registro de atualização: o CQS dela já é o principal, sem secundário repetido
    secundarios = []
    if pode and atualizacoes and not exclusivo:
        escolhas = {str(x.get("id")): x.get("cqs") for x in (res.get("cqs_atualizacoes") or []) if isinstance(x, dict)}
        for u in atualizacoes:
            c2 = por_cod.get(str(escolhas.get(u["id"])))
            if c2 and _sec is not None and str(c2["codtiporegistro"]) not in _sec:
                c2 = None   # fora dos permitidos como secundário
            if c2 and not cqs_combina_modalidade(c2, u["modalidade"]):
                log.warning("Atend %s: IA sugeriu CQS %s para atualização %s; descartado",
                            aid, c2["codtiporegistro"], u["modalidade"])
                c2 = None
            secundarios.append({**{k: u[k] for k in ("analista", "data", "tipo", "modalidade")},
                                "cqs": str(c2["codtiporegistro"]) if c2 else None,
                                "cqs_nome": limpar(c2.get("nometiporegistro")) if c2 else None,
                                "cqs_pontos": str(c2.get("pontos")) if c2 else None})
        if any(s["cqs"] is None for s in secundarios):
            log.warning("Atend %s: IA não classificou todos os registros de atualização", aid)
    return cqs, secundarios


# ---------- ciclo ----------
def ciclo(cfg, lite, client, manual=False):
    inicio = datetime.now().isoformat(timespec="seconds")
    avaliados = erros = tin = tout = 0
    _cache0, _versao = llm.USO["cache"], None
    max_chars = cfg.getint("agente", "max_chars_historico", fallback=12000)
    max_ciclo = cfg.getint("agente", "max_por_ciclo", fallback=20)
    log.info("Limite deste ciclo: %d avaliações", max_ciclo)

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
        cqs_versao = versao_cqs(cfg, catalogo)
        if catalogo:
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
        # aprendizado: histórico real de revisões do ERP (incremental) + índice de casos parecidos
        if cfg.getboolean("aprendizado", "importar_historico", fallback=True):
            try:
                _validos = (codigos_cqs(cfg, "principal") or set()) | (codigos_cqs(cfg, "secundario") or set())
                hist.importar(cfg, conn, lite, consultar, limpar, _validos or None)
            except Exception as e:
                log.warning("Não consegui importar o histórico de revisões do ERP: %s", e)
        indice = indice_aprendizado(lite) if cfg.getboolean("aprendizado", "usar_similares", fallback=True) else None
        cfg["agente"]["_postura_texto"] = postura_texto(cfg, lite)
        cfg["agente"]["_regras_revisao"] = regras_revisao(cfg)
        _versao = versao_prompt(cfg, catalogo)   # para medir o acerto por versão do prompt
        _vigentes = codigos_vigentes(cfg, catalogo) if catalogo else None   # CQS que ainda existem (exemplos)
        _dist_cqs = distribuicao_cqs(cfg, lite)
        _rdm_on = cfg.getboolean("rdm", "verificar", fallback=True) and cfg.has_option("queries", "rdms")
        _rdm_fin = rdm.finalizadas(cfg)
        _internos = {_norm(u.get("nomeusuario")) for u in usuarios if u.get("nomeusuario")}
        _exigir_contato = cfg.getboolean("agente", "exigir_contato_cliente", fallback=True)
        _continuidade_on = cfg.getboolean("agente", "continuidade_outro_atendimento", fallback=True)
        _so_atual_on = cfg.getboolean("agente", "concluir_so_atualizacao", fallback=True)
        _ult_erro, _seguidos = None, 0

        for at in candidatos:
            if avaliados >= max_ciclo:
                break
            # chave composta: empresa + número + desdobramento (mesmo número pode ter vários desdobramentos)
            chave = [c.strip().lower() for c in cfg["queries"]["chave_colunas"].split(",")]
            aid = "-".join(str(at[c]) for c in chave)
            try:
                inter = consultar(conn, cfg["queries"]["interacoes"], tuple(at[c] for c in chave))
                # RDMs vinculadas: "num/desd:situação;..." (None = não deu para consultar: não trava nem reavalia)
                rdms_assin = None
                if _rdm_on:
                    try:
                        rdms_assin = rdm.assinatura(consultar(conn, cfg["queries"]["rdms"],
                                                              tuple(at[c] for c in chave)))
                    except Exception as e:
                        log.warning("Atend %s: não consegui consultar as RDMs vinculadas: %s", aid, e)
                # contato da interação de conclusão: o próprio analista ou usuário interno não comprova falar com o cliente
                # atendimento aberto só para documentar uma atualização: conclui pela regra (contato não importa)
                exclusivo = classificar_so_atualizacao(inter) if _so_atual_on else None   # 'puro' | 'extra' | None
                contato_concl = contato_conclusao(inter, at.get("analista"), _internos)
                bloq_contato = _exigir_contato and contato_concl not in (None, "cliente") and not exclusivo
                # só reavalia se chegou interação nova desde a última avaliação
                ult = lite.execute(
                    "SELECT qtd_interacoes, pode_concluir, cqs_ok, COALESCE(qtd_fichas,0), COALESCE(qtd_audios,0), "
                    "cqs_versao, CASE WHEN status='pendente' AND COALESCE(status_cqs,'pendente')='pendente' "
                    "THEN 'pendente' ELSE 'revisado' END, COALESCE(cqs_sec_ok,0), rdms, continuidade, so_atualizacao "
                    "FROM avaliacoes WHERE atendimento_id=? ORDER BY id DESC LIMIT 1", (aid,)
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
                # RDM vinculada mudou de situação (ou foi vinculada/desvinculada) desde a última avaliação;
                # avaliação de antes deste recurso (rdms NULL) só é refeita se ainda estiver pendente
                mudou_rdm = bool(rdms_assin is not None and ult and rdms_assin != (ult[8] or "")
                                 and (ult[8] is not None or ult[6] == "pendente"))
                # "Sim" ainda pendente cuja conclusão não foi com o cliente (avaliado antes desta regra): refaz
                refazer_contato = bool(bloq_contato and ult and ult[1] == 1 and ult[6] == "pendente")
                # "Não" ainda pendente, avaliado antes da regra de continuidade, que cita outro atendimento: refaz
                refazer_cont = bool(_continuidade_on and ult and ult[1] == 0 and ult[6] == "pendente" and ult[9] is None
                                    and citados_outros(inter, at.get("numatendimento")))
                # só registro de atualização, "Não" ainda pendente e avaliado antes desta regra: refaz uma vez
                refazer_atual = bool(exclusivo and ult and ult[1] == 0 and ult[6] == "pendente" and ult[10] is None)
                if (ult and ult[0] == len(inter) and ult[3] == len(arquivos) and ult[4] == len(audios_pasta)
                        and not precisa_cqs and not mudou_rdm and not refazer_contato and not refazer_cont
                        and not refazer_atual):
                    # sem interação nova: não reavalia, mas atualiza cliente/analista/assunto
                    # (e guarda a situação das RDMs nas avaliações antigas, para notar quando mudar)
                    lite.execute(
                        "UPDATE avaliacoes SET cliente=?, analista=?, assunto=?, rdms=COALESCE(rdms, ?) "
                        "WHERE atendimento_id=?",
                        (at.get("cliente"), at.get("analista"), at.get("assunto"), rdms_assin, aid),
                    )
                    lite.commit()
                    continue
                if not inter:
                    continue

                historico = montar_historico(inter, max_chars, prot, at.get("analista"), _internos)
                # print de conversa com fala do cliente anexado na conclusão: comprova que ela foi com o cliente
                if bloq_contato and print_do_cliente_na_conclusao(inter, prot, at.get("analista"), _internos):
                    bloq_contato, contato_concl = False, "cliente"
                # só o CQS precisa ser refeito (catálogo mudou, ou "Sim" antigo sem CQS): prompt curto, análise mantida
                if (ult and ult[0] == len(inter) and ult[3] == len(arquivos) and ult[4] == len(audios_pasta)
                        and precisa_cqs and exclusivo != "puro"
                        and not (mudou_rdm or refazer_contato or refazer_cont or refazer_atual)):
                    i, o = refazer_so_cqs(lite, cfg, client, at, aid, historico, catalogo, por_cod, atualizacoes, prot,
                                          exclusivo, cqs_versao, _versao, inicio)
                    tin, tout = tin + i, tout + o
                    avaliados += 1
                    _seguidos = 0
                    continue
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
                exemplos = montar_exemplos(lite, cfg, prot_base, cqs_versao, texto_consulta(at, inter), indice,
                                           codigos=_vigentes)
                if _dist_cqs:
                    exemplos = (exemplos + "\n\n" + _dist_cqs).strip()
                dec = decidir(client, cfg, conn, at, inter, historico, catalogo, por_cod, exemplos, prot,
                              "\n".join(fichas_txt), "\n".join(audios_txt), atualizacoes, rdms_assin, _rdm_fin,
                              exclusivo, contato_concl, bloq_contato, _continuidade_on, aid)
                res, pode, conf, cqs = dec["res"], dec["pode"], dec["conf"], dec["cqs"]
                secundarios, continuidade = dec["secundarios"], dec["continuidade"]
                tin, tout = tin + dec["tin"], tout + dec["tout"]
                avaliados += 1
                _seguidos = 0
                lite.execute(
                    """INSERT INTO avaliacoes(atendimento_id,cliente,assunto,analista,
                       qtd_interacoes,pode_concluir,confianca,justificativa,pendencias,
                       resumo,avaliado_em,modelo,cqs_cod,cqs_nome,cqs_descricao,cqs_pontos,cqs_ok,
                       fichas,qtd_fichas,audios,qtd_audios,cqs_versao,cqs_secundarios,cqs_sec_ok,ciclo_inicio,
                       qtd_imagens,rdms,continuidade,so_atualizacao,versao_prompt)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,1,?,?,?,?,?,?)""",
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
                     json.dumps(secundarios, ensure_ascii=False) if secundarios else None, inicio,
                     prot.imagens_lidas, rdms_assin,
                     continuidade or "",   # "" = conferido sem continuidade (NULL = avaliado antes da regra)
                     # só registro de atualização: 1 = só ele, 2 = com texto a mais, 0 = não (NULL = antes da regra)
                     {"puro": 1, "extra": 2}.get(exclusivo, 0), _versao),
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
                # o mesmo erro em atendimentos seguidos é problema geral (config, chave ou crédito da IA, rede):
                # para o ciclo em vez de repetir em todos os da fila
                _chave_erro = f"{type(e).__name__}: {str(e)[:100]}"
                _seguidos = _seguidos + 1 if _chave_erro == _ult_erro else 1
                _ult_erro = _chave_erro
                if _seguidos >= 3:
                    log.error("Mesmo erro em %d atendimentos seguidos; ciclo interrompido: %s", _seguidos, _chave_erro)
                    break
    finally:
        conn.close()

    lite.execute(
        "INSERT INTO execucoes(inicio,fim,candidatos,avaliados,erros,tokens_in,tokens_out,tokens_cache) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (inicio, datetime.now().isoformat(timespec="seconds"),
         len(candidatos), avaliados, erros, tin, tout, llm.USO["cache"] - _cache0),
    )
    lite.commit()
    log.info("Ciclo ok: %d avaliados, %d erros, %d/%d tokens (%d da entrada vieram do cache)",
             avaliados, erros, tin, tout, llm.USO["cache"] - _cache0)
    try:
        _ac = hist.texto_acerto(lite, _versao)
        if _ac:
            log.info("Acerto real (comparado com o ERP): %s", _ac)
    except Exception:
        pass

    # relatório PDF do ciclo (Modelo B)
    if avaliados and cfg.getboolean("relatorio", "gerar", fallback=True):
        try:
            from relatorio import gerar_relatorio
            pasta = cfg.get("relatorio", "pasta", fallback="relatorios")
            pasta = pasta if os.path.isabs(pasta) else str(BASE / pasta)
            pdf = gerar_relatorio(lite, inicio, pasta, cfg["agente"].get("usuario_execucao", ""),
                                  f'{llm.PROVEDORES.get(cfg["agente"].get("provider", ""), {}).get("nome", cfg["agente"].get("provider", ""))}'
                                  f' · {cfg["agente"].get("modelo", "")}',
                                  cfg.getint("relatorio", "dias_sem_confirmacao", fallback=30))
            if pdf:
                log.info("Relatório do ciclo: %s", pdf)
        except Exception:
            pdf = None
            log.exception("Falha ao gerar o relatório PDF do ciclo")
        # e-mail com o PDF (ciclo vazio não gera PDF, então não envia)
        if pdf and cfg.getboolean("email", "enviar", fallback=False) and (
                not manual or cfg.getboolean("email", "enviar_em_busca_manual", fallback=False)):
            try:
                from email_relatorio import enviar_relatorio
                para = enviar_relatorio(cfg, lite, pdf, cfg["agente"].get("usuario_execucao", ""))
                log.info("Relatório enviado por e-mail para: %s", ", ".join(para))
            except Exception:
                log.exception("Falha ao enviar o relatório por e-mail (o PDF continua em %s)", pdf)
    elif not avaliados:
        log.info("Ciclo sem avaliações: nenhum relatório gerado nem enviado")


_NUMERICAS = {
    ("agente", "max_por_ciclo"): int, ("agente", "max_chars_historico"): int, ("agente", "max_tokens_resposta"): int,
    ("agente", "exemplos_por_tipo"): int, ("agente", "intervalo_minutos"): int, ("agente", "limite_revisao"): float,
    ("agente", "limite_confianca"): float, ("agente", "temperatura"): float, ("aprendizado", "exemplos_similares"): int,
    ("aprendizado", "exemplos_historico"): int, ("aprendizado", "meses_historico"): int,
    ("privacidade", "max_imagens_por_atendimento"): int, ("fichas", "max_fichas_por_atendimento"): int,
    ("fichas", "max_chars_ficha"): int, ("fichas", "max_paginas"): int, ("audios", "max_audios_por_atendimento"): int,
    ("audios", "max_minutos_por_audio"): int, ("relatorio", "dias_sem_confirmacao"): int,
    ("email", "porta"): int, ("email", "timeout_segundos"): int,
}


def validar_cfg(cfg):
    """Opções numéricas do ini com valor inválido (ex.: "800{"): sem isso, cada atendimento do ciclo daria erro."""
    erros = []
    for (sec, opc), tipo in _NUMERICAS.items():
        v = cfg.get(sec, opc, fallback="").strip()
        if not v:
            continue
        try:
            tipo(v)
        except ValueError:
            erros.append(f"[{sec}] {opc} = {v}")
    return erros


def executar(args, manual=False):
    try:
        cfg = ler_cfg()
        erros_cfg = validar_cfg(cfg)
        if erros_cfg:
            log.error("Ciclo não executado: valor inválido no config.ini (deve ser número): %s", "; ".join(erros_cfg))
            return
        if args.max is not None:
            cfg["agente"]["max_por_ciclo"] = str(args.max)
        lite = init_sqlite(BASE / cfg["saida"]["sqlite"])
        client = criar_client(cfg, args.usuario)
        ciclo(cfg, lite, client, manual=manual)
        lite.close()
    except Exception:
        log.exception("Falha no ciclo")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uma-vez", action="store_true")
    ap.add_argument("--max", type=int, default=None, help="sobrescreve max_por_ciclo nesta execução")
    ap.add_argument("--usuario", default=None, help="usa a chave de IA cadastrada por este usuário")
    ap.add_argument("--manual", action="store_true", help="execução pelo painel (Buscar próximos)")
    args = ap.parse_args()

    if args.uma_vez:
        executar(args, manual=args.manual)
        return

    anunciado = None
    while True:
        try:
            cfg = ler_cfg()                     # relido a cada minuto: mudou o ini, vale sem reiniciar
            alvo = proxima_execucao(cfg)
        except Exception:
            log.exception("Erro no [agendamento] do config.ini; tentando de novo em 1 minuto")
            time.sleep(60)
            continue
        if alvo is None:                        # modo intervalo (antigo)
            executar(args)
            time.sleep(cfg.getint("agente", "intervalo_minutos", fallback=15) * 60)
            continue
        if alvo != anunciado:
            log.info("Próximo ciclo agendado: %s %s", NOMES[alvo.weekday()],
                     alvo.strftime("%d/%m/%Y %H:%M"))
            anunciado = alvo
        falta = (alvo - datetime.now()).total_seconds()
        if falta > 60:
            time.sleep(60)
            continue
        time.sleep(max(falta, 0))
        executar(args)
        anunciado = None
        time.sleep(1)


if __name__ == "__main__":
    main()
