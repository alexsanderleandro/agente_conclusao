"""Busca de casos parecidos (TF-IDF simples, local, sem dependência externa).

Usado para escolher, para cada atendimento, os exemplos de aprendizado mais parecidos com ele
(em vez de só os mais recentes).
"""
import math
import re
import unicodedata
from collections import Counter

_STOP = set("""
a o e de da do das dos em no na nos nas um uma uns umas para por com sem que se ao aos as os
foi ser esta este essa esse isso isto ja nao sim mais muito pois pelo pela pelos pelas como
quando onde qual quais seu sua seus suas ele ela eles elas voce voces nos lhe lhes me te
tem ter teve sera estao estava estou fica ficou pode podem deve favor obrigado obrigada
bom boa dia tarde noite ola att atenciosamente cliente analista atendimento ok segue
""".split())


def _norm(txt):
    txt = unicodedata.normalize("NFKD", str(txt or "").lower())
    return "".join(c for c in txt if not unicodedata.combining(c))


def tokens(txt):
    return [t for t in re.findall(r"[a-z0-9]{3,}", _norm(txt)) if t not in _STOP and not t.isdigit()]


class Indice:
    """docs: lista de (chave, texto). busca(texto, k, filtro) -> [(score, chave)]"""

    def __init__(self, docs):
        self.chaves, vets = [], []
        df = Counter()
        for chave, txt in docs:
            tf = Counter(tokens(txt))
            if not tf:
                continue
            self.chaves.append(chave)
            vets.append(tf)
            df.update(tf.keys())
        n = max(len(vets), 1)
        self.idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}
        self.vets = [self._pesar(tf) for tf in vets]

    def _pesar(self, tf):
        v = {t: (1 + math.log(c)) * self.idf.get(t, 1.0) for t, c in tf.items()}
        norma = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norma for t, x in v.items()}

    def busca(self, texto, k=5, filtro=None, minimo=0.05):
        q = self._pesar(Counter(tokens(texto)))
        if not q:
            return []
        res = []
        for chave, v in zip(self.chaves, self.vets):
            if filtro and not filtro(chave):
                continue
            s = sum(x * v.get(t, 0.0) for t, x in q.items())
            if s >= minimo:
                res.append((s, chave))
        res.sort(key=lambda x: -x[0])
        return res[:k]
