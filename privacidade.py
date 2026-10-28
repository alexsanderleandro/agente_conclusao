"""
Proteção de dados (LGPD) antes de enviar qualquer texto para a IA.

Camada 1 - mascaramento: CPF, CNPJ, chave NF-e, e-mail, telefone, cartão, IP e senhas viram
           marcadores ([CPF], [SENHA]...). Nomes de usuários do ERP, do cliente e das pessoas do
           cliente registradas como contato viram pseudônimos ([USUARIO_156], [CLIENTE],
           [CONTATO_CLIENTE_1]) e voltam ao nome real só aqui, localmente, no resumo/justificativa
           que a IA devolve.
Camada 2 - imagens: os PNG embutidos no RichText são lidos por OCR local (Tesseract).
           A imagem NUNCA é enviada; só o texto lido, já mascarado.
"""
import io
import logging
import re
import unicodedata

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
# NomeContato que não é nome de pessoa: não vira [CONTATO_CLIENTE_n]
_NAO_E_NOME = frozenset("""cliente clientes financeiro fiscal compras vendas loja recepcao gerente gerencia escritorio
empresa contabilidade contador contadora ti suporte caixa setor dono dona proprietario proprietaria responsavel
administrativo adm comercial sr sra senhor senhora deposito estoque faturamento diretoria secretaria rh matriz
filial unidade nao informado usuario""".split())


def _sem_acento(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()


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
        self._contatos = {}           # 1º nome do contato do cliente (sem acento) -> marcador
        variantes = []                # (prioridade, regex, marcador): 0 nomes compostos e cliente, 1 contato do
                                      # cliente (1º nome), 2 1º nome e login de uma palavra dos usuários
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
                    variantes.append((0 if re.search(r"[\s._]", v) else 2,
                                      re.compile(rf"(?i){_ANTES}{re.escape(v)}{_DEPOIS}"), marc))
            p = re.split(r"[.\s_]", nome)[0]
            if len(p) >= 4:
                primeiros.setdefault(p.capitalize(), set()).add(marc)
        # primeiro nome só se não for ambíguo; case-sensitive (evita trocar palavras comuns)
        for p, marcs in primeiros.items():
            if len(marcs) == 1:
                variantes.append((2, re.compile(rf"{_ANTES}{re.escape(p)}{_DEPOIS}"), next(iter(marcs))))
        if cliente and len(str(cliente).strip()) >= 3:
            c = str(cliente).strip()
            self.para_nome["[CLIENTE]"] = c
            for v in _variantes_cliente(c):
                variantes.append((0, re.compile(rf"(?i){_ANTES}{re.escape(v)}{_DEPOIS}"), "[CLIENTE]"))
        self._variantes = variantes
        self._ordenar()

    def _ordenar(self):
        # pela prioridade e, nela, mais longos primeiro ("Rafael Freitas" antes de "Rafael")
        self._variantes.sort(key=lambda x: (x[0], -len(x[1].pattern)))

    def adicionar(self, nome, marcador="[CLIENTE]"):
        """Variante extra (ex.: nome da pasta do cliente na rede, "GET", "MinasFreios").
        Nomes curtos (até 4 letras) só casam com a grafia exata, para não trocar palavras comuns."""
        nome = str(nome or "").strip()
        if len(nome) < 3:
            return
        flag = "" if len(nome) <= 4 else "(?i)"
        self._variantes.append((0, re.compile(rf"{flag}{_ANTES}{re.escape(nome)}{_DEPOIS}"), marcador))
        self._ordenar()
        if marcador == "[CLIENTE]":
            self.para_nome.setdefault(marcador, nome)

    def adicionar_contato(self, nome):
        """Pessoa do cliente registrada como contato da interação (NomeContato) -> [CONTATO_CLIENTE_n]: o nome não
        vai para a IA e ela sabe que quem falou ou fez foi o cliente (antes o 1º nome ia em claro, ou virava
        [USUARIO_n] quando coincidia com um usuário: 93 de 193 decisões de 19 a 27/10). O 1º nome (como registrado,
        Capitalizado ou, com 5+ letras, em maiúsculas; não troca "clara" minúsculo) passa na frente do 1º nome e do
        login de uma palavra dos usuários; nome composto de usuário e o nome do cliente continuam antes.
        Devolve o marcador (None se não parecer nome de pessoa)."""
        nome = re.sub(r"\s+", " ", str(nome or "")).strip()
        partes = [p for p in re.split(r"[\s._\-/()]+", nome) if p]
        if not partes or len(partes[0]) < 3 or not partes[0].isalpha() or _sem_acento(partes[0]) in _NAO_E_NOME:
            return None
        primeiro = partes[0]
        marc = self._contatos.get(_sem_acento(primeiro))
        if marc is None:
            marc = self._contatos[_sem_acento(primeiro)] = f"[CONTATO_CLIENTE_{len(self._contatos) + 1}]"
            self.para_nome[marc] = nome
        novos = [(1, rf"{_ANTES}{re.escape(v)}{_DEPOIS}") for v in
                 {primeiro, primeiro.capitalize()} | ({primeiro.upper()} if len(primeiro) >= 5 else set())]
        if len(partes) > 1:
            novos.append((0, rf"(?i){_ANTES}{re.escape(nome)}{_DEPOIS}"))
        ja = {(rx.pattern, m) for _, rx, m in self._variantes}
        self._variantes += [(pr, re.compile(p), marc) for pr, p in novos if (p, marc) not in ja]
        self._ordenar()
        return marc

    def ocultar(self, txt):
        if not txt:
            return txt
        for _, rx, marc in self._variantes:
            txt = rx.sub(marc, txt)
        return txt

    def reidentificar(self, txt):
        if not txt or not isinstance(txt, str):
            return txt
        return re.sub(r"\[(USUARIO_[\w-]+|CLIENTE|CONTATO_CLIENTE_\d+)\]",
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

    def ler_print(self, png):
        """OCR de um print anexado ao atendimento. Print de conversa do WhatsApp sai com as falas separadas por quem
        falou (CLIENTE = recebidas, à esquerda; ANALISTA = enviadas pela empresa, à direita), na ordem da conversa.
        -> {"conversa": bool, "texto": str, "cliente": bool (há fala do cliente),
            "confirma": bool (fala do cliente confirma o resultado ou agradece: confirma_resultado)}"""
        vazio = {"conversa": False, "texto": "", "cliente": False, "confirma": False}
        if not self.ok:
            return vazio
        from PIL import Image, ImageFile, ImageOps
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            im = Image.open(io.BytesIO(png))
            im.load()
            im = ImageOps.exif_transpose(im).convert("RGB")
            if min(im.size) < self.min_lado:
                return vazio
            a = _np().asarray(im).astype("int32")
            if not _eh_conversa(a):
                return {**vazio, "texto": self.ler(png)}
            texto, cliente, confirma = self._ler_conversa(im, a)
        except Exception as e:
            log.warning("OCR falhou num print: %s", e)
            return vazio
        return {"conversa": True, "texto": texto, "cliente": cliente, "confirma": confirma}

    def _ler_conversa(self, im, a):
        np = _np()
        f = 2 if im.width >= 800 else 3
        cinza = im.convert("L")
        if im.width < 1600:                       # print de tela: ampliar melhora muito o OCR
            cinza = cinza.resize((im.width * f, im.height * f))
        else:
            f = 1
        d = self._pt.image_to_data(cinza, lang=self.idioma, timeout=60, output_type=self._pt.Output.DICT)
        linhas = {}
        for i, p in enumerate(d["text"]):
            p = (p or "").strip()
            if not p or float(d["conf"][i]) < 0:
                continue
            x, y, w, h = d["left"][i] / f, d["top"][i] / f, d["width"][i] / f, d["height"][i] / f
            k = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            l = linhas.setdefault(k, {"p": [], "x0": x, "y0": y, "x1": x + w, "y1": y + h})
            l["p"].append(p)
            l["x0"], l["y0"] = min(l["x0"], x), min(l["y0"], y)
            l["x1"], l["y1"] = max(l["x1"], x + w), max(l["y1"], y + h)
        fundo = _mascara(a, _WA_FUNDO, 4)
        # área da conversa (colunas com o fundo dela): no WhatsApp Web a lista de conversas fica à esquerda dela
        cols = _np().flatnonzero(fundo.mean(axis=0) >= 0.25)
        area = (int(cols[0]), int(cols[-1])) if cols.size else (0, a.shape[1] - 1)
        falas, outros = [], []
        for l in sorted(linhas.values(), key=lambda l: (l["y0"], l["x0"])):
            txt = re.sub(r"[ \t]+", " ", " ".join(l["p"])).strip()
            lado = _lado_fala(a, fundo, l, area)
            if lado:
                txt = _WA_MARCA.sub("", _WA_HORA.sub("", txt)).strip(" |«»>\"“”'")
                if not re.search(r"[^\W\d_]{2,}", txt) or _WA_ROTINA.match(txt) or _WA_CAIXA.search(txt):
                    continue          # só hora/marca de leitura, data, "Hoje", "Digite uma mensagem"...
                # linha seguinte do mesmo balão (mesmo lado, logo abaixo): continua a mesma fala
                ant = falas[-1] if falas else None
                if ant and ant["lado"] == lado and l["y0"] - ant["y1"] <= 0.8 * (l["y1"] - l["y0"]):
                    ant["txt"] += " " + txt
                    ant["y1"] = l["y1"]
                else:
                    falas.append({"lado": lado, "txt": txt, "y1": l["y1"]})
            elif re.search(r"[^\W\d_]{3,}", txt) and not _WA_ROTINA.match(txt) and not _WA_CAIXA.search(txt):
                outros.append(txt)
        partes = [f'{x["lado"]}: "{x["txt"]}"' for x in falas]
        # passou do limite: fica o FIM da conversa (é onde costuma estar o aceite do cliente)
        while partes and len(" | ".join(partes)) > self.max_chars:
            partes = partes[1:]
        texto = (("... | " if len(partes) < len(falas) else "") + " | ".join(partes)) or "(nenhuma fala legível)"
        if falas and not any(x["lado"] == "CLIENTE" for x in falas):
            # sem isso a IA chegou a "ver" aceite do cliente num print só com falas do analista (caso 437)
            texto += " (nenhuma fala do CLIENTE foi lida neste print)"
        if outros:
            texto += " | outros textos da imagem: " + " / ".join(outros)[:400]
        return texto, any(x["lado"] == "CLIENTE" for x in falas), any(
            x["lado"] == "CLIENTE" and confirma_resultado(x["txt"]) for x in falas)


# ---------------------------------------------------------------- prints de conversa do WhatsApp
# O analista anexa o print da conversa como prova do aceite do cliente, mas o OCR comum lê por colunas e mistura as
# falas. Cada linha é atribuída pelo balão em que está: ENVIADA por quem tirou o print (a empresa: balão verde, à
# direita) ou RECEBIDA (o cliente: balão branco, à esquerda, cercado pelo fundo da conversa). Cores do WhatsApp Web
# e do celular, claro e escuro (a medição de 21/10 nos prints dos atendimentos: fundo #F5F1EB, enviado #D9FDD3).
_WA_FUNDO = ((0xF5, 0xF1, 0xEB), (0xEF, 0xEA, 0xE2), (0xEC, 0xE5, 0xDD), (0xE5, 0xDD, 0xD5), (0x0B, 0x14, 0x1A))
_WA_ENVIADO = ((0xD9, 0xFD, 0xD3), (0xD7, 0xFE, 0xD3), (0xDC, 0xF8, 0xC6), (0xE7, 0xFF, 0xDB), (0x00, 0x5C, 0x4B))
_WA_RECEBIDO = ((0xFF, 0xFF, 0xFF), (0x20, 0x2C, 0x33), (0x1F, 0x2C, 0x34))
_WA_ROTINA = re.compile(r"(?i)^(hoje|ontem|digite uma mensagem|mensagem|online|digitando\W*|editada|encaminhada|"
                        r"\d+ mensage(m|ns) n[ãa]o lidas?|visto por [úu]ltimo.*|(segunda|ter[çc]a|quarta|quinta|"
                        r"sexta)(-feira)?|s[áa]bado|domingo|\d{1,2}/\d{1,2}/\d{2,4})\W*$")
# hora no canto do balão (+ marca de leitura das enviadas, que o OCR lê como "w", "4", "GW"...); o OCR às vezes
# perde os dois-pontos ("1433", "14334") ou lê o 1 como "j". Valor com vírgula ("130,00") não é hora: fica.
_WA_HORA = re.compile(r"\s+[jlI|]?(\d{1,2}[:.;]\s?\d{2}|\d{4,5})(\s+\S{1,2}|[^\w\s]{1,3})?\s*$")
_WA_MARCA = re.compile(r"(?i)\s*\b(editada|encaminhada)\b\W*$")       # rótulos do WhatsApp no fim do balão
_WA_CAIXA = re.compile(r"(?i)digite uma mensagem")                     # caixa de digitação (com os ícones)


# fala do cliente que confirma o resultado ou agradece. Um "ok" solto não basta (pode responder a um aviso, a uma
# informação parcial ou a um pedido de teste ainda sem resultado: teste de 22/10). Usada só na trava de contato.
_WA_CONFIRMA = re.compile(r"(?i)\b(deu (tudo )?certo|deu bom|funcionou|funcionando|resolveu|resolvemos|resolvid[oa]|"
                          r"consegui|conseguimos|conseguiu|certinho|obrigad[oa]s?|obg|valeu|agrade\w+|entendi|"
                          r"pode encerrar|pode finalizar)\b")
_WA_SEM_PROBLEMA = re.compile(r"(?i)\b(nenhum problema|sem problemas?|nenhum erro|sem erros?)\b")
# cliente dispensa o suporte ("não precisamos mais", "não quero mais", "desisti"): pode concluir com CQS 54 (gerente, 22/10)
_WA_DISPENSA = re.compile(r"(?i)\bn[ãa]o\s+(precis\w+|quer\w*)\s+mais\b|\bdesist\w+")


def confirma_resultado(txt):
    """A fala confirma o resultado, agradece ou dispensa o suporte? ("deu certo", "resolveu", "consegui", "não tivemos
    nenhum problema", "obrigado", "não precisamos mais", "pode encerrar"); com negação antes ("não deu certo",
    "não consegui", "não pode encerrar") não vale."""
    if _WA_SEM_PROBLEMA.search(txt) or _WA_DISPENSA.search(txt):
        return True
    return any(not re.search(r"(?i)\bn[ãa]o\s+(\w+\s+)?$", txt[:m.start()]) for m in _WA_CONFIRMA.finditer(txt))


def _np():
    import numpy
    return numpy


def _mascara(a, cores, tol):
    np = _np()
    m = np.zeros(a.shape[:2], dtype=bool)
    for c in cores:
        m |= (np.abs(a - np.array(c)) <= tol).all(axis=-1)
    return m


def _perto(cor, cores, tol):
    return any(all(abs(x - y) <= tol for x, y in zip(cor, c)) for c in cores)


def eh_print_conversa(png):
    """O PNG é print de conversa do WhatsApp? Só pelas cores, sem OCR (rápido; o testar.py usa para escolher casos)."""
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        im = Image.open(io.BytesIO(png)).convert("RGB")
        return min(im.size) >= 80 and _eh_conversa(_np().asarray(im).astype("int32"))
    except Exception:
        return False


def _eh_conversa(a):
    """Print de conversa do WhatsApp: o fundo da conversa ocupa boa parte da imagem e há balões."""
    passo = max(1, a.shape[1] // 600)             # amostra (rápido em print grande)
    s = a[::passo, ::passo]
    return bool(_mascara(s, _WA_FUNDO, 4).mean() >= 0.12
                and (_mascara(s, _WA_ENVIADO, 6).mean() >= 0.003 or _mascara(s, _WA_RECEBIDO, 6).mean() >= 0.01))


def _lado_fala(a, fundo, l, area):
    """'ANALISTA' (balão verde), 'CLIENTE' (balão branco/cinza com o fundo da conversa ao lado, começando à
    ESQUERDA da área da conversa) ou None (cabeçalho, lista de conversas, imagem enviada no meio da conversa, tela
    do sistema no mesmo print...). A cor do balão é a mais frequente na caixa da linha."""
    np = _np()
    H, W = fundo.shape
    x0, y0 = max(0, int(l["x0"]) - 2), max(0, int(l["y0"]) - 2)
    x1, y1 = min(W, int(l["x1"]) + 3), min(H, int(l["y1"]) + 3)
    if x1 <= x0 or y1 <= y0:
        return None
    caixa = a[y0:y1, x0:x1].reshape(-1, 3)
    u, c = np.unique((caixa[:, 0] << 16) | (caixa[:, 1] << 8) | caixa[:, 2], return_counts=True)
    m = int(u[c.argmax()])
    cor = (m >> 16 & 255, m >> 8 & 255, m & 255)
    r, g, b = cor
    if (_perto(cor, _WA_ENVIADO, 10) or (g >= 228 and g - r >= 25 and g - b >= 28)      # verde (e a citação nele)
            or (r <= 30 and 75 <= g <= 120 and g - b >= 8)):
        return "ANALISTA"
    a0, a1 = area
    if _perto(cor, _WA_RECEBIDO, 8) and a0 - 0.02 * W <= l["x0"] <= a0 + 0.35 * (a1 - a0):
        lg = max(20, 3 * (y1 - y0))
        esq, dir_ = fundo[y0:y1, max(0, x0 - lg):x0], fundo[y0:y1, x1:min(W, x1 + lg)]
        if max(esq.mean() if esq.size else 0, dir_.mean() if dir_.size else 0) >= 0.15:
            return "CLIENTE"
    return None
