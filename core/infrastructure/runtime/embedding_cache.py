from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=256)
def cached_openai_embedding(api_key: str, model: str, query: str) -> tuple[float, ...]:
    import openai

    client = openai.OpenAI(api_key=api_key)
    response = client.embeddings.create(input=[query], model=model)
    return tuple(float(value) for value in response.data[0].embedding)
