"""Migrações únicas do avaliacoes.db (controladas por PRAGMA user_version). Usado pelo agente e pelo painel."""
import re


def _cod_certo(motivo, cod_ia):
    """'CQS errado, o certo é 55' -> '55'. Só devolve se houver exatamente um código diferente do da IA."""
    cods = {c for c in re.findall(r"(?<!\d)(\d{2,3})(?!\d)", motivo or "") if c != str(cod_ia or "")}
    return cods.pop() if len(cods) == 1 else None


def migrar(con):
    v = con.execute("PRAGMA user_version").fetchone()[0]
    if v < 1:
        # antes, "aceita" valia para análise + CQS juntos -> o CQS desses também foi aceito
        con.execute("UPDATE avaliacoes SET status_cqs='aceita' WHERE status='aceita' AND pode_concluir=1 "
                    "AND COALESCE(cqs_cod,'')<>'' AND status_cqs IS NULL")
        con.execute("PRAGMA user_version=1")
    if v < 2:
        # rejeitadas antigas de um "Sim": se o motivo cita CQS, o erro foi o CQS (a análise estava certa);
        # se não cita, o erro foi a análise (fica como está)
        rows = con.execute(
            "SELECT id, motivo, cqs_cod FROM avaliacoes WHERE status='rejeitada' AND pode_concluir=1 "
            "AND status_cqs IS NULL AND motivo LIKE '%cqs%'").fetchall()   # LIKE do SQLite ignora maiúsc./minúsc.
        for id_, motivo, cod in rows:
            con.execute("UPDATE avaliacoes SET status='aceita', status_cqs='rejeitada', motivo_cqs=?, motivo='', "
                        "cqs_correto=? WHERE id=?", (motivo, _cod_certo(motivo, cod), id_))
        # "Não" rejeitado (podia concluir) que cita o CQS: a análise errou mesmo; guarda o CQS certo se houver
        for id_, motivo in con.execute(
                "SELECT id, motivo FROM avaliacoes WHERE status='rejeitada' AND pode_concluir=0 "
                "AND cqs_correto IS NULL AND motivo LIKE '%cqs%'").fetchall():
            con.execute("UPDATE avaliacoes SET cqs_correto=? WHERE id=?", (_cod_certo(motivo, None), id_))
        con.execute("PRAGMA user_version=2")
    con.commit()
