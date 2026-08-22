"""Pretty-print a /api/search payload for the smoke test."""
import json
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else "ratio"
payload = json.load(sys.stdin)

for story in payload["results"]:
    if mode == "ratio":
        metric = f"{float(story['favorites_per_1k_words']):>8.1f} favs/1K"
    else:
        metric = f"{story['favorite_count']:>8,} favs   "
    print(f"  {metric}  {story['word_count']:>7,}w  {story['title'][:46]}")
