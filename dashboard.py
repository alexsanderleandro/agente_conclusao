import configparser
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyodbc
import streamlit as st

import cripto
import llm

BASE = Path(__file__).parent
cfg = configparser.ConfigParser(interpolation=None)
cfg.read(BASE / "config.ini", encoding="utf-8")
DB = BASE / cfg["saida"]["sqlite"]
LIMITE = cfg.getfloat("agente", "limite_confianca", fallback=0.8)

st.set_page_config(page_title="Avaliação de atendimentos", layout="wide")

ULTIMO_USUARIO = BASE / "ultimo_usuario.txt"


def _ler_ultimo_usuario():
    try:
        return ULTIMO_USUARIO.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _salvar_ultimo_usuario(usuario):
    try:
        ULTIMO_USUARIO.write_text(usuario, encoding="utf-8")
    except OSError:
        pass


def validar_login(usuario, senha):
    """Valida no banco do ERP: usuário ativo (InativoSN=0), senha pela csspValidaSenha e
    permissão de acesso (PDVGerenteSN=1). Retorna (ok, mensagem)."""
    conn = pyodbc.connect(cfg["banco"]["conn_string"], timeout=15)
    try:
        cur = conn.cursor()
        cur.execute("SELECT PDVGerenteSN FROM Usuarios WITH (NOLOCK) WHERE NomeUsuario = ? AND InativoSN = 0",
                    usuario)
        u = cur.fetchone()
        if u is None:
            return False, "Usuário ou senha inválidos."   # inexistente ou inativo (mesma mensagem)
        cur.execute("EXEC dbo.csspValidaSenha ?, ?", usuario, senha)
        r = cur.fetchone()
        if not (r and r[0] == 1):
            return False, "Usuário ou senha inválidos."
        if not u[0]:   # senha correta, mas sem perfil de gerente
            return False, "Acesso restrito a usuários gerentes."
        return True, ""
    finally:
        conn.close()


# ---- login: nada abaixo disso roda sem usuário logado ----
if not st.session_state.get("usuario_logado"):
    st.title("Revisão de conclusão de atendimentos")
    _, _col, _ = st.columns([1, 1, 1])
    with _col:
        st.subheader("Login")
        with st.form("login"):
            _usuario = st.text_input("Usuário", value=_ler_ultimo_usuario())
            _senha = st.text_input("Senha", type="password")
            _entrar = st.form_submit_button("Entrar", type="primary")
        if _entrar:
            if not _usuario.strip() or not _senha:
                st.error("Informe usuário e senha.")
            else:
                with st.spinner("Verificando usuário..."):
                    try:
                        _ok, _msg = validar_login(_usuario.strip(), _senha)
                    except Exception as e:
                        _ok, _msg = None, None
                        st.error("Erro ao validar usuário.")
                        st.code(str(e))
                if _ok:
                    st.session_state["usuario_logado"] = _usuario.strip()
                    _salvar_ultimo_usuario(_usuario.strip())
                    st.rerun()
                elif _ok is False:
                    st.error(_msg)
    st.stop()

_t1, _t2 = st.columns([6, 1])
_t1.title("Revisão de conclusão de atendimentos")
_t2.caption(f"👤 {st.session_state['usuario_logado']}")
if _t2.button("Sair"):
    st.session_state.clear()
    st.rerun()

# ---- chave de IA do usuário logado (obrigatória; gravada cifrada no config.ini) ----
USUARIO = st.session_state["usuario_logado"]
try:
    _cred = cripto.carregar(cfg, USUARIO)
except Exception as e:
    _cred = None
    st.error(f"Sua chave salva não pôde ser lida ({e}). Cadastre de novo.")


def form_chave_ia(atual):
    _provs = list(llm.PROVEDORES)
    _idx = _provs.index(atual["provedor"]) if atual and atual.get("provedor") in _provs else 0
    _c1, _c2 = st.columns(2)
    prov = _c1.selectbox("Empresa da IA", _provs, index=_idx,
                         format_func=lambda p: llm.PROVEDORES[p]["nome"], key="ia_prov")
    _mod_padrao = atual["modelo"] if atual and atual.get("provedor") == prov else llm.PROVEDORES[prov]["modelo"]
    modelo = _c2.text_input("Modelo", value=_mod_padrao, key=f"ia_modelo_{prov}",
                            help="Nome do modelo na API da empresa escolhida. Confira na sua conta.")
    mesma = bool(atual and atual.get("provedor") == prov)
    chave = st.text_input("Chave da API", type="password", key=f"ia_chave_{prov}",
                          placeholder="deixe vazio para manter a atual" if mesma else "cole aqui a sua chave")
    if st.button("Testar e salvar", type="primary", key="ia_salvar"):
        chave = chave.strip() or (atual["chave"] if mesma else "")
        if not chave or not modelo.strip():
            st.error("Informe o modelo e a chave da API.")
            return
        with st.spinner("Testando a chave..."):
            try:
                llm.testar_chave(prov, modelo.strip(), chave)
            except Exception as e:
                st.error("A chave não funcionou com esse modelo.")
                st.code(str(e)[:500])
                return
        try:
            cripto.salvar(USUARIO, prov, modelo.strip(), chave)
        except Exception as e:
            st.error(f"Chave válida, mas não consegui gravar no config.ini: {e}")
            return
        st.session_state["msg_chave"] = "Chave testada e salva."
        st.rerun()


if not _cred:
    st.subheader("🔑 Cadastre sua chave de IA para continuar")
    st.caption("Cada usuário usa a própria chave. Ela é testada e gravada criptografada no config.ini "
               "do agente; o painel nunca mostra a chave de volta.")
    form_chave_ia(None)
    st.stop()

if st.session_state.get("msg_chave"):
    st.success(st.session_state.pop("msg_chave"))
with st.expander(f"🔑 Minha IA: {llm.PROVEDORES.get(_cred['provedor'], {}).get('nome', _cred['provedor'])} · "
                 f"{_cred['modelo']} · chave ****{_cred['chave'][-4:]}"):
    form_chave_ia(_cred)


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
             "cqs_ok INTEGER DEFAULT 0", "motivo TEXT", "decidido_em TEXT",
             "fichas TEXT", "qtd_fichas INTEGER DEFAULT 0", "audios TEXT", "qtd_audios INTEGER DEFAULT 0",
             "na_fila INTEGER DEFAULT 1", "cqs_secundarios TEXT", "cqs_sec_ok INTEGER DEFAULT 0"):  # bancos de versões antigas
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


@st.cache_data(ttl=120, show_spinner=False)
def fila_atual_erp():
    """Atendimentos que estão AGORA na fila do ERP (Situacao=0 e última iteração = 26),
    pela mesma query 'candidatos' do agente. None se não conseguir consultar."""
    chave = [c.strip().lower() for c in cfg["queries"]["chave_colunas"].split(",")]
    conn = pyodbc.connect(cfg["banco"]["conn_string"], timeout=15)
    try:
        cur = conn.cursor()
        cur.execute(cfg["queries"]["candidatos"])
        cols = [c[0].lower() for c in cur.description]
        return {"-".join(str(dict(zip(cols, r))[c]) for c in chave) for r in cur.fetchall()}
    finally:
        conn.close()


try:
    _fila = fila_atual_erp()
    _fonte_fila = "ERP agora"
except Exception as e:
    _fila = set(df.loc[df.get("na_fila", 1) == 1, "atendimento_id"]) if "na_fila" in df else None
    _fonte_fila = "última execução do agente"
    st.warning(f"Não consegui consultar a fila no ERP ({str(e)[:120]}); usando a {_fonte_fila}.")
_fora = 0
if _fila is not None:
    _na_fila = df.atendimento_id.isin(_fila)
    _fora = int((~_na_fila).sum())
    if not st.sidebar.checkbox(f"Mostrar também os que saíram da fila ({_fora})", value=False,
                               help="Avaliados antes, mas que já foram concluídos ou movimentados no ERP"):
        df = df[_na_fila]

# ---- buscar próximos (roda um ciclo do agente agora) ----
def buscar_proximos(n):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        p = subprocess.run([sys.executable, str(BASE / "agente.py"), "--uma-vez", "--max", str(int(n)),
                            "--usuario", USUARIO],
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
c0.metric("Encontrados no ERP", len(_fila) if _fila is not None else (_r[0] if _r else len(df)),
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
    fila_atual_erp.clear()   # relê a fila do ERP depois do ciclo
    st.rerun()
_bt2.caption("Roda um ciclo do agente agora: avalia os próximos atendimentos ainda não avaliados "
             "(consome crédito da sua chave de IA).")

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
        "confianca", "cqs_cod", "cqs_sec", "resumo", "status", "avaliado_em"]


def _secs(js):
    try:
        return json.loads(js) if isinstance(js, str) and js else []
    except ValueError:
        return []


v = v.assign(cqs_sec=v.cqs_secundarios.map(
    lambda js: ", ".join(f"{s.get('cqs') or '?'} ({s.get('analista') or '?'})" for s in _secs(js))))
vis = v[cols].copy()
vis["pode_concluir"] = vis["pode_concluir"].map({1: "Sim", 0: "Não"})
vis["cqs_cod"] = vis["cqs_cod"].fillna("")
vis = vis.rename(columns={
    "atendimento_id": "Atendimento", "cliente": "Cliente", "assunto": "Assunto",
    "analista": "Analista", "pode_concluir": "Pode concluir", "confianca": "Confiança",
    "cqs_cod": "CQS", "cqs_sec": "CQS secundário", "resumo": "Resumo",
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
    for s in _secs(r.cqs_secundarios):
        st.write(f"**CQS secundário:** {s.get('cqs') or 'não classificado'} — {s.get('cqs_nome') or ''} "
                 f"(pontos: {s.get('cqs_pontos') or '-'}) · analista **{s.get('analista') or '?'}** · "
                 f"{s.get('tipo') or ''} {s.get('modalidade') or ''} · {s.get('data') or ''}")
    if isinstance(r.fichas, str) and r.fichas:
        st.write("**Fichas de visita lidas:** " + " · ".join(r.fichas.splitlines()))
    if isinstance(r.audios, str) and r.audios:
        st.write("**Áudios transcritos:** " + " · ".join(r.audios.splitlines()))
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
