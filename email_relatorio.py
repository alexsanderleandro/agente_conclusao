"""
Envio do relatório PDF do ciclo por e-mail (SMTP da Locaweb ou qualquer outro).

Config em [email] no config.ini. A senha da conta fica cifrada (mesma chave-mestra das chaves de IA),
cadastrada pelo painel. Porta 465 = SSL direto; 587/2525 = STARTTLS.
"""
import logging
import re
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from html import escape
from pathlib import Path

import cripto

log = logging.getLogger("agente")
_SIM = {"1", "sim", "s", "true", "yes", "on"}


def _bool(cfg, opc, padrao=False):
    v = cfg.get("email", opc, fallback=None)
    return padrao if v is None or not v.strip() else v.strip().lower() in _SIM


def _lista(txt):
    return [e.strip() for e in re.split(r"[;,\s]+", txt or "") if e.strip()]


def config_email(cfg, senha=None):
    if not cfg.has_section("email"):
        raise RuntimeError("Seção [email] não existe no config.ini")
    usuario = cfg.get("email", "usuario", fallback="").strip()
    c = {
        "host": cfg.get("email", "smtp_host", fallback="").strip(),
        "porta": cfg.getint("email", "porta", fallback=587),
        "usuario": usuario,
        "remetente": cfg.get("email", "remetente", fallback="").strip() or usuario,
        "para": _lista(cfg.get("email", "destinatarios", fallback="")),
        "cc": _lista(cfg.get("email", "copia", fallback="")),
        "assunto": cfg.get("email", "assunto", fallback="Revisão de conclusão – ciclo {data}"),
        "timeout": cfg.getint("email", "timeout_segundos", fallback=60),
    }
    falta = [n for n, v in (("smtp_host", c["host"]), ("usuario", c["usuario"]), ("destinatarios", c["para"])) if not v]
    if falta:
        raise RuntimeError(f"[email] incompleto: falta {', '.join(falta)}")
    c["senha"] = senha or cripto.carregar_senha_email(cfg)
    if not c["senha"]:
        raise RuntimeError("Senha da conta de e-mail não cadastrada (cadastre no painel)")
    return c


def _smtp(c):
    ctx = ssl.create_default_context()
    if c["porta"] == 465:
        s = smtplib.SMTP_SSL(c["host"], c["porta"], timeout=c["timeout"], context=ctx)
    else:
        s = smtplib.SMTP(c["host"], c["porta"], timeout=c["timeout"])
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
    s.login(c["usuario"], c["senha"])
    return s


def _enviar(c, assunto, html, texto, anexo=None):
    msg = EmailMessage()
    nome, end = parseaddr(c["remetente"])
    msg["From"] = formataddr((nome, end or c["usuario"]))
    msg["To"] = ", ".join(c["para"])
    if c["cc"]:
        msg["Cc"] = ", ".join(c["cc"])
    msg["Subject"] = assunto
    msg["Message-ID"] = make_msgid(domain=(end or c["usuario"]).split("@")[-1])
    msg.set_content(texto)
    msg.add_alternative(html, subtype="html")
    if anexo:
        p = Path(anexo)
        msg.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename=p.name)
    with _smtp(c) as s:
        s.send_message(msg)


# ---------------------------------------------------------------- conteúdo
def _br(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return iso or "-"


def _id(aid):
    p = str(aid).split("-")
    return f"{p[1]}/{p[2]}" if len(p) == 3 else str(aid)


def dados_ciclo(lite, inicio):
    """Números do ciclo e a lista dos que podem concluir, direto do avaliacoes.db."""
    cur = lite.execute("SELECT fim, candidatos, avaliados, erros FROM execucoes WHERE inicio=? "
                       "ORDER BY id DESC LIMIT 1", (inicio,))
    e = cur.fetchone() or (None, None, 0, 0)
    sims = lite.execute("SELECT atendimento_id, cliente, analista, cqs_cod, cqs_nome FROM avaliacoes "
                        "WHERE ciclo_inicio=? AND pode_concluir=1 ORDER BY id", (inicio,)).fetchall()
    n = lite.execute("SELECT COUNT(*) FROM avaliacoes WHERE ciclo_inicio=?", (inicio,)).fetchone()[0]
    return {"inicio": inicio, "fim": e[0], "candidatos": e[1], "avaliados": n or e[2] or 0,
            "erros": e[3] or 0, "sim": sims}


def inicio_pelo_pdf(pdf):
    """'Relatorio_ciclo_2026-10-07_190002.pdf' -> '2026-10-07T19:00:02'"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})", Path(pdf).stem)
    return f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}" if m else None


def _corpo(d, executado_por, max_linhas=40):
    n_sim = len(d["sim"])
    n_nao = d["avaliados"] - n_sim
    linhas = "".join(
        f'<tr style="background:{"#F1F3F7" if i % 2 else "#fff"}"><td>{escape(_id(a))}</td><td>{escape(str(c or ""))}</td>'
        f'<td>{escape(str(an or "-"))}</td><td>{escape(f"{cq} – {cn or ""}" if cq else "não classificado")}</td></tr>'
        for i, (a, c, an, cq, cn) in enumerate(d["sim"][:max_linhas]))
    extra = (f'<p style="font-size:12px;color:#5A6478">… e mais {n_sim - max_linhas} no PDF.</p>'
             if n_sim > max_linhas else "")
    tabela = (f'<table cellpadding="4" cellspacing="0" style="border-collapse:collapse;font-size:12px;'
              f'border:1px solid #C9CFDA"><tr style="background:#1F2A44;color:#fff"><th align="left">Atendimento</th>'
              f'<th align="left">Cliente</th><th align="left">Analista</th><th align="left">CQS</th></tr>{linhas}</table>{extra}'
              if n_sim else '<p style="font-size:12px">Nenhum atendimento liberado para conclusão neste ciclo.</p>')
    html = f"""<div style="font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#1F2A44">
<p><b>Revisão de conclusão de atendimentos</b><br>
Ciclo de {_br(d['inicio'])} · executado por {escape(executado_por or '-')}</p>
<p>Encontrados no ERP: <b>{d['candidatos'] if d['candidatos'] is not None else '-'}</b> ·
Avaliados: <b>{d['avaliados']}</b> ·
<span style="color:#1E7B4A">Podem concluir: <b>{n_sim}</b></span> ·
<span style="color:#B42318">Não autorizados: <b>{n_nao}</b></span> ·
Erros: <b>{d['erros']}</b></p>
<p><b>Podem concluir</b></p>{tabela}
<p style="font-size:12px">O relatório completo (resumo, justificativa, pendências e CQS) segue em anexo.</p>
<p style="font-size:11px;color:#5A6478">Sugestões geradas por IA — a decisão de concluir é do analista.
E-mail automático do agente de revisão; não responda.</p></div>"""
    texto = (f"Revisão de conclusão de atendimentos – ciclo de {_br(d['inicio'])}\n"
             f"Encontrados: {d['candidatos']} | Avaliados: {d['avaliados']} | Podem concluir: {n_sim} | "
             f"Não autorizados: {n_nao} | Erros: {d['erros']}\n\n"
             + "\n".join(f"- {_id(a)} {c} ({an}) CQS {cq or 'não classificado'}" for a, c, an, cq, _ in d["sim"])
             + "\n\nRelatório completo em anexo.")
    return html, texto


def enviar_relatorio(cfg, lite, pdf, executado_por=""):
    """Envia o PDF do ciclo. Levanta exceção se falhar (quem chama decide se registra ou mostra)."""
    c = config_email(cfg)
    inicio = inicio_pelo_pdf(pdf)
    d = dados_ciclo(lite, inicio)
    html, texto = _corpo(d, executado_por)
    assunto = c["assunto"].replace("{data}", _br(inicio)).replace("{concluir}", str(len(d["sim"])))
    _enviar(c, assunto, html, texto, pdf)
    return c["para"] + c["cc"]


def enviar_teste(cfg, senha=None):
    """E-mail de teste (sem anexo). senha: testa uma senha ainda não gravada."""
    c = config_email(cfg, senha)
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    _enviar(c, "Teste – agente de revisão de conclusão",
            f'<p style="font-family:Arial">Teste de envio do agente de revisão ({agora}). Se chegou, está tudo certo.</p>',
            f"Teste de envio do agente de revisão ({agora}).")
    return c["para"] + c["cc"]
