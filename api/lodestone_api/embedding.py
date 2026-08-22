"""Query-side embedding.

The crawler embeds summaries; this embeds the search box. Both must use the
same model -- vectors from different models are not comparable, and cosine
distance between them is noise that looks like a result.

Deliberately a separate, minimal client rather than an import from the crawler
package: the API is deployable on its own (it needs no residential egress), and
should not pull in the crawler's dependencies to do it.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

EMBEDDING_HOST = os.environ.get("LODESTONE_EMBEDDING_HOST", "http://127.0.0.1:11434")
EMBEDDING_MODEL = os.environ.get("LODESTONE_EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_DIMENSIONS = 768
EMBEDDING_TIMEOUT_SECONDS = 30


class EmbeddingUnavailable(RuntimeError):
    """The embedding server could not be reached or returned something unusable.

    Semantic search is an enhancement, not the core product, so callers should
    degrade to keyword search rather than failing the whole request.
    """


def embedQuery(text: str) -> list[float]:
    payload = json.dumps({"model": EMBEDDING_MODEL, "input": [text]}).encode()
    request = urllib.request.Request(
        f"{EMBEDDING_HOST}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=EMBEDDING_TIMEOUT_SECONDS) as response:
            body = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise EmbeddingUnavailable(f"embedding server unreachable: {error}") from error

    vectors = body.get("embeddings") or []
    if not vectors:
        raise EmbeddingUnavailable("embedding server returned no vector")

    vector = vectors[0]
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise EmbeddingUnavailable(
            f"model returned {len(vector)} dimensions, expected {EMBEDDING_DIMENSIONS}"
        )
    return vector


def toPgVector(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
