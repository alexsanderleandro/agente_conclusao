"""
Áudios de WhatsApp ligados ao atendimento, transcritos LOCALMENTE (faster-whisper, CPU).
O áudio nunca sai do servidor; só o texto transcrito vai para a IA, já mascarado pelo agente.

Onde o agente procura:
  1) caminhos citados no texto da iteração, ex.: M:\\GET\\Audios WhatsApp\\Atend_5910473 .ogg
  2) a subpasta de áudios da pasta do cliente, arquivos com o nº do atendimento no nome.
As transcrições ficam em cache no avaliacoes.db (não retranscreve o mesmo arquivo).
"""
import logging
import os
import re

log = logging.getLogger("agente")

EXTENSOES = (".ogg", ".opus", ".oga", ".mp3", ".m4a", ".wav", ".aac", ".amr")
_EXT_RX = "|".join(e.lstrip(".") for e in EXTENSOES)
# de "X:\" ou "\\servidor" até a extensão de áudio (o caminho pode ter espaços)
_CAMINHO = re.compile(rf"(?i)((?:[a-z]:\\|\\\\[\w.$-]+\\)[^\r\n\"<>|?*{{}}]*?\.(?:{_EXT_RX}))(?![\w])")


def _desescapar_rtf(t):
    t = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes.fromhex(m.group(1)).decode("cp1252", "ignore"), t)
    return t.replace("\\\\", "\\")


# qualquer arquivo citado (áudio, PDF, imagem...) - usado para descobrir a pasta do cliente pelo link
_CAMINHO_QQ = re.compile(r"(?i)((?:[a-z]:\\|\\\\[\w.$-]+\\)[^\r\n\"<>|?*{}]*?\.[a-z0-9]{2,4})(?![\w])")


def caminhos_no_texto(texto_bruto, texto_limpo="", qualquer_arquivo=False):
    """Caminhos de áudio (ou de qualquer arquivo) citados na iteração: no RTF bruto, inclusive
    em hyperlink, ou no texto limpo."""
    rx = _CAMINHO_QQ if qualquer_arquivo else _CAMINHO
    vistos, saida = set(), []
    bruto = texto_bruto.decode("cp1252", "ignore") if isinstance(texto_bruto, (bytes, bytearray)) else str(texto_bruto or "")
    for fonte in (_desescapar_rtf(bruto), texto_limpo or ""):
        for m in rx.finditer(fonte):
            c = m.group(1).strip()
            k = c.lower()
            if k not in vistos:
                vistos.add(k)
                saida.append(c)
    return saida


def mapear_unidade(caminho, mapa):
    """'M:\\GET\\x.ogg' com mapa {'M:\\': '\\\\srv\\dados\\'} -> '\\\\srv\\dados\\GET\\x.ogg' (serviço não vê M:)."""
    for de, para in (mapa or {}).items():
        if de and caminho.lower().startswith(de.lower()):
            caminho = para + caminho[len(de):]
            break
    return caminho.replace("\\", os.sep) if os.sep != "\\" else caminho


def pasta_cliente_pelos_links(textos_brutos, raiz, mapa=None):
    """Pasta do cliente a partir dos arquivos citados nas iterações: M:\\GET\\Audios...\\x.ogg -> M:\\GET.
    Pega o 1º nível abaixo da raiz; se os links apontarem para clientes diferentes, usa o mais citado."""
    raiz_n = os.path.normcase(os.path.normpath(raiz)).rstrip("\\/")   # "M:\\" -> "m:"
    contagem = {}
    for t in textos_brutos:
        for c in caminhos_no_texto(t, qualquer_arquivo=True):
            real = os.path.normpath(mapear_unidade(c, mapa))
            if not os.path.normcase(real).startswith(raiz_n + os.sep):
                continue
            primeiro = real[len(raiz_n) + 1:].split(os.sep)[0]
            if primeiro:
                p = (raiz if raiz.endswith(("\\", "/")) else raiz + os.sep) + primeiro
                contagem[p] = contagem.get(p, 0) + 1
    return max(contagem, key=contagem.get) if contagem else None


def audios_na_pasta(pasta, numatendimento):
    """Arquivos de áudio cujo nome contém o nº do atendimento como número inteiro (Atend_5910473 .ogg)."""
    if not pasta or not os.path.isdir(pasta) or not numatendimento:
        return []
    rx = re.compile(rf"(?<!\d){re.escape(str(numatendimento))}(?!\d)")
    achados = []
    for arq in os.listdir(pasta):
        c = os.path.join(pasta, arq)
        if arq.lower().endswith(EXTENSOES) and rx.search(arq) and os.path.isfile(c):
            achados.append(c)
    return sorted(achados, key=os.path.getmtime)


def duracao_segundos(caminho):
    try:
        import av
        with av.open(caminho) as f:
            if f.duration:
                return f.duration / 1_000_000
            s = f.streams.audio[0]
            return float(s.duration * s.time_base) if s.duration else None
    except Exception:
        return None


class Transcritor:
    _modelo_cache = {}

    def __init__(self, modelo="small", dispositivo="cpu", lite=None, max_minutos=10, idioma="pt"):
        self.nome_modelo, self.dispositivo = modelo, dispositivo
        self.lite = lite
        self.max_seg = max_minutos * 60
        self.idioma = idioma
        self.ok = True
        try:
            import faster_whisper  # noqa: F401
        except Exception as e:
            log.warning("Áudios desativados (faster-whisper indisponível): %s", e)
            self.ok = False
        if lite is not None:
            lite.execute("""CREATE TABLE IF NOT EXISTS transcricoes(
                caminho TEXT PRIMARY KEY, tamanho INTEGER, mtime REAL, texto TEXT, em TEXT)""")
            lite.commit()

    def _modelo(self):
        k = (self.nome_modelo, self.dispositivo)
        if k not in Transcritor._modelo_cache:
            from faster_whisper import WhisperModel
            log.info("Carregando modelo de transcrição '%s' (1ª vez baixa o modelo)...", self.nome_modelo)
            Transcritor._modelo_cache[k] = WhisperModel(self.nome_modelo, device=self.dispositivo,
                                                        compute_type="int8")
        return Transcritor._modelo_cache[k]

    def transcrever(self, caminho):
        """Texto do áudio, '' se não der. Usa o cache quando o arquivo não mudou."""
        if not self.ok:
            return ""
        try:
            st = os.stat(caminho)
        except OSError as e:
            log.warning("Áudio não encontrado/inacessível: %s (%s)", caminho, e)
            return ""
        chave = os.path.normcase(os.path.abspath(caminho))
        if self.lite is not None:
            r = self.lite.execute("SELECT texto FROM transcricoes WHERE caminho=? AND tamanho=? AND mtime=?",
                                  (chave, st.st_size, st.st_mtime)).fetchone()
            if r:
                return r[0]
        dur = duracao_segundos(caminho)
        if dur and dur > self.max_seg:
            texto = f"(áudio de {dur/60:.0f} min, acima do limite de {self.max_seg/60:.0f} min; não transcrito)"
        else:
            try:
                segs, _ = self._modelo().transcribe(caminho, language=self.idioma, vad_filter=True, beam_size=1)
                texto = " ".join(s.text.strip() for s in segs).strip()
            except Exception as e:
                log.warning("Falha ao transcrever %s: %s", caminho, e)
                return ""
        if self.lite is not None:
            from datetime import datetime
            self.lite.execute("INSERT OR REPLACE INTO transcricoes VALUES (?,?,?,?,?)",
                              (chave, st.st_size, st.st_mtime, texto, datetime.now().isoformat(timespec="seconds")))
            self.lite.commit()
        return texto
