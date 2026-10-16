"""
RDMs vinculadas ao atendimento (RDM.CodEmpresa + NumAtendimento + DesdobramentoAtendimento).

Regra: o atendimento só pode ser concluído quando TODAS as RDMs vinculadas estão finalizadas
(concluída ou cancelada; [rdm] finalizadas no config.ini). Qualquer outra situação é pendência.
A situação fica em avaliacoes.rdms como assinatura "NumRDM/Desdobramento:Situacao;..." (ordenada):
serve para mostrar no painel e para reavaliar o atendimento quando alguma RDM muda de situação.
"""
FINALIZADAS = {"3": "Concluída", "4": "Cancelada"}


def finalizadas(cfg):
    """[rdm] finalizadas = 3:Concluída, 4:Cancelada -> {"3": "Concluída", "4": "Cancelada"}"""
    v = cfg.get("rdm", "finalizadas", fallback="") if cfg is not None and cfg.has_section("rdm") else ""
    fin = {}
    for item in v.split(","):
        cod, _, nome = item.partition(":")
        if cod.strip():
            fin[cod.strip()] = nome.strip() or f"situação {cod.strip()}"
    return fin or dict(FINALIZADAS)


def assinatura(rows):
    """Linhas da query [queries] rdms (numrdm, desdobramento, situacao) -> "1570712/0:3;1570712/1:4"."""
    itens = {(int(r["numrdm"]), int(r["desdobramento"] or 0), str(r["situacao"]).strip()) for r in rows}
    return ";".join(f"{n}/{d}:{s}" for n, d, s in sorted(itens))


def _itens(assin):
    for x in str(assin or "").split(";"):
        rdm_, _, sit = x.partition(":")
        if rdm_.strip():
            yield rdm_.strip(), sit.strip()


def pendentes(assin, fin):
    """Pendências das RDMs que não estão concluídas nem canceladas."""
    return [f"RDM {r} não está concluída nem cancelada (situação {s})" for r, s in _itens(assin) if s not in fin]


def texto_ia(assin, fin):
    """Seção do prompt (vazia quando o atendimento não tem RDM vinculada)."""
    linhas = [f"- RDM {r}: {fin[s]}" if s in fin else f"- RDM {r}: PENDENTE (situação {s}, nem concluída nem cancelada)"
              for r, s in _itens(assin)]
    return ("RDMs VINCULADAS A ESTE ATENDIMENTO (situação atual no sistema):\n" + "\n".join(linhas)) if linhas else ""


def descrever(assin, fin):
    """Texto curto para o painel: "1570712/0 Concluída · 591046/1 ⚠️ pendente (situação 18)"."""
    return " · ".join(f"{r} {fin[s]}" if s in fin else f"{r} ⚠️ pendente (situação {s})" for r, s in _itens(assin))
