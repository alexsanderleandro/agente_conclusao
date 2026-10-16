"""
Proteção de dados (LGPD) antes de enviar qualquer texto para a IA.

Camada 1 - mascaramento: CPF, CNPJ, chave NF-e, e-mail, telefone, cartão, IP e senhas viram
           marcadores ([CPF], [SENHA]...). Nomes de usuários do ERP e do cliente viram
           pseudônimos ([USUARIO_156], [CLIENTE]) e voltam ao nome real só aqui, localmente,
           no resumo/justificativa que a IA devolve.
Camada 2 - imagens: os PNG embutidos no RichText são lidos por OCR local (Tesseract).
           A imagem NUNCA é enviada; só o texto lido, já mascarado.
"""
import io
import logging
import re

log = logging.getLogger("agente")

# ---------------------------------------------------------------- camada 1: dados pessoais
_SENHA = re.compile(
    r"(?i)\b(senhas?|password|passwd|pwd|pin|token)"
    r"(\s*(?:(?:do|da|de)\s+\w+\s*)?(?P<sep>[:=\-]|\bé\b|\beh\b)?\s*)"
    r"([\"']?)(?P<val>\S{2,})"
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# números: (?<!\d)/(?!\d) em vez de \b, para pegar também "LTDA_65381113000120"
_CHAVE_NFE = re.compile(r"(?<!\d)(?:\d{4}\s?){10}\d{4}(?!\d)")
_CNPJ = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")
_CPF = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-\d{2}(?!\d)|(?<![\d/.-])\d{11}(?![\d/.-])")
_CARTAO = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_TELEFONE = re.compile(r"(?:\+?55\s?)?(?:\(?(?<!\d)\d{2}\)?[\s.-]?)?(?<!\d)9?\d{4}[\s.-]\d{4}(?!\d)")
_IP = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")


def _senha(m):
    # sem separador explícito, só mascara se o valor "parece senha" (tem dígito ou símbolo):
    # "senha Local123" -> mascara; "senha resetada" -> mantém
    val = m.group("val")
    if m.group("sep") or re.search(r"[\d\W_]", val.strip("\"'.,;")):
        return f"{m.group(1)}{m.group(2)}[SENHA]"
    return m.group(0)


def _luhn_ok(digitos):
    s, alt = 0, False
    for d in reversed(digitos):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        s += n
        alt = not alt
    return s % 10 == 0


def _cartao(m):
    d = re.sub(r"\D", "", m.group(0))
    return "[CARTAO]" if 13 <= len(d) <= 19 and _luhn_ok(d) else m.group(0)


def mascarar_pii(txt):
    """Troca dados pessoais/sensíveis por marcadores. Ordem importa (do mais específico ao geral)."""
    if not txt:
        return txt
    txt = _SENHA.sub(_senha, txt)
    txt = _EMAIL.sub("[EMAIL]", txt)
    txt = _CHAVE_NFE.sub("[CHAVE_NFE]", txt)
    txt = _CARTAO.sub(_cartao, txt)
    txt = _CNPJ.sub("[CNPJ]", txt)
    txt = _CPF.sub("[CPF]", txt)
    txt = _IP.sub("[IP]", txt)
    txt = _TELEFONE.sub("[TELEFONE]", txt)
    return txt


_L = "A-Za-z0-9\u00C0-\u00FF"          # letras/dígitos (com acento); "_" e "." contam como separador
_ANTES, _DEPOIS = rf"(?<![{_L}])", rf"(?![{_L}])"
_SUFIXOS = re.compile(r"(?i)[\s,.-]+(ltda|me|epp|eireli|s\.?\s?a|s/a|cia|comercio|com[eé]rcio)\.?$")


def _variantes_cliente(nome):
    """'LOCAL ELETRONICA LTDA' -> {'LOCAL ELETRONICA LTDA', 'LOCAL ELETRONICA'}"""
    vs, atual = {nome}, nome
    sem_unidade = re.split(r"\s+[-–]\s+", nome)[0].strip()     # "X - Unidade BH" -> "X"
    if len(sem_unidade) >= 4 and sem_unidade != nome:
        vs.add(sem_unidade)
        atual = sem_unidade
    while True:
        novo = _SUFIXOS.sub("", atual).strip()
        if novo == atual or len(novo) < 4:
            break
        vs.add(novo)
        atual = novo
    # nome "colado", como nas pastas da rede: "MINAS FREIOS" -> "MINASFREIOS" (casa com "MinasFreios")
    return vs | {v.replace(" ", "") for v in vs if " " in v}


class Pseudonimizador:
    """Nomes conhecidos (usuários do ERP + cliente) <-> marcadores. O mapa nunca sai da máquina."""

    def __init__(self, usuarios=(), cliente=None):
        self.para_nome = {}           # marcador -> nome real (para reidentificar)
        variantes = []                # (regex, marcador)
        primeiros = {}
        for u in usuarios:
            nome = str(u.get("nomeusuario") or "").strip()
            cod = u.get("codusuario")
            if not nome or cod is None:
                continue
            marc = f"[USUARIO_{cod}]"
            self.para_nome[marc] = nome
            for v in {nome, nome.replace(".", " "), nome.replace("_", " ")}:
                if len(v) >= 3:
                    variantes.append((re.compile(rf"(?i){_ANTES}{re.escape(v)}{_DEPOIS}"), marc))
            p = re.split(r"[.\s_]", nome)[0]
            if len(p) >= 4:
                primeiros.setdefault(p.capitalize(), set()).add(marc)
        # primeiro nome só se não for ambíguo; case-sensitive (evita trocar palavras comuns)
        for p, marcs in primeiros.items():
            if len(marcs) == 1:
                variantes.append((re.compile(rf"{_ANTES}{re.escape(p)}{_DEPOIS}"), next(iter(marcs))))
        if cliente and len(str(cliente).strip()) >= 3:
            c = str(cliente).strip()
            self.para_nome["[CLIENTE]"] = c
            for v in _variantes_cliente(c):
                variantes.append((re.compile(rf"(?i){_ANTES}{re.escape(v)}{_DEPOIS}"), "[CLIENTE]"))
        # mais longos primeiro ("Rafael Freitas" antes de "Rafael")
        self._variantes = sorted(variantes, key=lambda x: -len(x[0].pattern))

    def adicionar(self, nome, marcador="[CLIENTE]"):
        """Variante extra (ex.: nome da pasta do cliente na rede, "GET", "MinasFreios").
        Nomes curtos (até 4 letras) só casam com a grafia exata, para não trocar palavras comuns."""
        nome = str(nome or "").strip()
        if len(nome) < 3:
            return
        flag = "" if len(nome) <= 4 else "(?i)"
        self._variantes.append((re.compile(rf"{flag}{_ANTES}{re.escape(nome)}{_DEPOIS}"), marcador))
        self._variantes.sort(key=lambda x: -len(x[0].pattern))
        if marcador == "[CLIENTE]":
            self.para_nome.setdefault(marcador, nome)

    def ocultar(self, txt):
        if not txt:
            return txt
        for rx, marc in self._variantes:
            txt = rx.sub(marc, txt)
        return txt

    def reidentificar(self, txt):
        if not txt or not isinstance(txt, str):
            return txt
        return re.sub(r"\[(USUARIO_[\w-]+|CLIENTE)\]",
                      lambda m: self.para_nome.get(m.group(0), m.group(0)), txt)


# ---------------------------------------------------------------- camada 2: imagens (OCR local)
_PNG_RTF = re.compile(r"\\pngblip\b(?:\\[a-z]+-?\d*\s?|\s)*([0-9a-fA-F\s]+)")


def extrair_pngs_rtf(rtf):
    """Retorna a lista de PNGs (bytes) embutidos no RichText, na ordem em que aparecem."""
    if not rtf:
        return []
    if isinstance(rtf, (bytes, bytearray)):
        rtf = rtf.decode("cp1252", errors="ignore")
    imgs = []
    for m in _PNG_RTF.finditer(str(rtf)):
        hx = re.sub(r"\s", "", m.group(1))
        if len(hx) < 200:
            continue
        try:
            imgs.append(bytes.fromhex(hx[: len(hx) // 2 * 2]))
        except ValueError:
            continue
    return imgs


class LeitorOCR:
    def __init__(self, tesseract_cmd="", idioma="por+eng", min_lado=80, max_chars=1500):
        self.ok = False
        self.idioma = idioma
        self.min_lado = min_lado
        self.max_chars = max_chars
        try:
            import pytesseract
            from PIL import Image  # noqa: F401
            if tesseract_cmd:
                pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
            disponiveis = set(pytesseract.get_languages(config=""))
            pedidos = [i for i in idioma.split("+") if i in disponiveis]
            if not pedidos:
                log.warning("OCR: idioma(s) '%s' não instalado(s) no Tesseract (tem: %s); usando eng",
                            idioma, ",".join(sorted(disponiveis)))
                pedidos = ["eng"]
            elif len(pedidos) < len(idioma.split("+")):
                log.warning("OCR: parte dos idiomas '%s' não está instalada; usando %s", idioma, "+".join(pedidos))
            self.idioma = "+".join(pedidos)
            self._pt = pytesseract
            self.ok = True
        except Exception as e:
            log.warning("OCR desativado (Tesseract/pytesseract indisponível): %s", e)

    def ler(self, png, ampliar=True, max_chars=None):
        """OCR de uma imagem (bytes). ampliar=True para prints de tela; False para foto/escaneado."""
        if not self.ok:
            return ""
        from PIL import Image, ImageFile, ImageOps
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            im = Image.open(io.BytesIO(png))
            im.load()
            im = ImageOps.exif_transpose(im)       # foto de celular: corrige a rotação
            if min(im.size) < self.min_lado:      # ícone, logo, assinatura
                return ""
            im = im.convert("L")
            if ampliar and im.width < 1600:        # print de tela: ampliar melhora muito o OCR
                f = 2 if im.width >= 800 else 3
                im = im.resize((im.width * f, im.height * f))
            elif im.width > 3000:                  # foto grande: reduz para acelerar
                im = im.resize((3000, int(im.height * 3000 / im.width)))
            txt = self._pt.image_to_string(im, lang=self.idioma, timeout=60)
        except Exception as e:
            log.warning("OCR falhou numa imagem: %s", e)
            return ""
        txt = re.sub(r"[ \t]+", " ", txt)
        txt = re.sub(r"\n\s*\n+", "\n", txt).strip()
        return txt[: (max_chars or self.max_chars)]
