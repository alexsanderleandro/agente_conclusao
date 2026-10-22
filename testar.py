"""
Teste do agente com casos REAIS já decididos (conjunto fixo, para comparar antes x depois).

Usa a MESMA decisão do agente (agente.decidir: IA + travas do código + escolha do CQS), com as iterações até o
ponto em que o caso foi avaliado. Não grava nada em avaliacoes; os resultados ficam em teste_resultados.

  python testar.py                     40 casos do ERP (20 devolvidos + 20 concluídos com CQS), sempre os mesmos
  python testar.py --painel            casos revisados pelo gerente no painel (verdade = decisão do gerente)
  python testar.py --painel --n 90     tamanho: 2/3 "Não" revisados (metade aceitos, metade rejeitados) + 1/3 "Sim"
  python testar.py --com-print         casos revisados no painel com print de conversa do WhatsApp (pelas cores)
  python testar.py --ids 437,386       só essas avaliações do painel (id da tabela avaliacoes)
  python testar.py --ocr-simples       prints lidos com o OCR comum, sem separar as falas (para comparar)
  python testar.py --repetir RODADA    os mesmos casos de uma rodada anterior (comparação justa)
  python testar.py --regras ARQUIVO    outro arquivo de regras (proposta), sem gravar nas aprovadas
  python testar.py --listar            só mostra os casos escolhidos, sem chamar a IA
  python testar.py --comparar A B      compara duas rodadas já feitas (sem IA)
  python testar.py --usuario alex      chave de IA de quem roda (padrão: a mesma regra do agente)
  python testar.py --modelo gpt-4.1    outro modelo · --sem-regras · --com-revisao [--modelo-revisao X]

Obs.: fichas de visita e áudios da pasta do cliente não entram no teste (só texto e imagens das iterações).
No painel, a situação das RDMs é a gravada na avaliação; nos casos do ERP, as RDMs não entram.
"""
import argparse
import random
import re
from datetime import datetime

import pyodbc

import agente
import historico as hist
import llm
import rdm
from privacidade import eh_print_conversa, extrair_pngs_rtf

# iterações até o pedido de revisão (mesmas colunas da query [queries] interacoes do agente)
SQL_INTER = """
SELECT AI.DataAlteracao AS data,
       COALESCE(U.NomeUsuario, CAST(AI.CodUsuario AS VARCHAR(20))) AS autor,
       COALESCE(T.NomeTipoIteracao, CAST(AI.CodTipoIteracao AS VARCHAR(10))) AS tipo,
       AI.TextoIteracao AS texto,
       AI.NomeContato AS contato,
       AI.CodTipoIteracao AS codtipo
FROM AtendimentoIteracao AI WITH (NOLOCK)
LEFT JOIN Usuarios U WITH (NOLOCK) ON U.CodUsuario = AI.CodUsuario
LEFT JOIN TipoIteracao T WITH (NOLOCK) ON T.CodTipoIteracao = AI.CodTipoIteracao
WHERE AI.CodEmpresa = ? AND AI.NumAtendimento = ? AND AI.Desdobramento = ? AND AI.NumIteracao <= ?
ORDER BY AI.NumIteracao
"""

COLS_NOVAS = ("revisao_mudou INTEGER DEFAULT 0", "ref_id INTEGER", "fonte TEXT", "modelo TEXT", "variante TEXT",
              "versao_prompt TEXT", "tokens_in INTEGER", "tokens_out INTEGER", "tokens_cache INTEGER")
_U = "id IN (SELECT MAX(id) FROM avaliacoes GROUP BY atendimento_id)"


def preparar_tabela(lite):
    lite.execute("""CREATE TABLE IF NOT EXISTS teste_resultados(
        id INTEGER PRIMARY KEY AUTOINCREMENT, rodada TEXT, atendimento_id TEXT, num_iter INTEGER,
        real TEXT, cqs_real TEXT, ia_concluir INTEGER, cqs_ia TEXT, confianca REAL,
        justificativa TEXT, pendencias TEXT, motivo_revisor TEXT, postura TEXT)""")
    for col in COLS_NOVAS:
        try:
            lite.execute(f"ALTER TABLE teste_resultados ADD COLUMN {col}")
        except Exception:
            pass
    lite.commit()


def amostra(lite, n, cqs_desde):
    """ERP, mesmos casos a cada execução (semente fixa): metade devolvidos, metade concluídos com CQS."""
    rnd = random.Random(42)
    dev = [r[0] for r in lite.execute("SELECT id FROM historico_revisoes WHERE desfecho='devolvido' AND data >= ? "
                                      "ORDER BY atendimento_id, num_iter", (cqs_desde,))]
    con = [r[0] for r in lite.execute("SELECT id FROM historico_revisoes WHERE desfecho='concluido' AND data >= ? "
                                      "AND COALESCE(cqs,'')<>'' ORDER BY atendimento_id, num_iter", (cqs_desde,))]
    k = n // 2
    ids = rnd.sample(dev, min(k, len(dev))) + rnd.sample(con, min(n - min(k, len(dev)), len(con)))
    return [("erp", i) for i in ids]


def amostra_painel(lite, n):
    """Painel, mesmos casos a cada execução (semente fixa): 2/3 "Não" revisados pelo gerente (metade que ele
    aceitou, metade que rejeitou) e 1/3 "Sim" com análise e CQS revisados."""
    rnd = random.Random(42)
    ids = lambda w: [r[0] for r in lite.execute(f"SELECT id FROM avaliacoes WHERE {_U} AND {w} ORDER BY id")]
    rej = ids("pode_concluir=0 AND status='rejeitada'")
    ace = ids("pode_concluir=0 AND status='aceita'")
    sim = ids("pode_concluir=1 AND status IN ('aceita','rejeitada') AND status_cqs IN ('aceita','rejeitada')")
    k_sim = n // 3
    k_rej = min(len(rej), (n - k_sim) // 2)
    k_ace = min(len(ace), n - k_sim - k_rej)
    escolha = rnd.sample(rej, k_rej) + rnd.sample(ace, k_ace) + rnd.sample(sim, min(k_sim, len(sim)))
    return [("painel", i) for i in escolha]


def amostra_com_print(lite, conn, cfg):
    """Casos revisados pelo gerente no painel em que as imagens que o agente lê (as últimas max_imagens) incluem print
    de conversa do WhatsApp (detecção só pelas cores, sem OCR nem IA)."""
    max_imgs = cfg.getint("privacidade", "max_imagens_por_atendimento", fallback=5)
    out = []
    for ref, aid, qtd in lite.execute(f"SELECT id, atendimento_id, qtd_interacoes FROM avaliacoes WHERE {_U} "
                                      "AND status IN ('aceita','rejeitada') ORDER BY id").fetchall():
        emp, num, desd = (int(x) for x in aid.split("-"))
        inter = agente.consultar(conn, cfg["queries"]["interacoes"], (emp, num, desd))[:qtd]
        pngs = [p for r in inter for p in extrair_pngs_rtf(r.get("texto"))][-max_imgs:]
        if any(eh_print_conversa(p) for p in pngs):
            out.append(("painel", ref))
    return out


def casos_da_rodada(lite, rodada):
    """Os mesmos casos de uma rodada anterior (as antigas, sem ref_id, eram do ERP)."""
    out = []
    for fonte, ref, aid, ni in lite.execute("SELECT fonte, ref_id, atendimento_id, num_iter FROM teste_resultados "
                                            "WHERE rodada=? ORDER BY id", (rodada,)):
        if ref is None:
            r = lite.execute("SELECT id FROM historico_revisoes WHERE atendimento_id=? AND num_iter=?", (aid, ni)).fetchone()
            ref, fonte = (r[0] if r else None), "erp"
        if ref is not None:
            out.append((fonte or "erp", ref))
    return out


def _sql_um_atendimento(cfg):
    """A query 'candidatos' do agente, filtrando um atendimento (traz cliente, analista, cidade... do mesmo jeito)."""
    q = cfg["queries"]["candidatos"]
    m = re.search(r"(?is)\bWHERE\s+A\.Situacao\b", q)
    return (q[:m.start()] + "WHERE A.CodEmpresa = ? AND A.NumAtendimento = ? AND A.Desdobramento = ?") if m else None


def carregar_caso(lite, conn, cfg, fonte, ref, sql_at):
    """-> dict: aid, num_iter, at, inter, rdms, real ('concluido' | 'devolvido'), cqs_real, motivo."""
    if fonte == "painel":
        aid, qtd, pode, st, cod, st_cqs, certo, motivo, rdms_a, cliente, assunto, analista = lite.execute(
            "SELECT atendimento_id, qtd_interacoes, pode_concluir, status, cqs_cod, status_cqs, cqs_correto, motivo, "
            "rdms, cliente, assunto, analista FROM avaliacoes WHERE id=?", (ref,)).fetchone()
    else:
        aid, qtd, assunto, cliente, analista, desf, cqs_h, motivo = lite.execute(
            "SELECT atendimento_id, num_iter, assunto, cliente, analista, desfecho, cqs, motivo FROM historico_revisoes "
            "WHERE id=?", (ref,)).fetchone()
        rdms_a = None
    emp, num, desd = (int(x) for x in aid.split("-"))
    erp = (agente.consultar(conn, sql_at, (emp, num, desd)) or [{}])[0] if sql_at else {}
    # cliente/assunto/analista como estavam quando o caso foi decidido; o resto (código e cidade do cliente) do ERP
    at = {**erp, "codempresa": emp, "numatendimento": num, "desdobramento": desd,
          **{k: v for k, v in (("cliente", cliente), ("assunto", assunto), ("analista", analista)) if v}}
    if fonte == "painel":
        inter = agente.consultar(conn, cfg["queries"]["interacoes"], (emp, num, desd))[:qtd]   # o que a IA viu
        concluir = bool(pode) if st == "aceita" else not bool(pode)      # verdade = decisão do gerente
        cqs_real = ((cod if st_cqs == "aceita" else certo if st_cqs == "rejeitada" else "") or "") if concluir else ""
        real = "concluido" if concluir else "devolvido"
    else:
        inter = agente.consultar(conn, SQL_INTER, (emp, num, desd, qtd))
        real, cqs_real = desf, cqs_h or ""
    return {"aid": aid, "num_iter": qtd, "at": at, "inter": inter, "rdms": rdms_a, "real": real,
            "cqs_real": cqs_real, "motivo": motivo or ""}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=None, help="tamanho do conjunto (ERP: 40; painel: 90)")
    ap.add_argument("--painel", action="store_true", help="casos revisados pelo gerente no painel")
    ap.add_argument("--com-print", action="store_true", help="casos do painel com print de conversa do WhatsApp")
    ap.add_argument("--ids", default=None, help="só essas avaliações do painel, separadas por vírgula (ex.: 437,386)")
    ap.add_argument("--ocr-simples", action="store_true", help="prints com o OCR comum, sem separar as falas")
    ap.add_argument("--repetir", default=None, metavar="RODADA", help="os mesmos casos de uma rodada anterior")
    ap.add_argument("--regras", default=None, metavar="ARQUIVO", help="outro arquivo de regras (proposta)")
    ap.add_argument("--listar", action="store_true", help="só mostra os casos, sem chamar a IA")
    ap.add_argument("--comparar", nargs=2, metavar=("RODADA_A", "RODADA_B"), help="compara duas rodadas (sem IA)")
    ap.add_argument("--usuario", default=None)
    ap.add_argument("--modelo", default=None, help="testar outro modelo do mesmo provedor (ex.: gpt-4.1)")
    ap.add_argument("--sem-regras", action="store_true", help="ignora regras_aprovadas.txt (para comparar)")
    ap.add_argument("--com-revisao", action="store_true", help="liga a 2ª revisão (para comparar)")
    ap.add_argument("--sem-revisao", action="store_true", help="desliga a 2ª revisão")
    ap.add_argument("--modelo-revisao", default=None, help="modelo da 2ª revisão (liga a 2ª revisão)")
    a = ap.parse_args()

    cfg = agente.ler_cfg()
    lite = agente.init_sqlite(agente.BASE / cfg["saida"]["sqlite"])
    hist.criar_tabela(lite)
    preparar_tabela(lite)
    if a.comparar:
        print(comparar(lite, *a.comparar))
        return
    cqs_desde = cfg.get("aprendizado", "cqs_desde", fallback="").strip() or "2000-01-01"
    if a.repetir:
        casos = casos_da_rodada(lite, a.repetir)
    elif a.ids:
        casos = [("painel", int(x)) for x in a.ids.split(",") if x.strip()]
    elif a.com_print:
        _c = pyodbc.connect(cfg["banco"]["conn_string"], readonly=True, timeout=30)
        try:
            casos = amostra_com_print(lite, _c, cfg)
        finally:
            _c.close()
    elif a.painel:
        casos = amostra_painel(lite, a.n or 90)
    else:
        casos = amostra(lite, a.n or 40, cqs_desde)
    if not casos:
        print("Sem casos para testar (painel sem revisões ou histórico vazio: python historico.py --importar).")
        return
    if a.listar:
        for k, (fonte, ref) in enumerate(casos, 1):
            if fonte == "painel":
                r = lite.execute("SELECT atendimento_id, pode_concluir, status, cqs_cod, status_cqs, cqs_correto "
                                 "FROM avaliacoes WHERE id=?", (ref,)).fetchone()
                print(f"[{k}] painel {r[0]}: IA {'Sim' if r[1] else 'Não'}, gerente {r[2]}"
                      f"{f', CQS {r[3]} {r[4]}' if r[4] else ''}{f' (certo {r[5]})' if r[5] else ''}")
            else:
                r = lite.execute("SELECT atendimento_id, desfecho, cqs FROM historico_revisoes WHERE id=?", (ref,)).fetchone()
                print(f"[{k}] erp {r[0]}: {r[1]} {r[2] or ''}")
        return

    client = agente.criar_client(cfg, a.usuario)
    variante = ["painel" if any(f == "painel" for f, _ in casos) else "erp"]
    if a.modelo:
        cfg["agente"]["modelo"] = a.modelo
        variante.append(f"modelo:{a.modelo}")
    if a.com_revisao or a.modelo_revisao:
        cfg["agente"]["segunda_revisao"] = "sim"
        if a.modelo_revisao:
            cfg["agente"]["modelo_revisao"] = a.modelo_revisao
        variante.append(f"revisao:{cfg.get('agente', 'modelo_revisao', fallback='') or cfg['agente']['modelo']}")
    if a.sem_revisao:
        cfg["agente"]["segunda_revisao"] = "nao"
    if a.ocr_simples:
        cfg["privacidade"]["separar_falas_whatsapp"] = "nao"
        variante.append("ocr-simples")
    if a.sem_regras:
        variante.append("sem-regras")
    elif a.regras:
        variante.append(f"regras:{a.regras}")
    revisao_on = cfg.getboolean("agente", "segunda_revisao", fallback=True)
    print(f"Modelo: {cfg['agente']['provider']} / {cfg['agente']['modelo']} · regras: "
          f"{'nenhuma' if a.sem_regras else (a.regras or 'aprovadas')} · 2ª revisão: "
          f"{(cfg.get('agente', 'modelo_revisao', fallback='') or 'sim') if revisao_on else 'não'} · {len(casos)} casos")
    conn = pyodbc.connect(cfg["banco"]["conn_string"], readonly=True, timeout=30)
    rodada = datetime.now().isoformat(timespec="seconds")
    try:
        usuarios = agente.consultar(conn, cfg["queries"]["usuarios"]) if cfg.has_option("queries", "usuarios") else []
        catalogo = agente.consultar(conn, cfg["queries"]["cqs_tipos"]) if cfg.has_option("queries", "cqs_tipos") else []
        por_cod = {str(c["codtiporegistro"]): c for c in catalogo}
        ocr = None
        if cfg.getboolean("privacidade", "ler_imagens", fallback=False):
            ocr = agente.LeitorOCR(cfg.get("privacidade", "tesseract_cmd", fallback=""),
                                   cfg.get("privacidade", "ocr_idioma", fallback="por+eng"))
        prot_base = agente.Protecao(cfg, usuarios, ocr)
        indice = agente.indice_aprendizado(lite)
        cfg["agente"]["_postura_texto"] = agente.postura_texto(cfg, lite)
        cfg["agente"]["_regras_revisao"] = "" if a.sem_regras else agente.regras_revisao(cfg, a.regras)
        versao = agente.versao_prompt(cfg, catalogo)
        cqs_versao = agente.versao_cqs(cfg, catalogo)
        vigentes = agente.codigos_vigentes(cfg, catalogo) if catalogo else None
        dist = agente.distribuicao_cqs(cfg, lite)
        max_chars = cfg.getint("agente", "max_chars_historico", fallback=12000)
        internos = {agente._norm(u.get("nomeusuario")) for u in usuarios if u.get("nomeusuario")}
        exigir = cfg.getboolean("agente", "exigir_contato_cliente", fallback=True)
        cont_on = cfg.getboolean("agente", "continuidade_outro_atendimento", fallback=True)
        so_on = cfg.getboolean("agente", "concluir_so_atualizacao", fallback=True)
        rdm_fin = rdm.finalizadas(cfg)
        sql_at = _sql_um_atendimento(cfg)
        for k, (fonte, ref) in enumerate(casos, 1):
            try:
                c = carregar_caso(lite, conn, cfg, fonte, ref, sql_at)
                at, inter, aid = c["at"], c["inter"], c["aid"]
                exclusivo = agente.classificar_so_atualizacao(inter) if so_on else None
                contato_concl = agente.contato_conclusao(inter, at.get("analista"), internos)
                bloq = exigir and contato_concl not in (None, "cliente") and not exclusivo
                prot = prot_base.para_atendimento(at.get("cliente"))
                historico = agente.montar_historico(inter, max_chars, prot, at.get("analista"), internos)
                if bloq and agente.print_do_cliente_na_conclusao(inter, prot, at.get("analista"), internos):
                    bloq, contato_concl = False, "cliente"
                atual = agente.extrair_atualizacoes(inter)
                ex = agente.montar_exemplos(lite, cfg, prot_base, cqs_versao, agente.texto_consulta(at, inter), indice,
                                            excluir_aid=aid, codigos=vigentes)
                if dist:
                    ex = (ex + "\n\n" + dist).strip()
                c0 = llm.USO["cache"]
                dec = agente.decidir(client, cfg, conn, at, inter, historico, catalogo, por_cod, ex, prot, "", "",
                                     atual, c["rdms"], rdm_fin, exclusivo, contato_concl, bloq, cont_on, aid)
            except Exception as e:
                print(f"[{k}/{len(casos)}] {fonte} {ref}: erro {e}")
                continue
            res, pode = dec["res"], dec["pode"]
            cqs = str(dec["cqs"]["codtiporegistro"]) if dec["cqs"] else ""
            mudou = int("[Devolvido na 2ª revisão]" in str(res.get("justificativa") or ""))
            lite.execute(
                "INSERT INTO teste_resultados(rodada,atendimento_id,num_iter,real,cqs_real,ia_concluir,cqs_ia,confianca,"
                "justificativa,pendencias,motivo_revisor,postura,revisao_mudou,ref_id,fonte,modelo,variante,versao_prompt,"
                "tokens_in,tokens_out,tokens_cache) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rodada, aid, c["num_iter"], c["real"], c["cqs_real"], int(pode), cqs, float(dec["conf"] or 0),
                 str(res.get("justificativa") or ""), str(res.get("pendencias") or "").strip(), c["motivo"],
                 cfg.get("agente", "postura", fallback="conservadora"), mudou, ref, fonte, cfg["agente"]["modelo"],
                 " ".join(variante), versao, dec["tin"], dec["tout"], llm.USO["cache"] - c0))
            lite.commit()
            ok = "ok " if pode == (c["real"] == "concluido") else "DIF"
            print(f"[{k}/{len(casos)}] {ok} {aid}: {'gerente' if fonte == 'painel' else 'ERP'} {c['real']}"
                  f"{' ' + c['cqs_real'] if c['cqs_real'] else ''} | IA {'Sim ' + cqs if pode else 'Não'}")
    finally:
        conn.close()
    print(f"\nRodada: {rodada}")
    print(relatorio(lite, rodada))


def resumo(lite, rodada):
    rows = lite.execute("SELECT real, ia_concluir, cqs_real, cqs_ia, COALESCE(tokens_in,0), COALESCE(tokens_out,0), "
                        "COALESCE(tokens_cache,0), COALESCE(fonte,'erp'), variante FROM teste_resultados WHERE rodada=?",
                        (rodada,)).fetchall()
    n = len(rows)
    m = {(s, d): 0 for s in (1, 0) for d in ("concluido", "devolvido")}
    for r in rows:
        m[(r[1], r[0])] += 1
    cq = [(r[2], r[3]) for r in rows if r[1] == 1 and r[0] == "concluido" and r[2]]
    cqs_ok = sum(1 for real, ia in cq if ia and ia in real.split(","))
    return {"n": n, "m": m, "acerto": (m[(1, "concluido")] + m[(0, "devolvido")]) / n if n else None,
            "cqs_ok": cqs_ok, "cqs_n": len(cq), "fonte": rows[0][7] if rows else "erp",
            "variante": rows[0][8] if rows else "",
            "tin": sum(r[4] for r in rows) / n if n else 0, "tout": sum(r[5] for r in rows) / n if n else 0,
            "cache": sum(r[6] for r in rows) / n if n else 0}


def relatorio(lite, rodada):
    s = resumo(lite, rodada)
    m, painel = s["m"], s["fonte"] == "painel"
    quem = "gerente" if painel else "revisor no ERP"
    rows = lite.execute("SELECT atendimento_id, real, cqs_real, ia_concluir, cqs_ia, justificativa, pendencias, "
                        "motivo_revisor FROM teste_resultados WHERE rodada=?", (rodada,)).fetchall()
    out = [f"\nRESULTADO DO TESTE ({s['n']} casos, verdade = {quem}; {s['variante'] or ''})",
           f"  IA Sim  -> devia concluir: {m[(1,'concluido')]:3d}   não devia: {m[(1,'devolvido')]:3d}   <- Sim indevido = erro grave",
           f"  IA Não  -> devia concluir: {m[(0,'concluido')]:3d}   não devia: {m[(0,'devolvido')]:3d}"]
    if s["acerto"] is not None:
        out.append(f"  Concordância da análise: {s['acerto']:.0%}")
    dev = m[(1, "devolvido")] + m[(0, "devolvido")]
    if dev:
        out.append(f"  Pegou {m[(0,'devolvido')]} de {dev} que não deviam concluir ({m[(0,'devolvido')] / dev:.0%})")
    if s["cqs_n"]:
        out.append(f"  CQS: acertou {s['cqs_ok']} de {s['cqs_n']} ({s['cqs_ok'] / s['cqs_n']:.0%})")
        pares = {}
        for r in rows:
            if r[3] == 1 and r[1] == "concluido" and r[2]:
                pares[(r[4] or "-", r[2])] = pares.get((r[4] or "-", r[2]), 0) + 1
        out.append("  CQS IA -> certo: " + ", ".join(f"{i}->{r} ({n})" for (i, r), n in
                                                     sorted(pares.items(), key=lambda x: -x[1])))
    out.append(f"  Tokens por caso: {s['tin']:,.0f} entrada ({s['cache']:,.0f} do cache) / {s['tout']:,.0f} saída"
               .replace(",", "."))
    rev = lite.execute("SELECT real, COUNT(*) FROM teste_resultados WHERE rodada=? AND revisao_mudou=1 GROUP BY real",
                       (rodada,)).fetchall()
    if rev:
        d = dict(rev)
        out.append(f"  2ª revisão virou Sim->Não em {sum(d.values())}: {d.get('devolvido', 0)} acertos, "
                   f"{d.get('concluido', 0)} erros")
    graves = [r for r in rows if r[3] == 1 and r[1] == "devolvido"]
    if graves:
        out.append(f"\nIA DISSE SIM, {quem.upper()} NÃO CONCLUIRIA:")
        for r in graves:
            out.append(f"  {r[0]} | {quem}: {str(r[7] or '')[:120]}")
    nao_conc = [r for r in rows if r[3] == 0 and r[1] == "concluido"]
    if nao_conc:
        so_conf = [r for r in nao_conc if hist.so_confirmacao(r[6])]
        out.append(f"\nIA DISSE NÃO, {quem.upper()} CONCLUIRIA ({len(nao_conc)}; {len(so_conf)} só por falta de confirmação):")
        for r in nao_conc[:25]:
            out.append(f"  {r[0]} | pendências: {str(r[6] or '')[:110]}" + (f" | {quem}: {str(r[7] or '')[:90]}" if painel else ""))
    return "\n".join(out)


def comparar(lite, a, b):
    """Antes x depois: os números das duas rodadas e os casos em que a decisão mudou."""
    sa, sb = resumo(lite, a), resumo(lite, b)
    pct = lambda x: f"{x:.0%}" if x is not None else "-"
    out = [f"{'':28}{'A ' + a:>26}{'B ' + b:>26}",
           f"{'variante':28}{sa['variante'] or '':>26}{sb['variante'] or '':>26}",
           f"{'casos':28}{sa['n']:>26}{sb['n']:>26}",
           f"{'concordância da análise':28}{pct(sa['acerto']):>26}{pct(sb['acerto']):>26}",
           f"{'Sim indevido (grave)':28}{sa['m'][(1, 'devolvido')]:>26}{sb['m'][(1, 'devolvido')]:>26}",
           f"{'Não indevido':28}{sa['m'][(0, 'concluido')]:>26}{sb['m'][(0, 'concluido')]:>26}",
           f"{'CQS certo':28}{str(sa['cqs_ok']) + '/' + str(sa['cqs_n']):>26}{str(sb['cqs_ok']) + '/' + str(sb['cqs_n']):>26}",
           f"{'tokens entrada / caso':28}{sa['tin']:>26,.0f}{sb['tin']:>26,.0f}".replace(",", "."),
           f"{'  dos quais do cache':28}{sa['cache']:>26,.0f}{sb['cache']:>26,.0f}".replace(",", ".")]
    da = {r[0]: r[1:] for r in lite.execute("SELECT COALESCE(ref_id, atendimento_id), real, ia_concluir, cqs_real, "
                                            "cqs_ia, atendimento_id FROM teste_resultados WHERE rodada=?", (a,))}
    mud = []
    for ref, real, ia, cqs_real, cqs_ia, aid in lite.execute(
            "SELECT COALESCE(ref_id, atendimento_id), real, ia_concluir, cqs_real, cqs_ia, atendimento_id "
            "FROM teste_resultados WHERE rodada=?", (b,)):
        if ref in da and (da[ref][1] != ia or da[ref][3] != cqs_ia):
            antes = "Sim " + (da[ref][3] or "") if da[ref][1] else "Não"
            depois = "Sim " + (cqs_ia or "") if ia else "Não"
            certo_a, certo_b = (da[ref][1] == 1) == (real == "concluido"), (ia == 1) == (real == "concluido")
            tag = "melhorou" if certo_b and not certo_a else "piorou" if certo_a and not certo_b else ""
            if not tag and ia == 1 and da[ref][1] == 1 and cqs_real:   # só o CQS mudou
                ca, cb = (da[ref][3] or "") in cqs_real.split(","), (cqs_ia or "") in cqs_real.split(",")
                tag = "CQS melhorou" if cb and not ca else "CQS piorou" if ca and not cb else ""
            mud.append(f"  {aid}: {antes.strip()} -> {depois.strip()} (devia {'concluir' if real == 'concluido' else 'não concluir'}"
                       f"{' ' + cqs_real if cqs_real else ''}) {tag}")
    if mud:
        out.append(f"\nCASOS QUE MUDARAM ({len(mud)}):")
        out += mud
    return "\n".join(out)


if __name__ == "__main__":
    main()
