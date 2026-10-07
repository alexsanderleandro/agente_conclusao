"""
Relatório PDF de um ciclo do agente (Modelo B - detalhado por atendimento).
Texto 8 / labels 9 / títulos 10, linhas zebradas, cabeçalho com data e hora do ciclo.

Uso: gerar_relatorio(lite, inicio_ciclo, pasta_saida, usuario, modelo) -> caminho do PDF (ou None)
"""
import json
import os
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

F, FB = "Helvetica", "Helvetica-Bold"
TXT = ParagraphStyle("txt", fontName=F, fontSize=8, leading=9.6)
TXT_R = ParagraphStyle("txtr", parent=TXT, alignment=TA_RIGHT)
LBL = ParagraphStyle("lbl", fontName=FB, fontSize=9, leading=10.6, textColor=colors.HexColor("#1F2A44"))
TIT = ParagraphStyle("tit", fontName=FB, fontSize=10, leading=12, textColor=colors.HexColor("#1F2A44"),
                     spaceBefore=4, spaceAfter=2)
KPI_N = ParagraphStyle("kn", fontName=FB, fontSize=10, leading=11, alignment=TA_CENTER)
KPI_L = ParagraphStyle("kl", fontName=F, fontSize=8, leading=9, alignment=TA_CENTER,
                       textColor=colors.HexColor("#5A6478"))
AZUL, ZEBRA, LINHA = colors.HexColor("#1F2A44"), colors.HexColor("#F1F3F7"), colors.HexColor("#C9CFDA")
VERDE, VERM = "#1E7B4A", "#B42318"


def _p(t, st=TXT, cor=None):
    t = escape("" if t is None else str(t)).replace("\n", "<br/>")
    return Paragraph(f'<font color="{cor}">{t}</font>' if cor else t, st)


def _br(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return iso or "-"


def _id(aid):
    """'1-1584430-0' -> '1584430/0' (empresa omitida)."""
    p = str(aid).split("-")
    return f"{p[1]}/{p[2]}" if len(p) == 3 else str(aid)


def _fontes(r):
    partes = ["Texto"]
    if r.get("qtd_imagens"):
        partes.append(f"{r['qtd_imagens']} imagem(ns)")
    n_aud = len([x for x in str(r.get("audios") or "").splitlines() if x.strip()])
    if n_aud:
        partes.append(f"{n_aud} áudio(s)")
    n_fic = len([x for x in str(r.get("fichas") or "").splitlines() if x.strip()])
    if n_fic:
        partes.append(f"{n_fic} ficha(s)")
    return " · ".join(partes)


NAO_CLASS = '<font color="#333333"><i>não classificado</i></font>'   # itálico, preto 80%


def _secundarios(js):
    """Markup do CQS secundário. Sem registro ou sem classificação -> 'não classificado' (itálico, cinza)."""
    try:
        lst = json.loads(js) if js else []
    except ValueError:
        lst = []
    partes = []
    for s in lst:
        if not s.get("cqs"):
            continue
        det = " ".join(x for x in (s.get("modalidade"), s.get("data")) if x)
        partes.append(escape(f"{s['cqs']} – {s.get('cqs_nome') or ''} · {s.get('analista') or '?'}"
                             + (f" ({det})" if det else "")))
    return "; ".join(partes) or NAO_CLASS


def gerar_relatorio(lite, inicio, pasta, usuario="", modelo=""):
    lite_row = lite.row_factory
    lite.row_factory = lambda c, r: {d[0]: r[i] for i, d in enumerate(c.description)}
    try:
        exe = lite.execute("SELECT * FROM execucoes WHERE inicio=? ORDER BY id DESC LIMIT 1", (inicio,)).fetchone()
        ats = lite.execute("SELECT * FROM avaliacoes WHERE ciclo_inicio=? ORDER BY pode_concluir DESC, id",
                           (inicio,)).fetchall()
    finally:
        lite.row_factory = lite_row
    if not ats:
        return None
    os.makedirs(pasta, exist_ok=True)
    caminho = os.path.join(pasta, f"Relatorio_ciclo_{datetime.fromisoformat(inicio):%Y-%m-%d_%H%M%S}.pdf")
    fim = (exe or {}).get("fim", "")
    n_sim = sum(1 for a in ats if a["pode_concluir"])

    def cab(cv, doc):
        w, h = doc.pagesize
        cv.saveState()
        cv.setFillColor(AZUL)
        cv.rect(0, h - 14 * mm, w, 14 * mm, stroke=0, fill=1)
        cv.setFillColor(colors.white)
        cv.setFont(FB, 10)
        cv.drawString(10 * mm, h - 6.5 * mm, "Relatório do ciclo de avaliação")
        cv.setFont(F, 8)
        cv.drawString(10 * mm, h - 11 * mm, f"Ciclo: {_br(inicio)}  →  {_br(fim)}   ·   Executado por: {usuario or '-'}"
                                            f"   ·   IA: {modelo or '-'}")
        cv.setFont(FB, 9)
        cv.drawRightString(w - 10 * mm, h - 6.5 * mm, "CEOSoftware · Revisão de conclusão")
        cv.setFillColor(colors.HexColor("#5A6478"))
        cv.setFont(F, 7.5)
        cv.drawString(10 * mm, 6 * mm, "Sugestões geradas por IA — a decisão de concluir é do analista. "
                                       "Dados pessoais mascarados no envio à IA.")
        cv.drawRightString(w - 10 * mm, 6 * mm, f"Página {doc.page}")
        cv.restoreState()

    doc = SimpleDocTemplate(caminho, pagesize=A4, leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=18 * mm, bottomMargin=11 * mm, title="Relatório do ciclo de avaliação")
    W = doc.width
    itens = [("Encontrados no ERP", (exe or {}).get("candidatos", "-")), ("Avaliados", len(ats)),
             ("Podem concluir", n_sim), ("Não autorizados", len(ats) - n_sim), ("Erros", (exe or {}).get("erros", 0)),
             ("Tokens (entrada/saída)", f"{(exe or {}).get('tokens_in', 0):,}/{(exe or {}).get('tokens_out', 0):,}".replace(",", "."))]
    kpi = Table([[_p(v, KPI_N) for _, v in itens], [_p(k, KPI_L) for k, _ in itens]], colWidths=[W / len(itens)] * len(itens))
    kpi.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, LINHA), ("LINEAFTER", (0, 0), (-2, -1), 0.5, LINHA),
                             ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8F9FB")),
                             ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                             ("TEXTCOLOR", (2, 0), (2, 0), colors.HexColor(VERDE)),
                             ("TEXTCOLOR", (3, 0), (3, 0), colors.HexColor(VERM))]))
    corpo = [_p("Resumo do ciclo", TIT), kpi, Spacer(1, 4), _p("Detalhe por atendimento", TIT)]

    for i, a in enumerate(ats):
        ok = bool(a["pode_concluir"])
        sel = f'<font color="{VERDE if ok else VERM}"><b>{"Sim" if ok else "Não"}</b></font>'
        topo = [_p(_id(a["atendimento_id"]), LBL), Paragraph(f"<b>{escape(str(a.get('cliente') or ''))}</b>", TXT),
                Paragraph(f"<b>Analista:</b> {escape(str(a.get('analista') or '-'))}   ·   <b>Concluir:</b> {sel}"
                          f"   ·   <b>Conf.</b> {float(a.get('confianca') or 0):.0%}", TXT_R), ""]
        if ok:
            cqs = (escape(f"{a['cqs_cod']} – {a.get('cqs_nome') or ''} ({a.get('cqs_pontos') or 0} pt)")
                   if a.get("cqs_cod") else NAO_CLASS)
            linhas = [topo,
                      [_p("CQS", LBL), Paragraph(cqs, TXT), _p("CQS sec.", LBL),
                       Paragraph(_secundarios(a.get("cqs_secundarios")), TXT)],
                      [_p("Resumo", LBL), _p(a.get("resumo")), "", ""],
                      [_p("Justificativa", LBL), _p(a.get("justificativa")), "", ""],
                      [_p("Fontes lidas", LBL), _p(_fontes(a)), "", ""]]
            spans = [(1, 2), (1, 3), (1, 4)]
        else:   # "Não": sem linha de CQS; a justificativa sobe para o lugar dela
            linhas = [topo,
                      [_p("Justificativa", LBL), _p(a.get("justificativa")), "", ""],
                      [_p("Resumo", LBL), _p(a.get("resumo")), "", ""],
                      [_p("Pendências", LBL), _p(a.get("pendencias") or "–", cor=VERM), "", ""],
                      [_p("Fontes lidas", LBL), _p(_fontes(a)), "", ""]]
            spans = [(1, 1), (1, 2), (1, 3), (1, 4)]
        t = Table(linhas, colWidths=[24 * mm, W * 0.42, 18 * mm, W - 42 * mm - W * 0.42])
        est = [("SPAN", (2, 0), (3, 0)), ("VALIGN", (0, 0), (-1, -1), "TOP"),
               ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
               ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
               ("LINEABOVE", (0, 0), (-1, 0), 0.8, AZUL), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDE2EC"))]
        est += [("SPAN", (c, r), (3, r)) for c, r in spans]
        if i % 2:
            est.append(("BACKGROUND", (0, 1), (-1, -1), ZEBRA))
        t.setStyle(TableStyle(est))
        corpo += [KeepTogether(t), Spacer(1, 3)]

    doc.build(corpo, onFirstPage=cab, onLaterPages=cab)
    return caminho
