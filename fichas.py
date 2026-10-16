"""
Fichas de visita na pasta do cliente (rede):
    <raiz>\\<PastaDoCliente>\\FichaVisita\\FichaDeVisita(Assinada) -Cliente- 23set2026.pdf

- acha a pasta do cliente pelo nome (normalizado) ou por um mapa manual no config.ini;
- pega só as fichas do período do atendimento (data no nome do arquivo; se não houver, data do arquivo);
- lê PDF com texto direto e, se a página for escaneada/foto, por OCR local; lê também JPG/PNG.
Nada é enviado daqui: o texto volta para o agente, que mascara (LGPD) antes de mandar à IA.
"""
import difflib
import io
import logging
import os
import re
import unicodedata
from datetime import date, datetime

log = logging.getLogger("agente")

_MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
          "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}
_SUFIXOS = re.compile(r"\s+(ltda|me|epp|eireli|s\.?\s?a\.?|s/a|cia)\.?$", re.I)


def normalizar(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = s.strip()
    while True:
        novo = _SUFIXOS.sub("", s).strip()
        if novo == s:
            break
        s = novo
    return re.sub(r"[^a-z0-9]", "", s.lower())


def data_no_nome(nome):
    """'... 23set2026.pdf' / '23-09-2026' / '23.09.26' / '20260923' -> date"""
    n = nome.lower()
    m = re.search(r"(\d{1,2})\s*[-_. ]?\s*(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-zç]*\s*[-_. ]?\s*(\d{2,4})", n)
    try:
        if m:
            a = int(m.group(3))
            return date(a + 2000 if a < 100 else a, _MESES[m.group(2)], int(m.group(1)))
        m = re.search(r"(?<!\d)(\d{1,2})[-_.](\d{1,2})[-_.](\d{2,4})(?!\d)", n)
        if m:
            a = int(m.group(3))
            return date(a + 2000 if a < 100 else a, int(m.group(2)), int(m.group(1)))
        m = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", n)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return None


class LocalizadorFichas:
    EXTENSOES = (".pdf", ".jpg", ".jpeg", ".png")

    def __init__(self, raiz, subpasta="FichaVisita", mapa_manual=None, similaridade=0.85):
        self.raiz = raiz
        self.subpasta = subpasta
        self.mapa_manual = {str(k).strip(): v.strip() for k, v in (mapa_manual or {}).items()}
        self.similaridade = similaridade
        self._pastas = None

    def _listar_pastas(self):
        if self._pastas is None:  # 1 listagem da raiz por ciclo
            try:
                self._pastas = {normalizar(d): d for d in os.listdir(self.raiz)
                                if os.path.isdir(os.path.join(self.raiz, d))}
            except OSError as e:
                log.warning("Fichas: não consegui listar %s: %s", self.raiz, e)
                self._pastas = {}
        return self._pastas

    def pasta_cliente(self, codcliente=None, nome=None):
        # 1) mapa manual do config.ini (por código ou por nome)
        for chave in (codcliente, nome):
            if chave is not None and str(chave).strip() in self.mapa_manual:
                p = self.mapa_manual[str(chave).strip()]
                return p if os.path.isabs(p) else os.path.join(self.raiz, p)
        # 2) nome normalizado ("MINAS FREIOS LTDA" -> "minasfreios" == pasta "MinasFreios"),
        #    também sem a unidade: "Pescados Serra e Mar - Unidade BH" -> "Pescados Serra e Mar"
        pastas = self._listar_pastas()
        nomes = [nome]
        sem_unidade = re.split(r"\s+[-–]\s+", str(nome or ""))[0]
        if sem_unidade and sem_unidade != nome:
            nomes.append(sem_unidade)
        for n in nomes:
            alvo = normalizar(n)
            if len(alvo) < 4:
                continue
            if alvo in pastas:
                return os.path.join(self.raiz, pastas[alvo])
            parecidas = difflib.get_close_matches(alvo, pastas.keys(), n=2, cutoff=self.similaridade)
            if len(parecidas) == 1:
                return os.path.join(self.raiz, pastas[parecidas[0]])
            if len(parecidas) > 1:
                log.warning("Fichas: pasta ambígua para '%s' (%s); use [pastas_clientes] no config.ini",
                            nome, ", ".join(pastas[p] for p in parecidas))
                return None
        return None

    def fichas(self, codcliente, nome, desde, pasta=None):
        """Lista [(caminho, data)] das fichas do período (desde <= data <= hoje), mais recentes primeiro.
        pasta: pasta do cliente já conhecida (ex.: tirada dos links do atendimento)."""
        base = pasta or self.pasta_cliente(codcliente, nome)
        if not base:
            return []
        pasta = os.path.join(base, self.subpasta)
        if not os.path.isdir(pasta):
            return []
        achadas = []
        for arq in os.listdir(pasta):
            caminho = os.path.join(pasta, arq)
            if not (os.path.isfile(caminho) and arq.lower().endswith(self.EXTENSOES)):
                continue
            d = data_no_nome(arq)
            if d is None:
                d = datetime.fromtimestamp(os.path.getmtime(caminho)).date()
            if desde <= d <= date.today():
                achadas.append((caminho, d))
        return sorted(achadas, key=lambda x: x[1], reverse=True)


def ler_ficha(caminho, ocr=None, max_paginas=5, min_chars_pagina=40):
    """Texto da ficha. PDF digital: texto direto; página escaneada/foto: OCR local."""
    ext = os.path.splitext(caminho)[1].lower()
    if ext in (".jpg", ".jpeg", ".png"):
        if not (ocr and ocr.ok):
            return ""
        with open(caminho, "rb") as f:
            return ocr.ler(f.read(), ampliar=False, max_chars=6000)
    import pypdfium2 as pdfium
    partes = []
    pdf = pdfium.PdfDocument(caminho)
    try:
        for i in range(min(len(pdf), max_paginas)):
            pg = pdf[i]
            txt = pg.get_textpage().get_text_range().strip()
            if len(txt) < min_chars_pagina and ocr and ocr.ok:  # escaneada/foto: renderiza e faz OCR
                img = pg.render(scale=2.5).to_pil()
                b = io.BytesIO()
                img.save(b, "PNG")
                txt = ocr.ler(b.getvalue(), ampliar=False, max_chars=6000)
            if txt:
                partes.append(txt)
    finally:
        pdf.close()
    return "\n".join(partes)
