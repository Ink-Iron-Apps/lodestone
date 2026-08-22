"""Estimate the cost of a full-corpus backfill.

Uses measured figures from the pilot rather than assumptions:

  * rows per request      -- from the Good Omens walk (1,764 rows / 72 requests)
  * seconds per request   -- from the same walk, crawl-delay included
  * bytes per row         -- measured at 409B in Postgres

Fandom story counts come from the section directories. FFN abbreviates them
("852K"), so the totals are accurate to a few percent, not exact.

Run: python scripts/estimate_backfill.py
"""
from __future__ import annotations

import re
import sys
import time

from curl_cffi import requests

SECTION_SLUGS = ("anime", "book", "cartoon", "comic", "game", "misc", "movie", "play", "tv")

# --- measured on the Good Omens pilot, 2026-08-21/22 -----------------------
ROWS_PER_REQUEST = 1764 / 72          # 24.5; a full page is 25, last page is partial
SECONDS_PER_REQUEST = 7 * 60 / 72     # 5.83s wall, of which 5s is the robots.txt delay
BYTES_PER_ROW = 409

# Highest story id seen on Just In, 2026-08-21. A hard ceiling on the corpus:
# ids are sequential and never reassigned, so no estimate may exceed it.
HIGHEST_STORY_ID = 14_584_841
EMBEDDING_BYTES_PER_ROW = 768 * 4     # float32 vector(768)
CROSSOVER_ROWS_PER_PAIR = 277 / 78    # 3.6; crossover archives are mostly tiny

COUNT_PATTERN = re.compile(
    r'<a href="/([a-z]+)/([^/"]+)/"[^>]*>(?:.*?)</a>\s*<SPAN CLASS=\'gray\'>\((\d+(?:\.\d+)?)([KM]?)\)</SPAN>'
)
CROSSOVER_ENTRY_PATTERN = re.compile(r'<a href="/crossovers/([^/"]+)/(\d+)/"')

SUFFIX_MULTIPLIERS = {"": 1, "K": 1_000, "M": 1_000_000}


def fetch(session, url: str) -> str:
    return session.get(url, timeout=30).content.decode("utf-8", errors="replace")


def humanDuration(seconds: float) -> str:
    days, remainder = divmod(seconds, 86400)
    hours = remainder / 3600
    if days:
        return f"{int(days)}d {hours:.0f}h"
    return f"{hours:.1f}h"


def main() -> int:
    session = requests.Session(impersonate="chrome")

    fandomCount = 0
    storyTotal = 0
    largestFandoms: list[tuple[int, str]] = []

    print("=== section directories ===")
    for sectionSlug in SECTION_SLUGS:
        html = fetch(session, f"https://www.fanfiction.net/{sectionSlug}/")
        sectionStories = 0
        sectionFandoms = 0
        for slugSection, slug, number, suffix in COUNT_PATTERN.findall(html):
            if slugSection != sectionSlug:
                continue
            storyCount = int(float(number) * SUFFIX_MULTIPLIERS[suffix])
            sectionStories += storyCount
            sectionFandoms += 1
            largestFandoms.append((storyCount, f"{sectionSlug}/{slug}"))
        fandomCount += sectionFandoms
        storyTotal += sectionStories
        print(f"  {sectionSlug:9} {sectionFandoms:>6,} fandoms  {sectionStories:>12,} stories")
        time.sleep(5)

    print(f"\n  TOTAL     {fandomCount:>6,} fandoms  {storyTotal:>12,} stories")

    print("\n=== crossover directories ===")
    crossoverFandoms: list[tuple[str, str]] = []
    for sectionSlug in SECTION_SLUGS:
        html = fetch(session, f"https://www.fanfiction.net/crossovers/{sectionSlug}/")
        found = sorted(set(CROSSOVER_ENTRY_PATTERN.findall(html)))
        crossoverFandoms.extend(found)
        print(f"  {sectionSlug:9} {len(found):>6,} fandoms with crossover archives")
        time.sleep(5)
    crossoverFandomCount = len(crossoverFandoms)

    # Sampling partner lists needs a UNIFORM sample. An earlier version of this
    # script sampled the largest fandoms and produced 33M total stories --
    # impossible, since FFN's highest story id is ~14.6M. The size distribution
    # is extremely skewed: Naruto has 3,929 partner links, Discworld 285.
    print("\n=== sampling partner lists (uniform) ===")
    sampleSize = 16
    stride = max(1, len(crossoverFandoms) // sampleSize)
    sample = crossoverFandoms[::stride][:sampleSize]

    canonicalPairCounts = []
    for slug, categoryId in sample:
        try:
            html = fetch(session, f"https://www.fanfiction.net/crossovers/{slug}/{categoryId}/")
        except Exception:
            continue
        # Count canonical pairs (idA < idB) the same way the crawler does, not
        # raw link matches -- each partner appears more than once on the page.
        pairs = {
            (int(a), int(b))
            for a, b in re.findall(r'-Crossovers/(\d+)/(\d+)/', html)
            if int(a) < int(b)
        }
        canonicalPairCounts.append(len(pairs))
        time.sleep(5)

    canonicalPairCounts.sort()
    medianPairs = canonicalPairCounts[len(canonicalPairCounts) // 2] if canonicalPairCounts else 0
    meanPairs = sum(canonicalPairCounts) / len(canonicalPairCounts) if canonicalPairCounts else 0
    print(f"  sampled {len(canonicalPairCounts)} fandoms: "
          f"median {medianPairs} pairs, mean {meanPairs:.0f}, max {max(canonicalPairCounts or [0])}")

    # Each pair is listed by both partners, so summing across fandoms counts
    # every pair twice.
    estimatedPairs = int(crossoverFandomCount * meanPairs / 2)

    # --- request budget ----------------------------------------------------
    # Pages are whole requests: an archive holding 3 stories still costs one
    # full request. Averaging fractional pages badly understates the crossover
    # cost, where the mean archive holds under four stories.
    fandomRequests = storyTotal / ROWS_PER_REQUEST + fandomCount * 0.5
    directoryRequests = len(SECTION_SLUGS) * 2 + crossoverFandomCount
    crossoverRequests = estimatedPairs * max(1.0, CROSSOVER_ROWS_PER_PAIR / ROWS_PER_REQUEST)

    # What the same crawl cost before short-page exhaustion: every archive paid
    # one extra request to discover an empty final page.
    naiveRequests = (storyTotal / ROWS_PER_REQUEST + fandomCount) + directoryRequests         + estimatedPairs * (max(1.0, CROSSOVER_ROWS_PER_PAIR / ROWS_PER_REQUEST) + 1)

    totalRequests = fandomRequests + directoryRequests + crossoverRequests
    totalSeconds = totalRequests * SECONDS_PER_REQUEST
    crossoverStories = int(estimatedPairs * CROSSOVER_ROWS_PER_PAIR)
    allStories = storyTotal + crossoverStories

    print("\n" + "=" * 66)
    print("BACKFILL ESTIMATE")
    print("=" * 66)
    print(f"  fandoms                     {fandomCount:>14,}")
    print(f"  stories in fandom archives  {storyTotal:>14,}")
    print(f"  fandoms w/ crossovers       {crossoverFandomCount:>14,}")
    print(f"  estimated crossover pairs   {estimatedPairs:>14,}  (upper bound)")
    print(f"  estimated crossover stories {crossoverStories:>14,}")
    print(f"  TOTAL STORIES               {allStories:>14,}")
    if allStories > HIGHEST_STORY_ID:
        print(f"  !! exceeds highest story id ({HIGHEST_STORY_ID:,}) -- estimate is too high")
    print()
    print(f"  requests: fandom archives   {fandomRequests:>14,.0f}")
    print(f"            crossovers        {crossoverRequests:>14,.0f}")
    print(f"            directories       {directoryRequests:>14,.0f}")
    print(f"            TOTAL             {totalRequests:>14,.0f}")
    print()
    print(f"  at {SECONDS_PER_REQUEST:.2f}s/request (robots.txt crawl-delay 5s):")
    print(f"            wall clock        {humanDuration(totalSeconds):>14}")
    print(f"  without short-page exhaustion: {naiveRequests:>10,.0f} req  "
          f"{humanDuration(naiveRequests * SECONDS_PER_REQUEST)}")
    print()
    tableBytes = allStories * BYTES_PER_ROW
    vectorBytes = allStories * EMBEDDING_BYTES_PER_ROW
    print(f"  storage:  table             {tableBytes / 1e9:>13.1f} GB")
    print(f"            embeddings        {vectorBytes / 1e9:>13.1f} GB")
    print(f"            TOTAL (+indexes)  {(tableBytes + vectorBytes) * 1.4 / 1e9:>13.1f} GB")
    print()
    print("  Largest fandoms (these dominate the tail):")
    for storyCount, name in sorted(largestFandoms, reverse=True)[:8]:
        pages = storyCount / ROWS_PER_REQUEST
        print(f"    {name:44} {storyCount:>9,} stories  {pages:>8,.0f} req  "
              f"{humanDuration(pages * SECONDS_PER_REQUEST):>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
