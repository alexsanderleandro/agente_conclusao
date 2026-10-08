import configparser
import hashlib
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
import migracoes
import llm
from agendamento import NOMES, proxima_execucao

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
    import tema_login
    # imagem de fundo: assets/login_fundo.(svg|png|jpg|jpeg|webp) - a primeira que existir
    _fundo = next((BASE / "assets" / f"login_fundo.{_ext}" for _ext in ("svg", "png", "jpg", "jpeg", "webp")
                   if (BASE / "assets" / f"login_fundo.{_ext}").is_file()), None)
    st.markdown(tema_login.css("c", _fundo), unsafe_allow_html=True)
    st.title("Revisão de conclusão de atendimentos")
    _, _col, _ = st.columns([1, 1, 1])
    with _col:
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
# título 40% menor que o padrão do st.title (2.75rem -> 1.65rem)
_t1.markdown('<h1 style="font-size:1.65rem;padding:0.4rem 0 0.2rem">Revisão de conclusão de atendimentos</h1>',
             unsafe_allow_html=True)
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


# ---- agenda automática e e-mail do relatório: editável aqui, gravado no config.ini ----
# (o painel relê o ini a cada interação, então mudar no arquivo também aparece aqui)
_DIAS_INI = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")
_SIMS = ("sim", "s", "1", "true", "yes", "on")
_EMAIL_RX = re.compile(r"^[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+$")


def _ini(sec, opc, padrao=""):
    return cfg.get(sec, opc, fallback=padrao).strip()


def _ini_bool(sec, opc, padrao=False):
    v = _ini(sec, opc)
    return padrao if not v else v.lower() in _SIMS


def _emails(txt):
    lst = [e.strip() for e in re.split(r"[;,\s]+", txt or "") if e.strip()]
    ruins = [e for e in lst if not _EMAIL_RX.match(e)]
    return lst, ruins


try:
    _prox = proxima_execucao(cfg)
    _txt_agenda = (f"⏰ Próximo ciclo: {NOMES[_prox.weekday()]} {_prox:%d/%m %H:%M}" if _prox
                   else f"⏰ Ciclo a cada {_ini('agente', 'intervalo_minutos', '15')} min")
except Exception as e:
    _txt_agenda = f"⏰ [agendamento] inválido: {e}"
_email_on = _ini_bool("email", "enviar")
_tem_senha = bool(_ini("email", "senha"))

# chave dos widgets muda quando o ini muda -> o formulário sempre mostra o que está no arquivo
_sig = hashlib.md5(repr([(s, sorted(cfg.items(s))) for s in ("agendamento", "email")
                          if cfg.has_section(s)]).encode()).hexdigest()[:10]

with st.expander(f"✉️ E-mail do relatório: {'ligado' if _email_on else 'desligado'}"
                 f"{'' if _tem_senha or not _email_on else ' · ⚠️ senha não cadastrada'}   ·   {_txt_agenda}",
                 expanded=bool(st.session_state.get("msg_email"))):
    with st.form(f"form_email_{_sig}"):
        st.markdown("**Agenda do ciclo automático**")
        _a1, _a2, _a3, _a4 = st.columns([1.3, 3, 1.4, 1])
        _modo = _a1.selectbox("Modo", ["horario", "intervalo"],
                              index=0 if _ini("agendamento", "modo", "intervalo").lower() == "horario" else 1,
                              format_func=lambda m: "Dias e horários" if m == "horario" else "Intervalo (min)")
        try:
            from agendamento import _dias_semana
            _dias_atuais = [_DIAS_INI[i] for i in sorted(_dias_semana(_ini("agendamento", "dias", "seg-sab")))]
        except Exception:
            _dias_atuais = list(_DIAS_INI[:6])
        _dias = _a2.multiselect("Dias", _DIAS_INI, default=_dias_atuais)
        _horas = _a3.text_input("Horários (HH:MM)", value=_ini("agendamento", "horarios", "19:00"),
                                help="Um ou mais, separados por vírgula. Ex.: 12:00, 19:00")
        _interv = _a4.number_input("Intervalo (min)", min_value=1, step=1,
                                   value=int(_ini("agente", "intervalo_minutos", "15") or 15),
                                   help="Usado só no modo Intervalo")
        st.markdown("**E-mail**")
        _c1, _c2, _c3, _c4 = st.columns([1, 2.2, 0.8, 2.4])
        _env = _c1.checkbox("Enviar e-mail", value=_email_on)
        _host = _c2.text_input("Servidor SMTP", value=_ini("email", "smtp_host"))
        _porta = _c3.number_input("Porta", min_value=1, max_value=65535, step=1,
                                  value=int(_ini("email", "porta", "587") or 587),
                                  help="587 STARTTLS · 465 SSL · 2525 alternativa")
        _usr = _c4.text_input("Conta (login)", value=_ini("email", "usuario"))
        _rem = st.text_input("Remetente (nome e e-mail)", value=_ini("email", "remetente"))
        _para = st.text_input("Destinatários (separe com ;)", value=_ini("email", "destinatarios"))
        _cc = st.text_input("Cópia (opcional)", value=_ini("email", "copia"))
        _ass = st.text_input("Assunto", value=_ini("email", "assunto"),
                             help="{data} = data/hora do ciclo · {concluir} = quantos podem concluir")
        _man = st.checkbox("Enviar também no \"Buscar próximos\" do painel",
                           value=_ini_bool("email", "enviar_em_busca_manual"))
        _sn = st.text_input("Senha da conta", type="password",
                            placeholder="deixe vazio para manter a atual" if _tem_senha else "senha da conta",
                            help="Só é gravada se o e-mail de teste funcionar. Fica cifrada no config.ini.")
        _f1, _f2 = st.columns(2)
        _salvar = _f1.form_submit_button("💾 Salvar no config.ini", type="primary")
        _testar = _f2.form_submit_button("✉️ Salvar e enviar teste")

    if _salvar or _testar:
        _erros = []
        _lp, _rp = _emails(_para)
        _lc, _rc = _emails(_cc)
        if _rp or _rc:
            _erros.append(f"E-mail inválido: {', '.join(_rp + _rc)}")
        if _env and not _lp:
            _erros.append("Informe ao menos um destinatário.")
        if _usr.strip() and not _EMAIL_RX.match(_usr.strip()):
            _erros.append("Conta (login) deve ser um e-mail.")
        _hs = [h.strip() for h in re.split(r"[,;\s]+", _horas) if h.strip()]
        try:
            _hs = [datetime.strptime(h, "%H:%M").strftime("%H:%M") for h in _hs]
        except ValueError:
            _erros.append("Horário inválido: use HH:MM, ex.: 19:00")
        if _modo == "horario" and (not _dias or not _hs):
            _erros.append("Escolha ao menos um dia e um horário.")
        if _erros:
            for _e in _erros:
                st.error(_e)
        else:
            _novos = {
                ("agendamento", "modo"): _modo,
                ("agendamento", "dias"): ",".join(d for d in _DIAS_INI if d in _dias),
                ("agendamento", "horarios"): ", ".join(sorted(set(_hs))),
                ("agente", "intervalo_minutos"): str(int(_interv)),
                ("email", "enviar"): "sim" if _env else "nao",
                ("email", "smtp_host"): _host.strip(),
                ("email", "porta"): str(int(_porta)),
                ("email", "usuario"): _usr.strip(),
                ("email", "remetente"): _rem.strip(),
                ("email", "destinatarios"): "; ".join(_lp),
                ("email", "copia"): "; ".join(_lc),
                ("email", "assunto"): _ass.strip(),
                ("email", "enviar_em_busca_manual"): "sim" if _man else "nao",
            }
            try:
                for (_s, _o), _v in _novos.items():
                    if _ini(_s, _o) != _v:
                        cripto.gravar_opcao(_s, _o, _v)
                _msg = "Configuração salva no config.ini (o agente aplica em até 1 minuto)."
                if _testar or _sn:
                    import email_relatorio
                    _cfg2 = configparser.ConfigParser(interpolation=None)
                    _cfg2.read(BASE / "config.ini", encoding="utf-8")
                    with st.spinner("Enviando e-mail de teste..."):
                        _dest = email_relatorio.enviar_teste(_cfg2, senha=_sn or None)
                    if _sn:
                        cripto.salvar_senha_email(_sn)
                        _msg += " Senha salva."
                    _msg += f" Teste enviado para: {', '.join(_dest)}"
                st.session_state["msg_email"] = ("ok", _msg)
            except Exception as e:
                st.session_state["msg_email"] = ("erro", f"Configuração salva, mas o teste falhou"
                                                         f"{' (senha NÃO foi salva)' if _sn else ''}: "
                                                         + ("usuário ou senha recusados pelo servidor SMTP"
                                                            if "535" in str(e) or "auth" in str(e).lower() else str(e)))
            st.rerun()
    if st.session_state.get("msg_email"):
        _t, _m = st.session_state.pop("msg_email")
        (st.success if _t == "ok" else st.error)(_m)


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
             "na_fila INTEGER DEFAULT 1", "cqs_secundarios TEXT", "cqs_sec_ok INTEGER DEFAULT 0", "ciclo_inicio TEXT",
             "status_cqs TEXT", "cqs_correto TEXT", "motivo_cqs TEXT", "decidido_por TEXT"):  # bancos de versões antigas
    try:
        con.execute(f"ALTER TABLE avaliacoes ADD COLUMN {_col}")
        con.commit()
    except sqlite3.OperationalError:
        pass
migracoes.migrar(con)   # revisões antigas -> análise e CQS separados
try:
    df = pd.read_sql(
        "SELECT * FROM avaliacoes WHERE id IN (SELECT MAX(id) FROM avaliacoes GROUP BY atendimento_id)",
        con,
    )
except Exception:
    df = None
_sem_dados = df is None or df.empty
if df is None:   # banco ainda vazio: segue até o botão "Buscar próximos" para poder fazer a 1ª busca
    df = pd.DataFrame(columns=["atendimento_id", "cliente", "assunto", "analista", "pode_concluir", "confianca",
                               "cqs_cod", "cqs_secundarios", "resumo", "status", "avaliado_em", "na_fila",
                               "status_cqs", "cqs_correto", "motivo_cqs", "decidido_por"])

# análise (concluir sim/não) e CQS são avaliados separadamente.
# CQS só se aplica quando a IA disse "Sim", sugeriu um CQS e a análise não foi rejeitada.
df["cqs_aplica"] = (df.pode_concluir == 1) & df.cqs_cod.fillna("").astype(str).ne("") & (df.status != "rejeitada")
df["st_cqs"] = df.status_cqs.where(df.status_cqs.notna(), "pendente").where(df.cqs_aplica, "-")
df["status_geral"] = "aceita"
df.loc[(df.status == "rejeitada") | (df.st_cqs == "rejeitada"), "status_geral"] = "rejeitada"
df.loc[(df.status == "pendente") | (df.st_cqs == "pendente"), "status_geral"] = "pendente"


@st.cache_data(ttl=600, show_spinner=False)
def catalogo_cqs():
    """[(código, nome)] dos CQS permitidos como principal ([cqs] principal); [] se não conseguir ler o ERP."""
    try:
        conn = pyodbc.connect(cfg["banco"]["conn_string"], timeout=15)
        try:
            cur = conn.cursor()
            cur.execute(cfg["queries"]["cqs_tipos"])
            cols = [c[0].lower() for c in cur.description]
            rows = [dict(zip(cols, x)) for x in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        return []
    perm = {c.strip() for c in cfg.get("cqs", "principal", fallback="").split(",") if c.strip()}
    sec = {c.strip() for c in cfg.get("cqs", "secundario", fallback="").split(",") if c.strip()}
    perm |= sec
    out = [(str(x["codtiporegistro"]), str(x.get("nometiporegistro") or "")) for x in rows]
    return [c for c in out if not perm or c[0] in perm]


@st.cache_data(ttl=120, show_spinner=False)
def fila_atual_erp():
    """(ids, hora da consulta) dos atendimentos que estão AGORA na fila do ERP (Situacao=0 e
    última iteração = 26), pela mesma query 'candidatos' do agente. Exceção se não conseguir consultar."""
    chave = [c.strip().lower() for c in cfg["queries"]["chave_colunas"].split(",")]
    conn = pyodbc.connect(cfg["banco"]["conn_string"], timeout=15)
    try:
        cur = conn.cursor()
        cur.execute(cfg["queries"]["candidatos"])
        cols = [c[0].lower() for c in cur.description]
        ids = {"-".join(str(dict(zip(cols, r))[c]) for c in chave) for r in cur.fetchall()}
        return ids, datetime.now().strftime("%H:%M:%S")
    finally:
        conn.close()


_fila_em = None
try:
    _fila, _fila_em = fila_atual_erp()
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
                            "--usuario", USUARIO, "--manual"],
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
    msg = f"{avaliados} atendimentos avaliados (limite desta busca: {int(n)})."
    return ("aviso", msg + f" {erros} com erro (veja o agente.log).") if erros else ("ok", msg)


if st.session_state.get("ultima_busca"):
    _tipo, _msg = st.session_state.pop("ultima_busca")
    {"ok": st.success, "info": st.info, "aviso": st.warning}.get(_tipo, st.error)(_msg)

# ---- métricas ----
aceitas = int((df.status == "aceita").sum())
rejeitadas = int((df.status == "rejeitada").sum())
cqs_ok = int((df.st_cqs == "aceita").sum())
cqs_err = int((df.st_cqs == "rejeitada").sum())
c0, c1, c2, c3, c4, c5 = st.columns(6)
try:
    _r = con.execute("SELECT candidatos FROM execucoes ORDER BY id DESC LIMIT 1").fetchone()
except sqlite3.Error:
    _r = None
c0.metric("Encontrados no ERP", len(_fila) if _fila is not None else (_r[0] if _r else len(df)),
          help=f"Atendimentos aguardando revisão na fila do ERP (consultado às {_fila_em})" if _fila_em
          else f"Atendimentos aguardando revisão (fonte: {_fonte_fila})")
# relê só a fila do ERP (sem rodar o agente): o resultado fica em cache por 2 minutos
c0.button("Atualizar", key="atualizar_fila", icon=":material/refresh:", type="tertiary",
          on_click=fila_atual_erp.clear, help="Consulta o ERP agora para ver se entraram ou saíram atendimentos da fila")
c1.metric("Pendentes (podem concluir)", int(((df.status == "pendente") & (df.pode_concluir == 1)).sum()))
c2.metric("Acerto da análise", f"{aceitas / (aceitas + rejeitadas):.0%}" if aceitas + rejeitadas else "-",
          help=f"Concluir sim/não: {aceitas} corretas · {rejeitadas} erradas")
c3.metric("Acerto do CQS", f"{cqs_ok / (cqs_ok + cqs_err):.0%}" if cqs_ok + cqs_err else "-",
          help=f"CQS sugerido: {cqs_ok} corretos · {cqs_err} errados")
c4.metric("Aguardando sua revisão", int((df.status_geral == "pendente").sum()),
          help="Análise ou CQS ainda sem avaliação")
c5.metric("Não autorizados a concluir", int((df.pode_concluir == 0).sum()))

_bt1, _bt2 = st.columns([1, 3])
if _bt1.button(f"🔄 Buscar próximos {int(_novo_max)}", type="primary"):
    with st.spinner(f"Avaliando até {int(_novo_max)} atendimentos... (pode levar alguns minutos)"):
        st.session_state["ultima_busca"] = buscar_proximos(_novo_max)
        st.session_state["exibir"] = "ultima"
    fila_atual_erp.clear()   # relê a fila do ERP depois do ciclo
    st.rerun()
_bt2.caption("Roda um ciclo do agente agora: avalia os próximos atendimentos ainda não avaliados "
             "(consome crédito da sua chave de IA).")
_pasta_rel = cfg.get("relatorio", "pasta", fallback="relatorios")
_pasta_rel = Path(_pasta_rel) if os.path.isabs(_pasta_rel) else BASE / _pasta_rel
_pdfs = sorted(_pasta_rel.glob("Relatorio_ciclo_*.pdf"), reverse=True) if _pasta_rel.is_dir() else []
if _pdfs:
    _sel_pdf = _bt2.selectbox("Relatório do ciclo (PDF)", _pdfs, format_func=lambda p: (lambda s: f"Ciclo {s[8:10]}/{s[5:7]}/{s[0:4]} {s[11:13]}:{s[13:15]}:{s[15:17]}")(
        p.stem.replace("Relatorio_ciclo_", "")), key="rel_pdf")
    _bd1, _bd2 = _bt2.columns(2)
    _bd1.download_button("📄 Baixar relatório", data=_sel_pdf.read_bytes(), file_name=_sel_pdf.name,
                         mime="application/pdf")
    if _bd2.button("✉️ Enviar por e-mail", help="Envia este relatório aos destinatários do [email] no config.ini"):
        try:
            import email_relatorio
            with sqlite3.connect(DB) as _lite, st.spinner("Enviando..."):
                _para = email_relatorio.enviar_relatorio(cfg, _lite, _sel_pdf, USUARIO)
            st.success(f"Relatório enviado para: {', '.join(_para)}")
        except Exception as e:
            st.error(f"Não foi possível enviar: {e}")
if _sem_dados:
    st.info("Ainda não há avaliações. Clique em **Buscar próximos** para fazer a primeira.")
    st.stop()

# ---- filtros ----
f0, f1, f2, f3 = st.columns([1.2, 1, 1, 1])
_ultimo = df["ciclo_inicio"].dropna().max() if "ciclo_inicio" in df and df["ciclo_inicio"].notna().any() else None
_n_ult = int((df["ciclo_inicio"] == _ultimo).sum()) if _ultimo else 0
_opts = [f"Última busca ({_n_ult})", "Todos da fila"]
exibir = f0.radio("Exibir", _opts, horizontal=True,
                  index=0 if st.session_state.pop("exibir", None) == "ultima" else 1,
                  help="Última busca = só os avaliados no último 'Buscar próximos'. "
                       "Todos da fila = tudo que já foi avaliado e continua aguardando revisão no ERP.")
status = f1.multiselect("Revisão", ["pendente", "aceita", "rejeitada"], default=["pendente"],
                        help="pendente = falta avaliar a análise ou o CQS · rejeitada = errou a análise ou o CQS")
_analistas = sorted(df.analista.dropna().astype(str).unique())
analistas_sel = f2.multiselect("Analista", _analistas, placeholder="Todos")
so_concluir = f3.checkbox("Só os que podem concluir", value=True)

v = df[df.status_geral.isin(status)]
if exibir.startswith("Última") and _ultimo:
    v = v[v["ciclo_inicio"] == _ultimo]
if analistas_sel:
    v = v[v.analista.isin(analistas_sel)]
if so_concluir:
    v = v[v.pode_concluir == 1]
v = v.sort_values("confianca", ascending=False)

_nao_avaliados = len(_fila - set(df.atendimento_id)) if _fila is not None else 0
st.markdown(f"**Na lista atual:** {len(v)} registros · fila do ERP: {len(_fila) if _fila is not None else '?'}"
            f" · avaliados: {len(df)} · ainda não avaliados: {_nao_avaliados}")
if len(v) < len(df):
    _motivos = []
    _base = df
    if exibir.startswith("Última") and _ultimo:
        _n = int((_base["ciclo_inicio"] != _ultimo).sum()); _base = _base[_base["ciclo_inicio"] == _ultimo]
        if _n: _motivos.append(f"{_n} de buscas anteriores (escolha **Todos da fila**)")
    _n = int((~_base.status_geral.isin(status)).sum()); _base = _base[_base.status_geral.isin(status)]
    if _n: _motivos.append(f"{_n} fora do filtro de revisão (já revisados)")
    if analistas_sel:
        _n = int((~_base.analista.isin(analistas_sel)).sum()); _base = _base[_base.analista.isin(analistas_sel)]
        if _n: _motivos.append(f"{_n} de outros analistas")
    if so_concluir:
        _n = int((_base.pode_concluir != 1).sum())
        if _n: _motivos.append(f"{_n} avaliados como **Não** (desmarque *Só os que podem concluir*)")
    if _motivos:
        st.caption("Ocultos pelos filtros: " + " · ".join(_motivos))
if _nao_avaliados:
    st.caption(f"{_nao_avaliados} atendimento(s) da fila ainda não foram avaliados: use **Buscar próximos**.")

cols = ["atendimento_id", "cliente", "assunto", "analista", "pode_concluir",
        "confianca", "status", "cqs_cod", "st_cqs", "cqs_sec", "resumo", "decidido_por", "avaliado_em"]


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
_ICON = {"pendente": "⏳ pendente", "aceita": "✅ correta", "rejeitada": "❌ errada", "-": ""}
vis["status"] = vis["status"].map(lambda x: _ICON.get(x, x))
vis["st_cqs"] = vis["st_cqs"].map(lambda x: _ICON.get(x, x).replace("correta", "correto").replace("errada", "errado"))
vis["decidido_por"] = vis["decidido_por"].fillna("")
vis = vis.rename(columns={
    "atendimento_id": "Atendimento", "cliente": "Cliente", "assunto": "Assunto",
    "analista": "Analista", "pode_concluir": "Pode concluir", "confianca": "Confiança",
    "cqs_cod": "CQS", "cqs_sec": "CQS secundário", "resumo": "Resumo",
    "status": "Análise", "st_cqs": "Status CQS", "decidido_por": "Revisado por", "avaliado_em": "Avaliado em"})
sel = st.dataframe(vis, on_select="rerun", selection_mode="single-row",
                   width="stretch", hide_index=True)

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
    # ---- revisão em duas partes: análise (concluir sim/não) e CQS ----
    st.divider()
    _txt = lambda x: x if isinstance(x, str) else ""
    _OPC = {"pendente": "⏳ Pendente", "aceita": "✅ Correta", "rejeitada": "❌ Errada"}
    _ia_sim = int(r.pode_concluir) == 1
    _tem_cqs = _ia_sim and bool(_txt(r.cqs_cod))
    with st.form(f"rev_{r.id}"):
        ca, cb = st.columns(2)
        with ca:
            st.markdown(f"**1 · Análise** — a IA disse: **{'Sim, pode concluir' if _ia_sim else 'Não pode concluir'}**")
            dec_an = st.radio("A decisão de concluir está", list(_OPC), format_func=_OPC.get, horizontal=True,
                              index=list(_OPC).index(r.status if r.status in _OPC else "pendente"),
                              key=f"an_{r.id}")
            mot_an = st.text_area("Motivo (obrigatório se errada)", _txt(r.motivo), height=90, key=f"mot_{r.id}",
                                  help="Ex.: cliente ainda aguardava retorno; havia pendência na ficha...")
        with cb:
            if _tem_cqs:
                st.markdown(f"**2 · CQS** — a IA sugeriu: **{r.cqs_cod} – {_txt(r.cqs_nome)}**"
                            + (" (e secundários)" if _secs(r.cqs_secundarios) else ""))
                _opc_cqs = {k: v.replace("Correta", "Correto").replace("Errada", "Errado") for k, v in _OPC.items()}
                dec_cqs = st.radio("O CQS está", list(_opc_cqs), format_func=_opc_cqs.get, horizontal=True,
                                   index=list(_opc_cqs).index(r.status_cqs if r.status_cqs in _opc_cqs else "pendente"),
                                   key=f"cq_{r.id}",
                                   help="Avalie o principal e os secundários. Se a análise estiver errada, "
                                        "o CQS não é avaliado.")
            else:
                dec_cqs = None
                st.markdown("**2 · CQS** — " + ("a IA disse que não pode concluir: sem CQS para avaliar."
                                                if not _ia_sim else "a IA não sugeriu CQS."))
                if not _ia_sim:
                    st.caption("Se a análise estiver errada (podia concluir), informe abaixo qual seria o CQS certo.")
            _cat = catalogo_cqs()
            _atual = _txt(r.cqs_correto)
            if _cat:
                _cods = [""] + [c for c, _ in _cat]
                _nomes = dict(_cat)
                cqs_cert = st.selectbox("CQS certo (se o sugerido estiver errado)", _cods,
                                        index=_cods.index(_atual) if _atual in _cods else 0,
                                        format_func=lambda c: f"{c} – {_nomes.get(c, '')}" if c else "—",
                                        key=f"cc_{r.id}")
            else:
                cqs_cert = st.text_input("CQS certo (código, se o sugerido estiver errado)", _atual, key=f"cc_{r.id}")
            mot_cqs = st.text_area("Motivo do CQS (obrigatório se errado)", _txt(r.motivo_cqs), height=68,
                                   key=f"mcq_{r.id}",
                                   help="Ex.: foi atendimento remoto, não in loco; secundário do analista X deveria ser 59")
        salvar = st.form_submit_button("💾 Salvar revisão", type="primary")
    if salvar:
        _erros = []
        if dec_an == "rejeitada" and not mot_an.strip():
            _erros.append("Escreva o motivo da análise errada (a IA aprende com isso).")
        if dec_an == "rejeitada" and _ia_sim:
            dec_cqs = None          # não devia concluir: CQS não se avalia
        if dec_cqs == "rejeitada" and not (mot_cqs.strip() or str(cqs_cert).strip()):
            _erros.append("Informe o CQS certo ou o motivo do CQS errado.")
        if dec_cqs == "aceita":
            cqs_cert = ""
        if _erros:
            for _e in _erros:
                st.warning(_e)
        else:
            con.execute("UPDATE avaliacoes SET status=?, motivo=?, status_cqs=?, cqs_correto=?, motivo_cqs=?, "
                        "decidido_em=?, decidido_por=? WHERE id=?",
                        (dec_an, mot_an.strip(), dec_cqs, str(cqs_cert).strip() or None, mot_cqs.strip(),
                         datetime.now().isoformat(timespec="seconds"), USUARIO, int(r.id)))
            con.commit()
            fila_atual_erp.clear()   # o atendimento pode ter sido concluído no ERP: relê a fila
            st.rerun()

with st.expander("Últimas execuções"):
    try:
        st.dataframe(pd.read_sql("SELECT * FROM execucoes ORDER BY id DESC LIMIT 30", con),
                     width="stretch", hide_index=True)
    except Exception:
        st.write("Sem execuções registradas ainda.")
