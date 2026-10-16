"""Agenda dos ciclos: [agendamento] modo/dias/horarios do config.ini."""
import re
from datetime import datetime, timedelta

NOMES = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
DIAS = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "sáb": 5, "dom": 6}


def _dia(x):
    try:
        return DIAS[x.strip()[:3]]
    except KeyError:
        raise ValueError(f"[agendamento] dia inválido: '{x}' (use seg, ter, qua, qui, sex, sab, dom)")


def _dias_semana(txt):
    dias = set()
    for p in re.split(r"[,;\s]+", (txt or "").strip().lower()):
        if not p:
            continue
        if "-" in p:                                   # faixa: seg-sab
            a, b = (_dia(x) for x in p.split("-", 1))
            dias.update(range(a, b + 1) if a <= b else list(range(a, 7)) + list(range(0, b + 1)))
        else:
            dias.add(_dia(p))
    return dias


def proxima_execucao(cfg, agora=None):
    """Próximo horário agendado ([agendamento] dias/horarios), ou None se o modo for por intervalo."""
    if cfg.get("agendamento", "modo", fallback="intervalo").strip().lower() != "horario":
        return None
    agora = agora or datetime.now()
    dias = _dias_semana(cfg.get("agendamento", "dias", fallback="seg-sab"))
    horas = sorted(datetime.strptime(h.strip(), "%H:%M").time()
                   for h in re.split(r"[,;\s]+", cfg.get("agendamento", "horarios", fallback="19:00")) if h.strip())
    if not dias or not horas:
        raise ValueError("[agendamento] precisa de pelo menos um dia e um horário")
    for d in range(8):
        dia = (agora + timedelta(days=d)).date()
        if dia.weekday() not in dias:
            continue
        for h in horas:
            alvo = datetime.combine(dia, h)
            if alvo > agora:
                return alvo
    return None
