"""Embedding helpers for optional semantic task re-mention matching."""
import math
import os

import clients
import obs
from retry import call_with_retry


EMBEDDING_MODEL = os.environ.get(
    "YUI_EMBEDDING_MODEL", "text-multilingual-embedding-002"
)


def embed_text(text: str) -> list[float]:
    """Return the embedding values for *text* from the configured Gemini model."""
    response = call_with_retry(
        lambda: clients.gemini_client().models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
        )
    )
    return list(response.embeddings[0].values)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return cosine similarity, treating empty, mismatched, and zero vectors as 0."""
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        obs.warning("embedding dimension mismatch", len_a=len(a), len_b=len(b))
        return 0.0

    magnitude_a = math.sqrt(sum(value * value for value in a))
    magnitude_b = math.sqrt(sum(value * value for value in b))
    if not magnitude_a or not magnitude_b:
        return 0.0

    dot_product = sum(left * right for left, right in zip(a, b, strict=True))
    return dot_product / (magnitude_a * magnitude_b)


def is_semantic_match_enabled() -> bool:
    """Return whether optional semantic task re-mention matching is enabled."""
    return os.environ.get("YUI_SEMANTIC_MATCH", "1") == "1"
