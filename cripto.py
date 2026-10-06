"""
Chaves de API por usuário, gravadas criptografadas no config.ini:

    [chaves_api]
    alex = gAAAAAB...   (Fernet: provedor + modelo + chave, tudo cifrado)

A chave-mestra fica FORA do config.ini: variável de ambiente AGENTE_CHAVE_MESTRA ou,
se não existir, o arquivo chave_mestra.key na pasta do agente (criado na 1ª gravação).
Quem tiver o config.ini E a chave-mestra consegue decifrar; por isso os dois ficam fora do git
e a pasta deve ter acesso restrito.
"""
import json
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

BASE = Path(__file__).parent
CFG_PATH = BASE / "config.ini"
ARQ_MESTRA = BASE / "chave_mestra.key"
SECAO = "chaves_api"


def _fernet(criar=False):
    k = os.environ.get("AGENTE_CHAVE_MESTRA")
    if not k:
        if ARQ_MESTRA.exists():
            k = ARQ_MESTRA.read_text(encoding="ascii").strip()
        elif criar:
            k = Fernet.generate_key().decode()
            ARQ_MESTRA.write_text(k, encoding="ascii")
        else:
            raise RuntimeError("Chave-mestra não encontrada (chave_mestra.key ou AGENTE_CHAVE_MESTRA)")
    return Fernet(k.encode() if isinstance(k, str) else k)


def _chave_usuario(usuario):
    # configparser grava as chaves em minúsculas; ":" "=" e "[" não podem aparecer na chave
    return re.sub(r"[:=\[\]\s]", "_", str(usuario).strip().lower())


def cifrar(provedor, modelo, chave_api):
    dados = json.dumps({"provedor": provedor, "modelo": modelo, "chave": chave_api})
    return _fernet(criar=True).encrypt(dados.encode()).decode()


def decifrar(token):
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except InvalidToken:
        raise RuntimeError("Não foi possível decifrar a chave (chave-mestra diferente da usada para gravar)")


def carregar(cfg, usuario):
    """{'provedor','modelo','chave'} do usuário, ou None se ele ainda não cadastrou."""
    k = _chave_usuario(usuario)
    if not cfg.has_section(SECAO) or not cfg.has_option(SECAO, k):
        return None
    return decifrar(cfg.get(SECAO, k))


def salvar(usuario, provedor, modelo, chave_api, caminho=CFG_PATH):
    """Grava/atualiza a linha do usuário em [chaves_api] sem mexer no resto do config.ini."""
    k = _chave_usuario(usuario)
    linha = f"{k} = {cifrar(provedor, modelo, chave_api)}"
    with open(caminho, encoding="utf-8", newline="") as f:
        txt = f.read()
    nl = "\r\n" if "\r\n" in txt else "\n"
    sec = re.search(rf"(?m)^\[{SECAO}\][ \t]*\r?$", txt)
    if not sec:
        txt = txt.rstrip("\r\n") + f"{nl}{nl}[{SECAO}]{nl}{linha}{nl}"
    else:
        prox = re.search(r"(?m)^\[", txt[sec.end():])
        fim = sec.end() + (prox.start() if prox else len(txt) - sec.end())
        corpo = txt[sec.end():fim]
        rx = re.compile(rf"(?m)^{re.escape(k)}[ \t]*=[^\r\n]*")
        if rx.search(corpo):
            corpo = rx.sub(lambda _: linha, corpo, count=1)
        else:
            corpo = corpo.rstrip("\r\n") + f"{nl}{linha}{nl}" + (nl if prox else "")
        txt = txt[:sec.end()] + corpo + txt[fim:]
    tmp = Path(str(caminho) + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(txt)
    os.replace(tmp, caminho)
