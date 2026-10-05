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
import configparser
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import pyodbc

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
                "cqs_ok INTEGER DEFAULT 0", "motivo TEXT", "decidido_em TEXT"):
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


def montar_historico(rows, max_chars):
    linhas = []
    for r in rows:
        d = r.get("data")
        d = d.strftime("%d/%m/%Y %H:%M") if hasattr(d, "strftime") else str(d)
        autor = r.get("autor", "")
        linhas.append(f"[{d}] tipo {r.get('tipo', '')} - {autor}: {limpar(r.get('texto'))}")
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
Responda SOMENTE com JSON, sem texto fora dele:
{{"pode_concluir": true|false, "confianca": 0.0-1.0, "justificativa": "até 3 frases", "pendencias": "o que falta, ou vazio", "resumo": "...", "cqs": codtiporegistro ou null}}

O campo "resumo" é o texto que o analista vai registrar no atendimento, em português, objetivo, até 6 frases:
- Se pode concluir: o que o cliente solicitou, o que foi feito e o resultado/confirmação.
- Se não pode: o que foi entendido do histórico até aqui e o motivo de não concluir (o que ainda falta).
Baseie-se só no que está no histórico; não invente fatos.

{cqs_bloco}

{exemplos_bloco}"""


def montar_bloco_cqs(catalogo):
    if not catalogo:
        return 'Campo "cqs": responda sempre null.'
    linhas = []
    for c in catalogo:
        desc = limpar(c.get("descricaotiporegistro"))[:300]
        linhas.append(f"{c.get('codtiporegistro')} | {limpar(c.get('nometiporegistro'))} | "
                      f"{desc} | pontos: {c.get('pontos')}")
    return (
        'Campo "cqs": SOMENTE se pode_concluir=true, escolha o codtiporegistro da lista abaixo que melhor '
        'classifica este atendimento, com base no histórico. Se nenhum servir, ou se pode_concluir=false, '
        'use null. Use apenas códigos da lista.\n'
        'Lista (codtiporegistro | nome | descrição | pontos):\n' + "\n".join(linhas)
    )


def montar_exemplos(lite, cfg):
    """Decisões recentes do analista (aceitou/rejeitou) para a IA calibrar o julgamento."""
    n = cfg.getint("agente", "exemplos_por_tipo", fallback=3)
    if n <= 0:
        return ""
    linhas = []
    for status, rotulo in (("rejeitada", "DISCORDOU"), ("aceita", "CONCORDOU")):
        rows = lite.execute(
            "SELECT assunto, pode_concluir, cqs_cod, cqs_nome, resumo, motivo FROM avaliacoes "
            "WHERE status=? ORDER BY COALESCE(decidido_em, avaliado_em) DESC, id DESC LIMIT ?",
            (status, n),
        ).fetchall()
        for assunto, pode, cqs_cod, cqs_nome, resumo, motivo in rows:
            cqs = f" (CQS {cqs_cod} - {cqs_nome})" if cqs_cod else ""
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


def criar_client(cfg):
    prov = cfg["agente"].get("provider", "anthropic").strip().lower()
    key = os.environ[cfg["agente"]["api_key_env"]]
    if prov == "openai":
        from openai import OpenAI
        return OpenAI(api_key=key)
    import anthropic
    return anthropic.Anthropic(api_key=key)


def avaliar(client, cfg, atendimento, historico, catalogo=(), exemplos=""):
    system = SYSTEM.format(criterio=cfg["agente"]["criterio_conclusao"],
                           cqs_bloco=montar_bloco_cqs(catalogo), exemplos_bloco=exemplos)
    user = (
        f"Assunto: {atendimento.get('assunto', '')}\n"
        f"Cliente: {atendimento.get('cliente', '')}\n\n"
        f"HISTÓRICO:\n{historico}"
    )
    prov = cfg["agente"].get("provider", "anthropic").strip().lower()
    max_tok = cfg.getint("agente", "max_tokens_resposta", fallback=800)
    if prov == "openai":
        r = client.chat.completions.create(
            model=cfg["agente"]["modelo"],
            max_completion_tokens=max_tok,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        texto = r.choices[0].message.content or ""
        tin, tout = r.usage.prompt_tokens, r.usage.completion_tokens
    else:
        r = client.messages.create(
            model=cfg["agente"]["modelo"], max_tokens=max_tok, system=system,
            messages=[{"role": "user", "content": user}],
        )
        texto = "".join(b.text for b in r.content if b.type == "text")
        tin, tout = r.usage.input_tokens, r.usage.output_tokens
    m = re.search(r"\{.*\}", texto, re.S)
    if not m:
        raise ValueError(f"Resposta sem JSON: {texto[:200]}")
    return json.loads(m.group(0)), tin, tout


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
        catalogo = []
        if cfg.has_option("queries", "cqs_tipos"):
            try:
                catalogo = consultar(conn, cfg["queries"]["cqs_tipos"])
                log.info("%d tipos de CQS carregados", len(catalogo))
            except Exception:
                log.exception("Falha ao carregar CQS; seguindo sem classificação")
        por_cod = {str(c["codtiporegistro"]): c for c in catalogo}
        exemplos = montar_exemplos(lite, cfg)  # decisões do analista, 1x por ciclo

        # troca códigos de usuário por nome nas linhas já gravadas (analista)
        if cfg.has_option("queries", "usuarios"):
            try:
                for u in consultar(conn, cfg["queries"]["usuarios"]):
                    if u.get("nomeusuario"):
                        lite.execute("UPDATE avaliacoes SET analista=? WHERE analista=?",
                                     (u["nomeusuario"], str(u["codusuario"])))
                lite.commit()
            except Exception:
                log.exception("Falha ao atualizar nomes de analistas")

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
                    "SELECT qtd_interacoes, pode_concluir, cqs_ok FROM avaliacoes WHERE atendimento_id=? "
                    "ORDER BY id DESC LIMIT 1", (aid,)
                ).fetchone()
                # reavalia se chegou interação nova, ou se era "Sim" avaliado antes do recurso CQS
                precisa_cqs = bool(catalogo) and ult and ult[1] == 1 and not ult[2]
                if ult and ult[0] == len(inter) and not precisa_cqs:
                    # sem interação nova: não reavalia, mas atualiza cliente/analista/assunto
                    lite.execute(
                        "UPDATE avaliacoes SET cliente=?, analista=?, assunto=? WHERE atendimento_id=?",
                        (at.get("cliente"), at.get("analista"), at.get("assunto"), aid),
                    )
                    lite.commit()
                    continue
                if not inter:
                    continue

                res, i, o = avaliar(client, cfg, at, montar_historico(inter, max_chars), catalogo, exemplos)
                tin, tout = tin + i, tout + o
                avaliados += 1
                conf = float(res.get("confianca", 0))
                pode = bool(res.get("pode_concluir"))
                cqs = por_cod.get(str(res.get("cqs"))) if pode and res.get("cqs") is not None else None
                lite.execute(
                    """INSERT INTO avaliacoes(atendimento_id,cliente,assunto,analista,
                       qtd_interacoes,pode_concluir,confianca,justificativa,pendencias,
                       resumo,avaliado_em,modelo,cqs_cod,cqs_nome,cqs_descricao,cqs_pontos,cqs_ok)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (aid, at.get("cliente"), at.get("assunto"), at.get("analista"),
                     len(inter), int(pode), conf, res.get("justificativa"),
                     res.get("pendencias"), res.get("resumo"), datetime.now().isoformat(timespec="seconds"),
                     cfg["agente"]["modelo"],
                     str(cqs["codtiporegistro"]) if cqs else None,
                     limpar(cqs.get("nometiporegistro")) if cqs else None,
                     limpar(cqs.get("descricaotiporegistro")) if cqs else None,
                     str(cqs.get("pontos")) if cqs else None),
                )
                lite.commit()
                log.info("Atend %s -> concluir=%s conf=%.2f cqs=%s", aid, pode, conf,
                         cqs["codtiporegistro"] if cqs else "-")
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
    args = ap.parse_args()

    while True:
        espera = 15 * 60
        try:
            cfg = ler_cfg()
            if args.max is not None:
                cfg["agente"]["max_por_ciclo"] = str(args.max)
            espera = cfg.getint("agente", "intervalo_minutos", fallback=15) * 60
            lite = init_sqlite(BASE / cfg["saida"]["sqlite"])
            client = criar_client(cfg)
            ciclo(cfg, lite, client)
            lite.close()
        except Exception:
            log.exception("Falha no ciclo")
        if args.uma_vez:
            break
        time.sleep(espera)


if __name__ == "__main__":
    main()
