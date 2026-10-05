import configparser
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

BASE = Path(__file__).parent
cfg = configparser.ConfigParser(interpolation=None)
cfg.read(BASE / "config.ini", encoding="utf-8")
DB = BASE / cfg["saida"]["sqlite"]
LIMITE = cfg.getfloat("agente", "limite_confianca", fallback=0.8)

st.set_page_config(page_title="Avaliação de atendimentos", layout="wide")
st.title("Revisão de conclusão de atendimentos")


def salvar_max_por_ciclo(valor):
    """Altera só a linha max_por_ciclo do config.ini, preservando comentários e formatação."""
    caminho = BASE / "config.ini"
    with open(caminho, encoding="utf-8", newline="") as f:
        txt = f.read()
    novo, n = re.subn(r"(?m)^(max_por_ciclo[ \t]*=[ \t]*)\d+", rf"\g<1>{int(valor)}", txt, count=1)
    if n == 0:
        raise ValueError("Linha max_por_ciclo não encontrada no config.ini")
    tmp = caminho.with_name("config.ini.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(novo)
    os.replace(tmp, caminho)


_max_atual = cfg.getint("agente", "max_por_ciclo", fallback=20)
with st.expander(f"Configuração do agente — máximo de registros por ciclo: {_max_atual}"):
    _c1, _c2 = st.columns([1, 3])
    _novo_max = _c1.number_input("Máximo de registros avaliados por ciclo", min_value=0, step=1,
                                 value=_max_atual, key="max_por_ciclo")
    _c2.caption("É o `max_por_ciclo` do config.ini: quantos atendimentos o agente avalia a cada rodada "
                "(controla o gasto de crédito da IA). 0 = não avalia nenhum. "
                "A mudança vale a partir do próximo ciclo do agente, sem reiniciar.")
    if st.button("Salvar no config.ini"):
        try:
            salvar_max_por_ciclo(_novo_max)
            st.success(f"Salvo: max_por_ciclo = {int(_novo_max)}")
        except Exception as e:
            st.error(f"Não consegui salvar: {e}")

con = sqlite3.connect(DB)
for _col in ("resumo TEXT", "cqs_cod TEXT", "cqs_nome TEXT", "cqs_descricao TEXT", "cqs_pontos TEXT",
             "cqs_ok INTEGER DEFAULT 0", "motivo TEXT", "decidido_em TEXT"):  # bancos de versões antigas
    try:
        con.execute(f"ALTER TABLE avaliacoes ADD COLUMN {_col}")
        con.commit()
    except sqlite3.OperationalError:
        pass
try:
    df = pd.read_sql(
        "SELECT * FROM avaliacoes WHERE id IN (SELECT MAX(id) FROM avaliacoes GROUP BY atendimento_id)",
        con,
    )
except Exception:
    st.info("Ainda não há avaliações. Rode o agente (agente.py) ao menos uma vez.")
    st.stop()

# ---- buscar próximos (roda um ciclo do agente agora) ----
def buscar_proximos(n):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        p = subprocess.run([sys.executable, str(BASE / "agente.py"), "--uma-vez", "--max", str(int(n))],
                           cwd=str(BASE), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800)
    except subprocess.TimeoutExpired:
        return "erro", "Passou de 30 minutos e foi interrompido. Tente um número menor."
    saida = (p.stdout or "") + (p.stderr or "")
    ok = re.search(r"Ciclo ok: (\d+) avaliados, (\d+) erros", saida)
    if p.returncode != 0 or not ok or "Falha no ciclo" in saida:
        return "erro", "\n".join(saida.strip().splitlines()[-15:]) or "Sem saída do agente."
    avaliados, erros = int(ok.group(1)), int(ok.group(2))
    if avaliados == 0 and erros == 0:
        return "info", "Nada novo para avaliar: todos os atendimentos encontrados já foram avaliados."
    msg = f"{avaliados} atendimentos avaliados."
    return ("aviso", msg + f" {erros} com erro (veja o agente.log).") if erros else ("ok", msg)


if st.session_state.get("ultima_busca"):
    _tipo, _msg = st.session_state.pop("ultima_busca")
    {"ok": st.success, "info": st.info, "aviso": st.warning}.get(_tipo, st.error)(_msg)

# ---- métricas ----
aceitas = (df.status == "aceita").sum()
rejeitadas = (df.status == "rejeitada").sum()
c0, c1, c2, c3, c4, c5 = st.columns(6)
try:
    _r = con.execute("SELECT candidatos FROM execucoes ORDER BY id DESC LIMIT 1").fetchone()
except sqlite3.Error:
    _r = None
c0.metric("Encontrados no ERP", _r[0] if _r else len(df),
          help="Atendimentos aguardando revisão achados na última execução do agente")
c1.metric("Pendentes (podem concluir)", int(((df.status == "pendente") & (df.pode_concluir == 1)).sum()))
c2.metric("Aceitas", int(aceitas))
c3.metric("Rejeitadas", int(rejeitadas))
c4.metric("Taxa de acerto", f"{aceitas / (aceitas + rejeitadas):.0%}" if aceitas + rejeitadas else "-")
c5.metric("Não autorizados a concluir", int((df.pode_concluir == 0).sum()))

_bt1, _bt2 = st.columns([1, 3])
if _bt1.button(f"🔄 Buscar próximos {int(_novo_max)}", type="primary"):
    with st.spinner(f"Avaliando até {int(_novo_max)} atendimentos... (pode levar alguns minutos)"):
        st.session_state["ultima_busca"] = buscar_proximos(_novo_max)
    st.rerun()
_bt2.caption("Roda um ciclo do agente agora: avalia os próximos atendimentos ainda não avaliados "
             "(consome crédito da IA). A chave da API precisa estar definida na janela que iniciou o painel.")

# ---- filtros ----
f1, f2, f3 = st.columns(3)
status = f1.multiselect("Status", ["pendente", "aceita", "rejeitada"], default=["pendente"])
_analistas = sorted(df.analista.dropna().astype(str).unique())
analistas_sel = f2.multiselect("Analista", _analistas, placeholder="Todos")
so_concluir = f3.checkbox("Só os que podem concluir", value=True)

v = df[df.status.isin(status)]
if analistas_sel:
    v = v[v.analista.isin(analistas_sel)]
if so_concluir:
    v = v[v.pode_concluir == 1]
v = v.sort_values("confianca", ascending=False)

st.markdown(f"**Na lista atual:** {len(v)} registros (de {len(df)} avaliados)")

cols = ["atendimento_id", "cliente", "assunto", "analista", "pode_concluir",
        "confianca", "cqs_cod", "resumo", "status", "avaliado_em"]
vis = v[cols].copy()
vis["pode_concluir"] = vis["pode_concluir"].map({1: "Sim", 0: "Não"})
vis["cqs_cod"] = vis["cqs_cod"].fillna("")
vis = vis.rename(columns={
    "atendimento_id": "Atendimento", "cliente": "Cliente", "assunto": "Assunto",
    "analista": "Analista", "pode_concluir": "Pode concluir", "confianca": "Confiança",
    "cqs_cod": "CQS", "resumo": "Resumo",
    "status": "Status", "avaliado_em": "Avaliado em"})
sel = st.dataframe(vis, on_select="rerun", selection_mode="single-row",
                   use_container_width=True, hide_index=True)

# ---- detalhe do registro marcado (aparece abaixo da lista) ----
if sel.selection.rows:
    r = v.iloc[sel.selection.rows[0]]
    st.subheader(f"Atendimento {r.atendimento_id} — {r.assunto}")
    st.write(f"**Confiança:** {r.confianca:.0%}" + ("  ⚠️ abaixo do limite" if r.confianca < LIMITE else ""))
    st.write(f"**Justificativa:** {r.justificativa}")
    resumo = r.resumo if isinstance(r.resumo, str) and r.resumo else "(sem resumo: avaliação feita antes do campo existir)"
    st.text_area("Resumo (texto para registrar no atendimento)", resumo, height=160,
                 key=f"resumo_{r.id}")
    if isinstance(r.cqs_cod, str) and r.cqs_cod:
        with st.expander(f"CQS: {r.cqs_cod}", expanded=True):
            st.write(f"**Pontos:** {r.cqs_pontos}")
            st.write(f"**Nome:** {r.cqs_nome}")
            st.write(f"**Descrição:** {r.cqs_descricao}")
    if isinstance(r.pendencias, str) and r.pendencias:
        st.write(f"**Pendências:** {r.pendencias}")
    motivo_ant = r.motivo if isinstance(r.motivo, str) else ""
    motivo = st.text_input("Motivo / observação (a IA aprende com isso; obrigatório para rejeitar)",
                           value=motivo_ant, key=f"motivo_{r.id}")
    st.caption("Aceitar = concordo com a avaliação da IA · Rejeitar = discordo "
               "(vale também para os 'Não': rejeitar significa que podia concluir).")
    b1, b2, _ = st.columns([1, 1, 6])
    for rotulo, novo, col in (("Aceitar", "aceita", b1), ("Rejeitar", "rejeitada", b2)):
        if col.button(rotulo, key=f"{novo}_{r.id}"):
            if novo == "rejeitada" and not motivo.strip():
                st.warning("Escreva o motivo da rejeição para a IA aprender com ele.")
            else:
                con.execute("UPDATE avaliacoes SET status=?, motivo=?, decidido_em=? WHERE id=?",
                            (novo, motivo.strip(), datetime.now().isoformat(timespec="seconds"), int(r.id)))
                con.commit()
                st.rerun()

with st.expander("Últimas execuções"):
    try:
        st.dataframe(pd.read_sql("SELECT * FROM execucoes ORDER BY id DESC LIMIT 30", con),
                     use_container_width=True, hide_index=True)
    except Exception:
        st.write("Sem execuções registradas ainda.")
