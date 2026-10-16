"""
Histórico real de revisões no ERP como gabarito de aprendizado.

Cada iteração 26 ("enviado para revisão") já decidida vira um caso:
  - próxima iteração com o texto "Conclusão revisada."  -> CONCLUÍDO (e o CQS lançado depois)
  - qualquer outra próxima iteração                      -> DEVOLVIDO ao analista (o texto dela é o motivo)
Fica numa tabela local (historico_revisoes) e é importado de forma incremental a cada ciclo.

Uso manual:
  python historico.py --importar --meses 12     carga inicial (ou recarga)
  python historico.py --acerto                  compara as sugestões do agente com o que aconteceu no ERP
"""
import argparse
import logging
import re
import unicodedata
from datetime import datetime, timedelta

log = logging.getLogger("agente")

SQL_REVISOES = """
SELECT R.CodEmpresa AS codempresa, R.NumAtendimento AS numatendimento, R.Desdobramento AS desdobramento,
       R.NumIteracao AS num_iter, R.DataAlteracao AS data,
       A.AssuntoAtendimento AS assunto, C.NomeCliente AS cliente,
       COALESCE(UA.NomeUsuario, CAST(A.CodUsuario AS VARCHAR(20))) AS analista,
       SUBSTRING(P.TextoIteracao, 1, 20000) AS texto_proxima, P.DataAlteracao AS data_proxima, P.CodTipoIteracao AS tipo_proxima,
       COALESCE(UP.NomeUsuario, CAST(P.CodUsuario AS VARCHAR(20))) AS revisor,
       SUBSTRING(ANT.TextoIteracao, 1, 30000) AS texto_anterior
FROM AtendimentoIteracao R WITH (NOLOCK)
JOIN Atendimento A WITH (NOLOCK)
  ON A.CodEmpresa = R.CodEmpresa AND A.NumAtendimento = R.NumAtendimento AND A.Desdobramento = R.Desdobramento
LEFT JOIN Clientes C WITH (NOLOCK) ON C.CodCliente = A.CodCliente
LEFT JOIN Usuarios UA WITH (NOLOCK) ON UA.CodUsuario = A.CodUsuario
CROSS APPLY (SELECT TOP 1 X.TextoIteracao, X.DataAlteracao, X.CodUsuario, X.CodTipoIteracao FROM AtendimentoIteracao X WITH (NOLOCK)
             WHERE X.CodEmpresa = R.CodEmpresa AND X.NumAtendimento = R.NumAtendimento
               AND X.Desdobramento = R.Desdobramento AND X.NumIteracao > R.NumIteracao
             ORDER BY X.NumIteracao) P
LEFT JOIN Usuarios UP WITH (NOLOCK) ON UP.CodUsuario = P.CodUsuario
OUTER APPLY (SELECT TOP 1 Y.TextoIteracao FROM AtendimentoIteracao Y WITH (NOLOCK)
             WHERE Y.CodEmpresa = R.CodEmpresa AND Y.NumAtendimento = R.NumAtendimento
               AND Y.Desdobramento = R.Desdobramento AND Y.NumIteracao < R.NumIteracao AND Y.CodTipoIteracao <> 26
             ORDER BY Y.NumIteracao DESC) ANT
WHERE R.CodTipoIteracao = 26 AND R.DataAlteracao >= ?
"""

SQL_CQS = """
SELECT A.NumAtendimento AS numatendimento, A.Desdobramento AS desdobramento,
       C.CodTipoRegistro AS cod, C.DataRegistro AS data
FROM CQSRegistros C WITH (NOLOCK)
JOIN CQSRegistrosAtendimento A WITH (NOLOCK) ON C.NumRegistroCQS = A.NumRegistroCQS
WHERE C.DataRegistro >= ?
"""


# textos (sem acento, minúsculos) da próxima iteração que NÃO são decisão do revisor
NAO_DECISAO = (r"^atendimento enviado para revis", r"^ticket \d+", r"^atendimento auditado")   # auditado = auditoria interna do gerente


def nao_decisao(cfg):
    v = cfg.get("aprendizado", "nao_decisao", fallback="").strip()
    return tuple(x.strip() for x in v.splitlines() if x.strip()) or NAO_DECISAO


def codigos_ignorar(cfg):
    return {c.strip() for c in cfg.get("cqs", "ignorar", fallback="").split(",") if c.strip()}


def criar_tabela(lite):
    lite.execute("""CREATE TABLE IF NOT EXISTS historico_revisoes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        atendimento_id TEXT, num_iter INTEGER, data TEXT, assunto TEXT, cliente TEXT, analista TEXT,
        desfecho TEXT,            -- 'concluido' | 'devolvido'
        data_desfecho TEXT, revisor TEXT,
        texto_caso TEXT,          -- último registro antes do pedido de revisão (texto puro)
        motivo TEXT,              -- texto do revisor quando devolveu
        cqs TEXT,                 -- códigos de CQS lançados na conclusão (ex.: '55,59'), só os das listas [cqs]
        cqs_todos TEXT,           -- todos os códigos lançados (diagnóstico)
        tipo_proxima TEXT,        -- tipo da iteração que decidiu (diagnóstico)
        importado_em TEXT,
        UNIQUE(atendimento_id, num_iter))""")
    for col in ("cqs_todos TEXT", "tipo_proxima TEXT"):
        try:
            lite.execute(f"ALTER TABLE historico_revisoes ADD COLUMN {col}")
        except Exception:
            pass
    lite.execute("CREATE INDEX IF NOT EXISTS ix_hist_at ON historico_revisoes(atendimento_id)")
    lite.commit()


def _sem_acento(t):
    t = unicodedata.normalize("NFKD", str(t or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def eh_conclusao(texto_limpo):
    return "conclusao revisada" in _sem_acento(texto_limpo)


def _dt(v):
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v)[:19])
    except Exception:
        return None


def montar_casos(revisoes, cqs_rows, limpar, codigos_validos=None, max_chars=1500, cqs_desde=None, ignorar=(),
                 nao_decisao=NAO_DECISAO):
    """Transforma as linhas do ERP em casos (função pura, testável sem banco)."""
    cqs_por_at = {}
    for c in cqs_rows:
        cqs_por_at.setdefault((str(c["numatendimento"]), str(c["desdobramento"])), []).append(c)
    casos = []
    for r in revisoes:
        aid = f'{r["codempresa"]}-{r["numatendimento"]}-{r["desdobramento"]}'
        prox = limpar(r.get("texto_proxima"))
        # a próxima iteração não é decisão do revisor (reenvio para revisão, novo ticket do cliente...): ignora
        if str(r.get("tipo_proxima") or "") == "26" or any(re.search(rx, _sem_acento(prox)) for rx in nao_decisao):
            continue
        concl = eh_conclusao(prox)
        d26, dprox = _dt(r.get("data")), _dt(r.get("data_proxima"))
        cqs, todos = [], []
        # CQS só vale a partir da data em que as regras atuais passaram a valer ([aprendizado] cqs_desde)
        if concl and d26 and (not cqs_desde or d26 >= cqs_desde):
            fim = (dprox or d26) + timedelta(days=3)
            for c in cqs_por_at.get((str(r["numatendimento"]), str(r["desdobramento"])), []):
                dc = _dt(c.get("data"))
                cod = str(c["cod"])
                if cod in ignorar:          # ex.: 66 = lixeira dos excluídos, 70 = antigo "atendimento do gerente"
                    continue
                if dc and d26.date() <= dc.date() <= fim.date():
                    if cod not in todos:
                        todos.append(cod)
                    if (not codigos_validos or cod in codigos_validos) and cod not in cqs:
                        cqs.append(cod)
        casos.append({
            "atendimento_id": aid, "num_iter": int(r["num_iter"]),
            "data": d26.isoformat(timespec="seconds") if d26 else str(r.get("data")),
            "assunto": limpar(r.get("assunto")), "cliente": limpar(r.get("cliente")), "analista": limpar(r.get("analista")),
            "desfecho": "concluido" if concl else "devolvido",
            "data_desfecho": dprox.isoformat(timespec="seconds") if dprox else None,
            "revisor": limpar(r.get("revisor")),
            "texto_caso": limpar(r.get("texto_anterior"))[-max_chars:],
            "motivo": "" if concl else prox[:max_chars],
            "cqs": ",".join(cqs), "cqs_todos": ",".join(todos), "tipo_proxima": str(r.get("tipo_proxima") or ""),
        })
    return casos


def _seguro(v):
    """Emoji do RichText chega como par de 'surrogates' (ou metade dele, se o corte caiu no meio):
    junta os pares válidos e descarta os soltos, senão o SQLite recusa o texto."""
    if not isinstance(v, str):
        return v
    try:
        v = v.encode("utf-16", "surrogatepass").decode("utf-16", "ignore")
    except UnicodeError:
        pass
    return re.sub(r"[\ud800-\udfff]", "", v)


def gravar(lite, casos):
    agora = datetime.now().isoformat(timespec="seconds")
    n = 0
    for c in casos:
        c = {k: _seguro(v) for k, v in c.items()}
        cur = lite.execute(
            "INSERT OR IGNORE INTO historico_revisoes(atendimento_id,num_iter,data,assunto,cliente,analista,desfecho,"
            "data_desfecho,revisor,texto_caso,motivo,cqs,cqs_todos,tipo_proxima,importado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["atendimento_id"], c["num_iter"], c["data"], c["assunto"], c["cliente"], c["analista"], c["desfecho"],
             c["data_desfecho"], c["revisor"], c["texto_caso"], c["motivo"], c["cqs"], c.get("cqs_todos", ""), c.get("tipo_proxima", ""), agora))
        n += cur.rowcount
    lite.commit()
    return n


def importar(cfg, conn, lite, consultar, limpar, codigos_validos=None, meses=None):
    """Importa as revisões decididas desde a última importação (ou dos últimos N meses na 1ª vez)."""
    criar_tabela(lite)
    ult = lite.execute("SELECT MAX(data) FROM historico_revisoes").fetchone()[0]
    if meses is None and ult:
        desde = _dt(ult) - timedelta(days=7)          # folga: revisões que ainda não tinham desfecho
    else:
        desde = datetime.now() - timedelta(days=30 * (meses or cfg.getint("aprendizado", "meses_historico", fallback=6)))
    cqs_desde = _dt(cfg.get("aprendizado", "cqs_desde", fallback="").strip() or None)
    sql_r = cfg.get("queries", "historico_revisoes", fallback=SQL_REVISOES)
    sql_c = cfg.get("queries", "historico_cqs", fallback=SQL_CQS)
    revisoes = consultar(conn, sql_r, (desde,))
    cqs_rows = consultar(conn, sql_c, (max(desde, cqs_desde) if cqs_desde else desde,))
    novos = gravar(lite, montar_casos(revisoes, cqs_rows, limpar, codigos_validos, cqs_desde=cqs_desde,
                                      ignorar=codigos_ignorar(cfg), nao_decisao=nao_decisao(cfg)))
    log.info("Histórico de revisões: %d novo(s) caso(s) desde %s (%d no total)", novos, desde.date(),
             lite.execute("SELECT COUNT(*) FROM historico_revisoes").fetchone()[0])
    return novos


def acerto_real(lite, desde=None, versao=None):
    """Sugestão do agente x o que o revisor fez no ERP (1ª revisão decidida depois da avaliação).
    desde = só avaliações feitas a partir dessa data (ISO); versao = só as feitas com essa versão do prompt."""
    criar_tabela(lite)
    filtro, par = "", []
    if desde:
        filtro += " AND a.avaliado_em >= ?"
        par.append(desde)
    if versao:
        filtro += " AND a.versao_prompt = ?"
        par.append(versao)
    rows = lite.execute(f"""
        SELECT a.atendimento_id, a.pode_concluir, a.cqs_cod,
               (SELECT h.desfecho FROM historico_revisoes h WHERE h.atendimento_id = a.atendimento_id
                  AND h.data_desfecho >= a.avaliado_em ORDER BY h.data_desfecho LIMIT 1),
               (SELECT h.cqs FROM historico_revisoes h WHERE h.atendimento_id = a.atendimento_id
                  AND h.data_desfecho >= a.avaliado_em ORDER BY h.data_desfecho LIMIT 1)
        FROM avaliacoes a WHERE a.id IN (SELECT MAX(id) FROM avaliacoes GROUP BY atendimento_id){filtro}""",
                        par).fetchall()
    tot = ok = cqs_tot = cqs_ok = 0
    for _, pode, cod, desf, cqs in rows:
        if not desf:
            continue
        tot += 1
        ok += int(bool(pode) == (desf == "concluido"))
        if pode and desf == "concluido" and cod and cqs:
            cqs_tot += 1
            cqs_ok += int(str(cod) in cqs.split(","))
    return {"comparados": tot, "acerto_analise": ok / tot if tot else None,
            "comparados_cqs": cqs_tot, "acerto_cqs": cqs_ok / cqs_tot if cqs_tot else None}


def _fmt_acerto(r):
    pct = lambda x: f"{x:.0%}" if x is not None else "-"
    return f"análise {pct(r['acerto_analise'])} em {r['comparados']}, CQS {pct(r['acerto_cqs'])} em {r['comparados_cqs']}"


def texto_acerto(lite, versao=None, dias=(7, 30)):
    """Acerto real no total, nos últimos N dias e na versão atual do prompt (o total mistura prompts antigos)."""
    tot = acerto_real(lite)
    if not tot["comparados"]:
        return ""
    partes = [f"total {_fmt_acerto(tot)}"]
    for d in dias:
        r = acerto_real(lite, desde=(datetime.now() - timedelta(days=d)).isoformat(timespec="seconds"))
        partes.append(f"últimos {d} dias {_fmt_acerto(r)}" if r["comparados"] else f"últimos {d} dias sem casos")
    if versao:
        r = acerto_real(lite, versao=versao)
        partes.append(f"versão atual {_fmt_acerto(r)}" if r["comparados"] else "versão atual ainda sem casos decididos")
    return " | ".join(partes)


_CONF = re.compile(r"(?i)confirm\w*|retorno do cliente|valida[çc][ãa]o")


def so_confirmacao(pendencias):
    """True se todas as pendências listadas pela IA forem sobre confirmação do cliente."""
    partes = [p.strip() for p in re.split(r"[|;]\s*|\n", str(pendencias or "")) if p.strip()]
    partes = [re.sub(r"^(Texto|Revisão|Imagem|Áudio|Ficha)\s*:\s*", "", p, flags=re.I) for p in partes]
    return bool(partes) and all(_CONF.search(p) for p in partes)


def concluidos_sem_confirmacao(lite, desde):
    """Atendimentos que a IA barrou SÓ por falta de confirmação do cliente e o revisor concluiu no ERP mesmo assim,
    com a conclusão em/após 'desde' (ISO). Só entram os que o agente avaliou antes da decisão do revisor.
    -> [(atendimento_id, cliente, analista, revisor, data_desfecho)]"""
    criar_tabela(lite)
    rows = lite.execute("""
        SELECT a.atendimento_id, a.cliente, a.analista, h.revisor, h.data_desfecho, a.pendencias
        FROM avaliacoes a
        JOIN historico_revisoes h ON h.id = (SELECT h2.id FROM historico_revisoes h2
              WHERE h2.atendimento_id = a.atendimento_id AND h2.data_desfecho >= a.avaliado_em
              ORDER BY h2.data_desfecho LIMIT 1)
        WHERE a.id IN (SELECT MAX(id) FROM avaliacoes GROUP BY atendimento_id)
          AND a.pode_concluir = 0 AND h.desfecho = 'concluido' AND h.data_desfecho >= ?
        ORDER BY h.data_desfecho""", (desde,)).fetchall()
    return [tuple(r[:5]) for r in rows if so_confirmacao(r[5])]


def indicador_sem_confirmacao(lite, inicio_ciclo, dias=30):
    """Para o relatório do ciclo: os novos desde o ciclo anterior (lista) e o total dos últimos N dias."""
    try:
        ant = lite.execute("SELECT MAX(inicio) FROM execucoes WHERE inicio < ?", (inicio_ciclo,)).fetchone()[0]
    except Exception:
        ant = None
    ref = _dt(inicio_ciclo) or datetime.now()
    desde = ant or (ref - timedelta(days=7)).isoformat(timespec="seconds")
    periodo = concluidos_sem_confirmacao(lite, (ref - timedelta(days=dias)).isoformat(timespec="seconds"))
    return {"novos": concluidos_sem_confirmacao(lite, desde), "desde": desde,
            "total_periodo": len(periodo), "dias": dias}


def diagnostico(lite, max_erros=15, ignorar=()):
    """Texto com a matriz de acertos, os erros e a comparação de CQS sugerido x lançado."""
    criar_tabela(lite)
    rows = lite.execute("""
        SELECT a.atendimento_id, a.pode_concluir, a.cqs_cod, a.justificativa, h.desfecho, h.cqs, h.cqs_todos, h.motivo
        FROM avaliacoes a
        JOIN historico_revisoes h ON h.id = (SELECT h2.id FROM historico_revisoes h2
              WHERE h2.atendimento_id = a.atendimento_id AND h2.data_desfecho >= a.avaliado_em
              ORDER BY h2.data_desfecho LIMIT 1)
        WHERE a.id IN (SELECT MAX(id) FROM avaliacoes GROUP BY atendimento_id)""").fetchall()
    out = []
    m = {(s, d): 0 for s in (1, 0) for d in ("concluido", "devolvido")}
    for _, pode, *_r in rows:
        m[(1 if pode else 0, _r[2])] += 1
    out.append("MATRIZ (IA x revisor no ERP)")
    out.append(f"  IA Sim  -> concluído: {m[(1,'concluido')]:4d}   devolvido: {m[(1,'devolvido')]:4d}")
    out.append(f"  IA Não  -> concluído: {m[(0,'concluido')]:4d}   devolvido: {m[(0,'devolvido')]:4d}")
    erros = [r for r in rows if bool(r[1]) != (r[4] == "concluido")]
    if erros:
        out.append(f"\nERROS DA ANÁLISE ({len(erros)}; mostrando até {max_erros})")
        for aid, pode, _, just, desf, _, _, mot in erros[:max_erros]:
            out.append(f"  {aid}: IA {'Sim' if pode else 'Não'} / ERP {desf} | IA: {str(just or '')[:90]}"
                       + (f" | revisor: {str(mot or '')[:90]}" if mot else ""))
    pares = {}
    for _, pode, cod, _, desf, cqs, todos, _ in rows:
        if pode and desf == "concluido" and cod:
            pares[(str(cod), todos or "(nenhum)")] = pares.get((str(cod), todos or "(nenhum)"), 0) + 1
    if pares:
        out.append("\nCQS SUGERIDO -> LANÇADO NO ERP (todos os códigos)")
        for (cod, todos), n in sorted(pares.items(), key=lambda x: -x[1])[:15]:
            out.append(f"  {cod} -> {todos}: {n}")
    _ini = lite.execute("SELECT MIN(data) FROM historico_revisoes WHERE COALESCE(cqs_todos, cqs, '')<>''").fetchone()[0] or ""
    out.append(f"\nCQS no histórico a partir de: {str(_ini)[:10] or '-'}")
    tot = lite.execute("SELECT COUNT(*) FROM historico_revisoes WHERE desfecho='concluido' AND data >= ?", (_ini,)).fetchone()[0]
    sem = lite.execute("SELECT COUNT(*) FROM historico_revisoes WHERE desfecho='concluido' AND data >= ? "
                       "AND COALESCE(cqs_todos, cqs, '')=''", (_ini,)).fetchone()[0]
    freq = {}
    for (t,) in lite.execute("SELECT COALESCE(cqs_todos, cqs) FROM historico_revisoes WHERE desfecho='concluido'"):
        for c in (t or "").split(","):
            if c and c not in ignorar:
                freq[c] = freq.get(c, 0) + 1
    out.append(f"HISTÓRICO desde então: {tot} concluídos, {sem} sem CQS encontrado na janela de 3 dias")
    out.append("  códigos mais lançados: " + ", ".join(f"{c} ({n})" for c, n in sorted(freq.items(), key=lambda x: -x[1])[:12]))
    tipos = lite.execute("SELECT desfecho, tipo_proxima, COUNT(*) FROM historico_revisoes "
                         "GROUP BY desfecho, tipo_proxima ORDER BY desfecho, 3 DESC").fetchall()
    if tipos:
        out.append("\nTIPO DA ITERAÇÃO QUE DECIDIU (código: quantidade)")
        for d in ("concluido", "devolvido"):
            out.append(f"  {d}: " + ", ".join(f"{t or '?'}: {n}" for dd, t, n in tipos if dd == d))
    return "\n".join(out)


if __name__ == "__main__":
    import agente
    ap = argparse.ArgumentParser()
    ap.add_argument("--importar", action="store_true")
    ap.add_argument("--meses", type=int, default=None)
    ap.add_argument("--acerto", action="store_true")
    ap.add_argument("--recarregar", action="store_true", help="apaga o histórico local antes de importar")
    a = ap.parse_args()
    cfg = agente.ler_cfg()
    lite = agente.init_sqlite(agente.BASE / cfg["saida"]["sqlite"])
    if a.recarregar:
        criar_tabela(lite)
        lite.execute("DELETE FROM historico_revisoes")
        lite.commit()
    if a.importar:
        import pyodbc
        conn = pyodbc.connect(cfg["banco"]["conn_string"], timeout=30)
        try:
            validos = set(agente.codigos_cqs(cfg, "principal") or []) | set(agente.codigos_cqs(cfg, "secundario") or [])
            importar(cfg, conn, lite, agente.consultar, agente.limpar, validos or None, a.meses or 6)
        finally:
            conn.close()
        for d, n in lite.execute("SELECT desfecho, COUNT(*) FROM historico_revisoes GROUP BY desfecho"):
            print(f"  {d}: {n}")
    if a.acerto:
        print("Obs.: 'acerto' aqui = concordância com o revisor; quando a IA nega e o revisor concluiu, "
              "pode ser o revisor que deixou pendência passar.")
        print("Comparado com o ERP: " + (texto_acerto(lite) or "nenhum caso decidido ainda").replace(" | ", "\n  "))
        vers = lite.execute("SELECT versao_prompt, MIN(avaliado_em), COUNT(*) FROM avaliacoes "
                            "WHERE versao_prompt IS NOT NULL GROUP BY versao_prompt ORDER BY 2").fetchall()
        if vers:
            print("Por versão do prompt (a partir de quando foi usada):")
            for v, ini, n in vers:
                r = acerto_real(lite, versao=v)
                print(f"  {v} desde {str(ini)[:16]} · {n} avaliações · "
                      + (_fmt_acerto(r) if r["comparados"] else "sem casos decididos no ERP ainda"))
        print()
        print(diagnostico(lite, ignorar=codigos_ignorar(cfg)))
