"""Measure per-story resource cost and project it to a full corpus.

Everything here is measured against the live pilot database and live FFN pages.
The point is to answer "what hardware does this need" without guessing -- an
earlier estimate put storage at 43GB by extrapolating a row size taken before
the embedding column and its index existed.

Run: python scripts/measure_footprint.py
"""
from __future__ import annotations

import os
import re
import sys
import time

import psycopg
from curl_cffi import requests

TARGET_STORIES = 8_770_708  # from scripts/estimate_backfill.py
TOTAL_REQUESTS = 508_448

SAMPLE_PAGES = [
    "https://www.fanfiction.net/book/Harry-Potter/?&srt=1&r=10&p=2",
    "https://www.fanfiction.net/anime/Naruto/?&srt=1&r=10&p=50",
    "https://www.fanfiction.net/j/0/0/0/",
]

OBJECT_QUERY = """
SELECT c.relname AS name, pg_relation_size(c.oid) AS bytes
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'i')
ORDER BY bytes DESC
"""


def gigabytes(byteCount: float) -> str:
    return f"{byteCount / 1e9:>8.1f} GB"


def measureDatabase(connectionString: str) -> None:
    with psycopg.connect(connectionString) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM stories")
        storyCount = cursor.fetchone()[0]
        cursor.execute(OBJECT_QUERY)
        objects = cursor.fetchall()

    print(f"=== database, measured on {storyCount:,} stories ===")
    print(f"  {'object':26} {'bytes/story':>12} {'projected':>12}")

    projectedTotal = 0.0
    embeddingRelated = 0.0
    for name, byteCount in objects:
        if byteCount < 100_000:
            continue
        perStory = byteCount / storyCount
        projected = perStory * TARGET_STORIES
        projectedTotal += projected
        if "embedding" in name:
            embeddingRelated += projected
        print(f"  {name:26} {perStory:>12,.0f} {gigabytes(projected):>12}")

    # The embedding column itself lives in TOAST, which pg_relation_size on the
    # heap does not count.
    with psycopg.connect(connectionString) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT sum(pg_column_size(summary_embedding)) FROM stories")
        embeddingColumnBytes = cursor.fetchone()[0] or 0
    embeddingColumnPerStory = embeddingColumnBytes / storyCount
    embeddingColumnProjected = embeddingColumnPerStory * TARGET_STORIES
    projectedTotal += embeddingColumnProjected
    embeddingRelated += embeddingColumnProjected
    print(f"  {'summary_embedding (TOAST)':26} {embeddingColumnPerStory:>12,.0f} "
          f"{gigabytes(embeddingColumnProjected):>12}")

    print(f"\n  PROJECTED TOTAL{'':13}{gigabytes(projectedTotal)}")
    print(f"  of which embeddings + index  {gigabytes(embeddingRelated)}"
          f"  ({embeddingRelated / projectedTotal:.0%})")

    print("\n  If the vectors are the binding constraint:")
    print(f"    halfvec(768), 2 bytes/dim  {gigabytes(projectedTotal - embeddingRelated / 2)}"
          "   ~half the vector cost, negligible recall loss")
    print(f"    no HNSW index              "
          f"{gigabytes(projectedTotal - embeddingRelated * 0.57)}"
          "   semantic search falls back to a scan")
    print(f"    no embeddings at all       {gigabytes(projectedTotal - embeddingRelated)}"
          "   loses meaning-matching entirely")


def measureBandwidth() -> None:
    print("\n=== bandwidth ===")
    session = requests.Session(impersonate="chrome")
    wireBytes = []
    decodedBytes = []
    for url in SAMPLE_PAGES:
        response = session.get(url, timeout=30)
        # curl_cffi decompresses transparently; Content-Length is the wire size.
        onWire = int(response.headers.get("Content-Length") or len(response.content))
        wireBytes.append(onWire)
        decodedBytes.append(len(response.content))
        time.sleep(5)

    meanWire = sum(wireBytes) / len(wireBytes)
    meanDecoded = sum(decodedBytes) / len(decodedBytes)
    print(f"  mean page, decoded         {meanDecoded / 1024:>8,.0f} KB")
    print(f"  mean page, on the wire     {meanWire / 1024:>8,.0f} KB")
    print(f"  full backfill transfer     {gigabytes(meanWire * TOTAL_REQUESTS)}"
          f"  ({TOTAL_REQUESTS:,} requests)")


if __name__ == "__main__":
    dsn = os.environ.get("LODESTONE_DSN")
    if not dsn:
        print("set LODESTONE_DSN (scripts/dev.sh does this for you)", file=sys.stderr)
        sys.exit(1)
    measureDatabase(dsn)
    measureBandwidth()
