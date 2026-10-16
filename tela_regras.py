"""
Tela do painel para manter as regras de revisão que entram no prompt do agente.

- Aprovadas: edita regras_aprovadas.txt (cópia de segurança em regras_bkp/ a cada gravação + log de quem alterou).
- Sugestões da IA: lista as regras de regras_sugeridas.txt com caixa de marcar; as marcadas vão para as aprovadas.
  O botão "Gerar novas sugestões" roda o regras.py com a chave de IA do usuário logado.
O agente relê o arquivo a cada avaliação: vale a partir da próxima, sem reiniciar nada.
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

import similares

BASE = Path(__file__).parent
APROVADAS = BASE / "regras_aprovadas.txt"
SUGERIDAS = BASE / "regras_sugeridas.txt"
BKP = BASE / "regras_bkp"
LOG = BKP / "alteracoes.log"

_REGRA = re.compile(r"^\s*(\d+[a-z]?[.)]|-)\s+\S")
# seção das situações que o gerente aceita concluir (o agente apresenta com instrução própria; fica sempre por último)
_SECAO_PODE = re.compile(r"(?im)^[ \t]*QUANDO PODE CONCLUIR\b.*$")


def _ler(p):
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _quando(p):
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
    except OSError:
        return "-"


def contar(txt):
    return sum(1 for l in txt.splitlines() if not l.lstrip().startswith("#") and _REGRA.match(l))


def salvar(txt, usuario, acao):
    """Grava as aprovadas guardando a versão anterior em regras_bkp/ e registrando quem alterou."""
    BKP.mkdir(exist_ok=True)
    agora = datetime.now()
    if APROVADAS.exists():
        shutil.copy2(APROVADAS, BKP / f"regras_aprovadas_{agora:%Y%m%d_%H%M%S}.txt")
    tmp = APROVADAS.with_name("regras_aprovadas.tmp")
    tmp.write_text(txt.replace("\r\n", "\n").rstrip() + "\n", encoding="utf-8")
    os.replace(tmp, APROVADAS)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{agora:%Y-%m-%d %H:%M:%S}\t{usuario}\t{acao}\t{contar(txt)} regras\n")


def ultima_alteracao():
    try:
        d, u, *_ = LOG.read_text(encoding="utf-8").strip().splitlines()[-1].split("\t")
        return f"{d[8:10]}/{d[5:7]}/{d[:4]} {d[11:16]} por {u}"
    except (OSError, IndexError, ValueError):
        return _quando(APROVADAS)


def sugestoes(txt):
    """[(tema, regra)] das linhas '- ...' de regras_sugeridas.txt (o tema é a última linha sem '-')."""
    tema, out = "", []
    for l in txt.splitlines():
        s = l.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- "):
            out.append((tema, s[2:].strip()))
        else:
            tema = s.rstrip(":")
    return out


def _limpa(regra):
    """Tira o '(N casos) Ex.: ...' do fim: na lista aprovada fica só a regra."""
    return re.sub(r"\s*\(\d+\s*casos?\).*$", "", regra, flags=re.I).strip() or regra


def adicionar(txt, marcadas, usuario):
    """Junta as sugestões marcadas [(tema, regra)] às aprovadas: as 'PODE CONCLUIR' na seção QUANDO PODE CONCLUIR
    (sempre a última), as demais num bloco NOVAS antes dela."""
    m = _SECAO_PODE.search(txt)
    neg, pos = (txt[:m.start()].rstrip(), txt[m.end():].strip()) if m else (txt.rstrip(), "")
    nao = [r for t, r in marcadas if not t.upper().startswith("PODE CONCLUIR")]
    sim = [r for t, r in marcadas if t.upper().startswith("PODE CONCLUIR")]
    if nao:
        neg += f"\n\nNOVAS (aprovadas em {datetime.now():%d/%m/%Y} por {usuario})\n" + "\n".join(f"- {r}" for r in nao)
    if sim:
        pos = (pos + "\n" if pos else "") + "\n".join(f"- {r}" for r in sim)
    return neg + (f"\n\nQUANDO PODE CONCLUIR\n{pos}" if pos else "")


def parecidas(txt, sugestoes_, minimo=0.35):
    """Para cada sugestão, a regra aprovada mais parecida (busca local por palavras), ou ''. Evita aprovar de novo
    o que já existe com outras palavras."""
    existentes = [l.strip() for l in txt.splitlines() if not l.lstrip().startswith("#") and _REGRA.match(l)]
    if not existentes:
        return [""] * len(sugestoes_)
    idx = similares.Indice(list(enumerate(existentes)))
    out = []
    for _, regra in sugestoes_:
        r = idx.busca(_limpa(regra), k=1, minimo=minimo)
        out.append(existentes[r[0][1]] if r else "")
    return out


def gerar_sugestoes(usuario):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        p = subprocess.run([sys.executable, str(BASE / "regras.py"), "--usuario", usuario], cwd=str(BASE), env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    except subprocess.TimeoutExpired:
        return False, "Passou de 15 minutos e foi interrompido."
    saida = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode != 0 or "Gravado:" not in saida:
        return False, "\n".join(saida.splitlines()[-10:]) or "Sem saída do regras.py."
    return True, saida.splitlines()[-1]


def mostrar(usuario):
    txt = _ler(APROVADAS)
    sug = sugestoes(_ler(SUGERIDAS))
    versao = f"{APROVADAS.stat().st_mtime_ns if APROVADAS.exists() else 0}"
    with st.expander("📋 Regras de revisão da IA"):
        if st.session_state.get("msg_regras"):
            _t, _m = st.session_state.pop("msg_regras")
            (st.success if _t == "ok" else st.error)(_m)
        t1, t2 = st.tabs([f"Aprovadas ({contar(txt)})", f"Sugestões da IA ({len(sug)})"])
        with t1:
            st.caption("Estas regras entram no prompt do agente. Uma regra por linha; títulos e linhas com # "
                       "são livres. Vale a partir da próxima avaliação, sem reiniciar.")
            novo = st.text_area("Regras aprovadas", txt, height=420, key=f"regras_txt_{versao}",
                                label_visibility="collapsed")
            c1, c2 = st.columns([1, 3])
            if c1.button("💾 Salvar regras", key="regras_salvar"):
                if novo.strip() == txt.strip():
                    st.info("Nada mudou nas regras.")
                else:
                    salvar(novo, usuario, "editou")
                    st.session_state["msg_regras"] = ("ok", f"Regras salvas ({contar(novo)}).")
                    st.rerun()
            c2.caption(f"Última alteração: {ultima_alteracao()} · versões anteriores em `regras_bkp/`")
        with t2:
            if sug:
                st.caption(f"Geradas em {_quando(SUGERIDAS)} a partir das devoluções reais do ERP e dos \"Não\" do agente "
                           "que você rejeitou. Marque as que valem e adicione: as de devolução vão para um bloco NOVAS e as "
                           "\"PODE CONCLUIR\" para a seção QUANDO PODE CONCLUIR, onde você pode ajustar o texto. "
                           "\"≈ parecida com\" avisa quando já existe uma regra aprovada com outras palavras.")
                base = txt.lower()
                perto = parecidas(txt, sug)
                with st.form("form_sugestoes", border=False):
                    marcadas, tema_ant = [], None
                    for i, (tema, regra) in enumerate(sug):
                        if tema != tema_ant:
                            st.markdown(f"**{tema or 'Sem tema'}**")
                            tema_ant = tema
                        ja = _limpa(regra).lower() in base
                        aviso = ("  ✓ já aprovada" if ja else
                                 f"  ≈ parecida com: \"{perto[i][:90]}\"" if perto[i] else "")
                        if st.checkbox(regra + aviso, key=f"sug_{versao}_{i}", disabled=ja):
                            marcadas.append((tema, _limpa(regra)))
                    add = st.form_submit_button("➕ Adicionar marcadas às aprovadas")
                if add:
                    if marcadas:
                        salvar(adicionar(txt, marcadas, usuario), usuario, f"adicionou {len(marcadas)} sugestão(ões)")
                        st.session_state["msg_regras"] = ("ok", f"{len(marcadas)} regra(s) adicionada(s) às aprovadas.")
                        st.rerun()
                    else:
                        st.warning("Nenhuma regra marcada.")
            else:
                st.info("Ainda não há sugestões. Clique em **Gerar novas sugestões**.")
            g1, g2 = st.columns([1, 3])
            if g1.button("🤖 Gerar novas sugestões", key="regras_gerar"):
                with st.spinner("Lendo as devoluções e os \"Não\" rejeitados e pedindo à IA para resumir em regras..."):
                    ok, msg = gerar_sugestoes(usuario)
                st.session_state["msg_regras"] = ("ok" if ok else "erro", msg if ok else f"Não consegui gerar: {msg}")
                st.rerun()
            g2.caption("Usa até 400 motivos de devolução do histórico e os \"Não\" que você rejeitou no painel (mascarados), "
                       "em até duas chamadas à sua chave de IA. Não mexe nas aprovadas. Rode a cada 1 ou 2 meses, "
                       "depois do `historico.py --importar`.")
