"""CSS da tela de login (3 propostas). Só é injetado enquanto ninguém está logado."""

_BASE = """
<style>
[data-testid="stHeader"] {{ background: transparent; }}
[data-testid="stToolbar"] * {{ color: {sub} !important; }}
[data-testid="stApp"] {{ background: {fundo}; background-attachment: fixed; }}
[data-testid="stMainBlockContainer"] {{ padding-top: 9vh; }}
h1 {{ text-align: center; font-size: 1.9rem !important; color: {titulo} !important; font-weight: 700; letter-spacing: -0.5px;
      text-shadow: {sombra_titulo}; padding-bottom: 0.2rem; }}
h3 {{ color: {texto} !important; font-size: 1.1rem !important; padding-bottom: 0.3rem; }}
[data-testid="stForm"] {{ background: {card}; border: 1px solid {borda_card} !important; border-radius: 14px;
      padding: 1.6rem 1.6rem 1.2rem; box-shadow: {sombra_card}; backdrop-filter: blur(10px); }}
[data-testid="stForm"] [data-testid="stWidgetLabel"] p {{ color: {label} !important; font-weight: 600; }}
/* campos: cobre o DOM novo (stTextInputRootElement) e o antigo (baseweb) do Streamlit */
[data-testid="stForm"] [data-testid="stTextInputRootElement"],
[data-testid="stForm"] div[data-baseweb="input"] {{ background: {campo} !important;
      border: 1px solid {borda_campo} !important; border-radius: 8px; color: {texto_campo} !important; }}
[data-testid="stForm"] div[data-baseweb="input"] > div,
[data-testid="stForm"] div[data-baseweb="base-input"] {{ background: transparent !important; }}
[data-testid="stForm"] [data-testid="stTextInputRootElement"]:focus-within,
[data-testid="stForm"] div[data-baseweb="input"]:focus-within {{ border-color: {destaque} !important;
      box-shadow: 0 0 0 3px {foco}; }}
[data-testid="stForm"] input,
[data-testid="stForm"] input:focus,
[data-testid="stForm"] input:hover {{ background: transparent !important; color: {texto_campo} !important;
      -webkit-text-fill-color: {texto_campo} !important; caret-color: {destaque} !important; }}
[data-testid="stForm"] input::placeholder {{ color: {sub} !important; -webkit-text-fill-color: {sub} !important; }}
[data-testid="stForm"] input::selection {{ background: {foco}; color: {texto_campo}; }}
/* preenchimento automático do navegador (Chrome pinta o fundo e a fonte por conta própria) */
[data-testid="stForm"] input:-webkit-autofill,
[data-testid="stForm"] input:-webkit-autofill:hover,
[data-testid="stForm"] input:-webkit-autofill:focus {{ -webkit-box-shadow: 0 0 0 1000px {campo} inset !important;
      -webkit-text-fill-color: {texto_campo} !important; transition: background-color 9999s; }}
[data-testid="stForm"] [data-testid="stTextInputRootElement"] button,
[data-testid="stForm"] div[data-baseweb="input"] button {{ color: {sub} !important; }}
[data-testid="stForm"] [data-testid="InputInstructions"] {{ color: {sub} !important; }}
[data-testid="stForm"] [data-testid="stElementContainer"]:has([data-testid="stFormSubmitButton"]),
[data-testid="stForm"] [data-testid="stElementContainer"]:has([data-testid="stFormSubmitButton"]) > div,
[data-testid="stFormSubmitButton"] {{ width: 100% !important; }}
[data-testid="stFormSubmitButton"] button {{ background: {botao} !important; border: 0; color: #fff !important; font-weight: 600;
      width: 100%; padding: 0.55rem 0; border-radius: 8px; margin-top: 0.3rem; }}
[data-testid="stFormSubmitButton"] button:hover {{ filter: brightness(1.08); color: #fff; }}
</style>
"""

TEMAS = {
    # A - Azul corporativo: marinho -> azul petróleo, brilho suave
    "a": dict(
        fundo="radial-gradient(1200px 600px at 15% 10%, rgba(46,134,171,0.35), transparent 60%),"
              "radial-gradient(900px 500px at 90% 90%, rgba(31,111,139,0.35), transparent 60%),"
              "linear-gradient(135deg, #07142B 0%, #0C2547 45%, #103A5C 75%, #0E4C5E 100%)",
        titulo="#F4F7FB", sombra_titulo="0 2px 18px rgba(0,0,0,0.35)", sub="#9FB4CC", texto="#E8EEF6",
        card="rgba(10,22,42,0.78)", borda_card="rgba(159,180,204,0.22)", sombra_card="0 20px 50px rgba(0,0,0,0.45)",
        label="#DCE6F2", campo="#F4F7FB", borda_campo="transparent", texto_campo="#0C2547",
        destaque="#2E86AB", foco="rgba(46,134,171,0.35)", botao="linear-gradient(90deg, #1F6F8B, #2E86AB)"),
    # B - Grafite e índigo: escuro neutro com toques de índigo/violeta
    "b": dict(
        fundo="radial-gradient(1000px 520px at 85% 0%, rgba(99,102,241,0.30), transparent 60%),"
              "radial-gradient(900px 600px at 0% 100%, rgba(139,92,246,0.22), transparent 60%),"
              "linear-gradient(160deg, #0F1117 0%, #161A2B 50%, #1C1B3A 100%)",
        titulo="#F5F5FA", sombra_titulo="0 2px 22px rgba(99,102,241,0.35)", sub="#A5A9C7", texto="#ECECF5",
        card="rgba(22,24,38,0.82)", borda_card="rgba(129,140,248,0.25)", sombra_card="0 24px 60px rgba(0,0,0,0.55)",
        label="#E0E1F0", campo="#262A40", borda_campo="#3A3F5C", texto_campo="#F5F5FA",
        destaque="#818CF8", foco="rgba(129,140,248,0.35)", botao="linear-gradient(90deg, #4F46E5, #7C3AED)"),
    # C - Claro executivo: off-white -> azul acinzentado -> verde-água, cartão branco
    "c": dict(
        fundo="radial-gradient(1100px 600px at 10% 0%, rgba(147,197,253,0.45), transparent 60%),"
              "radial-gradient(1000px 600px at 100% 100%, rgba(153,214,203,0.55), transparent 60%),"
              "linear-gradient(135deg, #F6F8FC 0%, #E6EDF6 50%, #DDEEEA 100%)",
        titulo="#13294B", sombra_titulo="none", sub="#5A6B85", texto="#13294B",
        card="rgba(255,255,255,0.92)", borda_card="#D5DEEA", sombra_card="0 18px 45px rgba(19,41,75,0.15)",
        label="#1F2A44", campo="#F3F6FA", borda_campo="#CBD5E1", texto_campo="#13294B",
        destaque="#1F6FB2", foco="rgba(31,111,178,0.22)", botao="linear-gradient(90deg, #13294B, #1F6FB2)"),
}


def css(tema="c"):
    return _BASE.format(**TEMAS.get(tema, TEMAS["c"]))
