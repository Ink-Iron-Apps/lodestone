"""Benchmark embedding throughput against the local model server.

Answers the question that actually gates a full backfill on a shared machine:
how many GPU-hours does embedding the whole corpus cost, and how much VRAM does
it hold while doing it.

Run: LODESTONE_EMBEDDING_HOST=... python scripts/bench_embedding.py
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request

HOST = os.environ.get("LODESTONE_EMBEDDING_HOST", "http://127.0.0.1:11434")
MODEL = os.environ.get("LODESTONE_EMBEDDING_MODEL", "nomic-embed-text")
TARGET_STORIES = 8_770_708

SAMPLE_SUMMARY = (
    "An angel and a demon who have been on Earth since the Beginning discover "
    "the Antichrist has been misplaced, and must avert an apocalypse neither of "
    "them particularly wants. Featuring a bookshop, a vintage Bentley, and "
    "several increasingly desperate lies to their respective head offices."
)


def readVramMegabytes() -> int | None:
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip().splitlines()
        return int(output[0])
    except Exception:
        return None


def embed(texts: list[str]) -> int:
    payload = json.dumps({"model": MODEL, "input": texts}).encode()
    request = urllib.request.Request(
        f"{HOST}/api/embed", data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=600) as response:
        return len(json.load(response).get("embeddings") or [])


if __name__ == "__main__":
    print(f"host={HOST} model={MODEL}")
    embed([SAMPLE_SUMMARY])  # warm the model so load time is excluded

    vramBefore = readVramMegabytes()
    print(f"VRAM with model resident: {vramBefore} MiB\n")

    print(f"{'batch':>7} {'seconds':>9} {'stories/s':>11} {'GPU-hours for corpus':>22}")
    for batchSize in (16, 64, 256):
        texts = [f"{SAMPLE_SUMMARY} Variation {index}." for index in range(batchSize)]
        startedAt = time.monotonic()
        returned = embed(texts)
        elapsed = time.monotonic() - startedAt
        rate = returned / elapsed if elapsed else 0
        gpuHours = TARGET_STORIES / rate / 3600 if rate else float("inf")
        print(f"{batchSize:>7} {elapsed:>9.2f} {rate:>11,.0f} {gpuHours:>22,.1f}")

    vramAfter = readVramMegabytes()
    if vramBefore is not None and vramAfter is not None:
        print(f"\nVRAM held during work: {vramAfter} MiB (delta {vramAfter - vramBefore:+d} MiB)")
