"""Chamada à IA para os 3 provedores (OpenAI, Anthropic, Google), com a chave de cada usuário."""

PROVEDORES = {
    "openai": {"nome": "OpenAI", "modelo": "gpt-4.1-mini"},
    "anthropic": {"nome": "Anthropic", "modelo": "claude-haiku-4-5"},
    "google": {"nome": "Google", "modelo": "gemini-3.6-flash"},
}


def criar_client(provedor, chave):
    provedor = provedor.strip().lower()
    if provedor == "openai":
        from openai import OpenAI
        return OpenAI(api_key=chave)
    if provedor == "anthropic":
        import anthropic
        return anthropic.Anthropic(api_key=chave)
    if provedor == "google":
        from google import genai
        return genai.Client(api_key=chave)
    raise ValueError(f"Provedor desconhecido: {provedor}")


def texto_seguro(s):
    """Emoji do RichText (\\uNNNN) chega como par de "surrogates", que a API recusa.
    Junta os pares válidos (vira o emoji certo) e descarta os soltos."""
    s = "" if s is None else str(s)
    try:
        return s.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        import re
        return re.sub(r"[\ud800-\udfff]", "", s)


def chamar(provedor, client, modelo, system, user, max_tokens=800, json_mode=True):
    """Retorna (texto, tokens_entrada, tokens_saida)."""
    provedor = provedor.strip().lower()
    system, user = texto_seguro(system), texto_seguro(user)
    if provedor == "openai":
        kw = {"response_format": {"type": "json_object"}} if json_mode else {}
        r = client.chat.completions.create(
            model=modelo, max_completion_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)
        return r.choices[0].message.content or "", r.usage.prompt_tokens, r.usage.completion_tokens
    if provedor == "anthropic":
        r = client.messages.create(model=modelo, max_tokens=max_tokens, system=system,
                                   messages=[{"role": "user", "content": user}])
        texto = "".join(b.text for b in r.content if b.type == "text")
        return texto, r.usage.input_tokens, r.usage.output_tokens
    if provedor == "google":
        from google.genai import types
        r = client.models.generate_content(
            model=modelo, contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                # no Gemini o "raciocínio" consome o limite de saída: deixa folga
                max_output_tokens=max(max_tokens, 4000),
                response_mime_type="application/json" if json_mode else None,
            ))
        u = r.usage_metadata
        return (r.text or ""), (u.prompt_token_count or 0), (u.candidates_token_count or 0)
    raise ValueError(f"Provedor desconhecido: {provedor}")


def testar_chave(provedor, modelo, chave):
    """Chamada mínima para validar chave + modelo. Levanta exceção se falhar."""
    client = criar_client(provedor, chave)
    chamar(provedor, client, modelo, "Responda apenas OK.", "teste", max_tokens=20, json_mode=False)
