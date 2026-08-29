"""Summary embeddings for semantic search.

FFN's search matches literal words in titles and summaries. That makes the
query readers actually have -- "competent time-travel Harry who isn't a Gary
Stu" -- inexpressible, because none of those words need appear in the summary of
a story that is exactly it. Embedding the summaries turns that into a nearest
-neighbour lookup.

Only summaries are embedded. Story text is never fetched or stored, which is
what keeps this inside the `use=reference` signal in FFN's robots.txt, and the
vectors are never used to train anything (`ai-train=no`).

Backend: an OpenAI-shaped local embedding server, Ollama by default. The
alternative would be sentence-transformers, which drags in ~2GB of torch for a
90MB model; going over HTTP to a local daemon keeps this package light and lets
the model be swapped without touching code. `EMBEDDING_DIMENSIONS` must match
the `vector(768)` column in the schema -- a mismatch is rejected loudly rather
than silently truncating.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, Optional, Sequence

import psycopg
from curl_cffi import requests

logger = logging.getLogger(__name__)

EMBEDDING_HOST = os.environ.get("LODESTONE_EMBEDDING_HOST", "http://127.0.0.1:11434")
EMBEDDING_MODEL = os.environ.get("LODESTONE_EMBEDDING_MODEL", "nomic-embed-text")

# Must equal the vector(N) width in db/schema.sql.
EMBEDDING_DIMENSIONS = 768

# Titles carry real signal -- "Time Traveler" says more than many summaries --
# so the embedded text is title plus summary rather than summary alone.
MAX_INPUT_CHARACTERS = 2000


class EmbeddingError(RuntimeError):
    pass


def buildEmbeddingText(title: str, summary: str) -> str:
    return f"{title}. {summary}".strip()[:MAX_INPUT_CHARACTERS]


# The model server shares a GPU with whatever else the machine is doing, so a
# request can stall while the model is swapped back in. Measured: a single
# embed took 3.8s on an idle card and 17s on a busy one, and a 64-item batch
# exceeded a 120s ceiling entirely.
EMBED_TIMEOUT_SECONDS = 600


def embedBatch(texts: Sequence[str], timeoutSeconds: int = EMBED_TIMEOUT_SECONDS) -> list[list[float]]:
    """Embed a batch of strings via the local embedding server."""
    if not texts:
        return []

    try:
        response = requests.post(
            f"{EMBEDDING_HOST}/api/embed",
            json={"model": EMBEDDING_MODEL, "input": list(texts)},
            timeout=timeoutSeconds,
        )
    except Exception as error:
        raise EmbeddingError(f"embedding request failed: {error}") from error

    if response.status_code != 200:
        raise EmbeddingError(f"embedding server returned {response.status_code}: {response.text[:200]}")

    vectors = response.json().get("embeddings")
    if not vectors:
        raise EmbeddingError("embedding server returned no embeddings")

    for vector in vectors:
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise EmbeddingError(
                f"model {EMBEDDING_MODEL} returns {len(vector)} dimensions, but the "
                f"schema column is vector({EMBEDDING_DIMENSIONS}). Change the model "
                f"or migrate the column -- do not truncate."
            )
    return vectors


def toPgVector(vector: Iterable[float]) -> str:
    """pgvector accepts its literal text form, which avoids needing an adapter."""
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"


def backfillEmbeddings(
    connectionString: str,
    batchSize: int = 64,
    limit: Optional[int] = None,
) -> int:
    """Embed every story whose vector is missing.

    The upsert nulls `summary_embedding` whenever the summary text changes, so
    this same pass also refreshes stories whose blurb was edited -- no separate
    invalidation job, and no recomputing vectors that are still correct.
    """
    embeddedCount = 0

    with psycopg.connect(connectionString) as connection:
        while True:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT story_id, title, summary
                    FROM stories
                    WHERE summary_embedding IS NULL AND deleted_at IS NULL
                    ORDER BY story_id
                    LIMIT %s
                    """,
                    (batchSize,),
                )
                rows = cursor.fetchall()

            if not rows:
                break

            texts = [buildEmbeddingText(title, summary) for _, title, summary in rows]
            try:
                vectors = embedBatch(texts)
            except EmbeddingError as error:
                # A stalled model server is transient and common on a shared
                # GPU. Stop this pass cleanly and let the caller retry rather
                # than losing the rows already embedded in this run.
                logger.warning("batch failed after %d embedded, stopping pass: %s",
                               embeddedCount, error)
                return embeddedCount

            with connection.cursor() as cursor:
                cursor.executemany(
                    "UPDATE stories SET summary_embedding = %s::halfvec WHERE story_id = %s",
                    [(toPgVector(vector), row[0]) for vector, row in zip(vectors, rows)],
                )
            connection.commit()

            embeddedCount += len(rows)
            logger.info("embedded %d stories", embeddedCount)

            if limit is not None and embeddedCount >= limit:
                break

    return embeddedCount


def buildVectorIndex(connectionString: str, maintenanceWorkMemory: str = "2GB") -> None:
    """Build the HNSW index over the binary-quantized column.

    Indexing the bit column rather than the vector is what makes this possible
    on modest hardware: 96 bytes per story instead of ~4KB, so the graph fits in
    a couple of GB rather than the ~28GB a full-precision index would need.

    Deliberately not in schema.sql -- one bulk build is far cheaper than
    maintaining the graph across millions of inserts, so this runs after a
    backfill rather than before.
    """
    with psycopg.connect(connectionString) as connection, connection.cursor() as cursor:
        # Session-scoped, so it cannot leave the server permanently reconfigured.
        cursor.execute(f"SET maintenance_work_mem = '{maintenanceWorkMemory}'")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS stories_embedding_bits_idx ON stories "
            "USING hnsw (summary_embedding_bits bit_hamming_ops)"
        )
        connection.commit()
    logger.info("HNSW index ready (binary-quantized, %s build memory)", maintenanceWorkMemory)
