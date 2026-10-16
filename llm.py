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


# uso acumulado no processo (só para medição): "cache" = tokens de entrada lidos do cache do provedor, que custam
# bem menos. Quem quer medir um trecho guarda o valor antes e faz a diferença depois.
USO = {"chamadas": 0, "cache": 0}


def _cache(n):
    USO["chamadas"] += 1
    USO["cache"] += int(n or 0)


# modelos que recusaram a temperatura (ex.: os de raciocínio da OpenAI): dali em diante chama sem ela
_SEM_TEMPERATURA = set()


def chamar(provedor, client, modelo, system, user, max_tokens=800, json_mode=True, temperatura=None):
    """Retorna (texto, tokens_entrada, tokens_saida). Os tokens lidos do cache vão para USO["cache"].
    temperatura: 0 = respostas estáveis (o mesmo caso dá a mesma decisão); None = padrão do provedor (varia)."""
    provedor = provedor.strip().lower()
    system, user = texto_seguro(system), texto_seguro(user)
    if temperatura is not None and (provedor, modelo) in _SEM_TEMPERATURA:
        temperatura = None
    try:
        return _chamar(provedor, client, modelo, system, user, max_tokens, json_mode, temperatura)
    except Exception as e:
        if temperatura is None or "temperature" not in str(e).lower():
            raise
        _SEM_TEMPERATURA.add((provedor, modelo))   # o modelo não aceita o parâmetro: repete sem ele
        return _chamar(provedor, client, modelo, system, user, max_tokens, json_mode, None)


def _chamar(provedor, client, modelo, system, user, max_tokens, json_mode, temperatura):
    if provedor == "openai":
        kw = {"response_format": {"type": "json_object"}} if json_mode else {}
        if temperatura is not None:
            kw["temperature"] = temperatura
        r = client.chat.completions.create(
            model=modelo, max_completion_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)
        _cache(getattr(getattr(r.usage, "prompt_tokens_details", None), "cached_tokens", 0))
        return r.choices[0].message.content or "", r.usage.prompt_tokens, r.usage.completion_tokens
    if provedor == "anthropic":
        kw = {"temperature": temperatura} if temperatura is not None else {}
        r = client.messages.create(model=modelo, max_tokens=max_tokens, system=system,
                                   messages=[{"role": "user", "content": user}], **kw)
        texto = "".join(b.text for b in r.content if b.type == "text")
        _cache(getattr(r.usage, "cache_read_input_tokens", 0))
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
                temperature=temperatura,
            ))
        u = r.usage_metadata
        _cache(getattr(u, "cached_content_token_count", 0))
        return (r.text or ""), (u.prompt_token_count or 0), (u.candidates_token_count or 0)
    raise ValueError(f"Provedor desconhecido: {provedor}")


def testar_chave(provedor, modelo, chave):
    """Chamada mínima para validar chave + modelo. Levanta exceção se falhar."""
    client = criar_client(provedor, chave)
    chamar(provedor, client, modelo, "Responda apenas OK.", "teste", max_tokens=20, json_mode=False)
