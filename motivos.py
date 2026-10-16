"""
Motivos estruturados da revisão do gerente (marcados com um clique no painel).

As listas dependem do que a IA respondeu:
  - IA disse Sim e estava errada -> o que ela deixou passar (por que NÃO podia concluir)
  - IA disse Não e estava errada -> por que ela foi rigorosa demais (podia concluir)
Podem ser trocadas no config.ini, [painel] motivos_sim / motivos_nao (separados por ;).
Ficam em avaliacoes.motivo_cat ("A; B") e entram nos exemplos de aprendizado junto com o texto livre.
"""
OUTRO = "Outro (descreva)"

MOTIVOS_SIM = [
    "Sem confirmação do cliente",
    "Registro não detalha o que foi feito",
    "Evidência citada não anexada",
    "Faltou RDM",
    "Depende de atualização ou monitoramento",
    "Assuntos misturados ou de outro atendimento",
    "Pendência em imagem, áudio ou ficha",
    "Conclusão não bate com o histórico",
]

MOTIVOS_NAO = [
    "Cliente confirmou e a IA não viu",
    "Pendência apontada já estava resolvida",
    "Exigiu algo que não se aplica ao caso",
    "Andamento segue em outro atendimento (nº informado)",
    "Leu errado o histórico, imagem ou áudio",
]


def lista(cfg, ia_sim):
    """Categorias para o caso: as do ini (se houver) ou as padrão, sempre com 'Outro' no fim."""
    opc = "motivos_sim" if ia_sim else "motivos_nao"
    v = cfg.get("painel", opc, fallback="") if cfg is not None and cfg.has_section("painel") else ""
    itens = [x.strip() for x in v.split(";") if x.strip()] or (MOTIVOS_SIM if ia_sim else MOTIVOS_NAO)
    return [x for x in itens if x != OUTRO] + [OUTRO]


def separar(txt):
    return [x.strip() for x in str(txt or "").split(";") if x.strip()]


def juntar(cats):
    return "; ".join(c for c in cats if c) or None


def para_exemplo(cats, motivo):
    """Texto do motivo para o prompt: categorias + texto livre."""
    c = [x for x in separar(cats) if x != OUTRO]
    m = str(motivo or "").strip()
    if c and m:
        return f"{', '.join(c)}. {m}"
    return ", ".join(c) or m
