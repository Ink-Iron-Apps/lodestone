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

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "crawler"))

from lodestone_crawler.fetcher import FanFictionFetcher
from lodestone_crawler.surfaces import crossoverDirectoryUrl, sectionDirectoryUrl

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


class DirectoryParseError(RuntimeError):
    """A directory fetched but parsed to nothing, which is never a real answer."""


def fetch(session, url: str) -> str:
    """Fetch through the crawler's own fetcher.

    This used to hold its own `requests.Session(impersonate="chrome")` with no
    headers set. Cloudflare answered 403 "Just a moment..." to every request --
    a Chrome TLS fingerprint arriving without Chrome's Accept and
    Accept-Language is a mismatch, and the mismatch is the tell. The requests
    still returned 200-shaped bodies to this script, which parsed them to zero
    and printed a confident estimate of nothing.

    FanFictionFetcher sets those headers, enforces the robots.txt crawl-delay
    itself, and raises BlockedError on a 403 instead of handing back a challenge
    page. Sharing it also means there is one place where politeness lives.
    """
    return session.fetch(url)


def requireMatches(count: int, what: str, url: str) -> None:
    """Fail loudly on a zero, rather than reporting it as a measurement.

    An estimate of zero is always a broken fetch or a stale pattern -- FFN has
    never had an empty section directory. Reporting it as data is how this
    script wasted a run; 56d5134 fixed the same silent zero in probe_egress.py.
    """
    if count == 0:
        raise DirectoryParseError(
            f"{what} parsed to 0 entries from {url} -- fetch or pattern is broken, "
            f"not an empty directory"
        )


def humanDuration(seconds: float) -> str:
    days, remainder = divmod(seconds, 86400)
    hours = remainder / 3600
    if days:
        return f"{int(days)}d {hours:.0f}h"
    return f"{hours:.1f}h"


def main() -> int:
    session = FanFictionFetcher()

    fandomCount = 0
    storyTotal = 0
    largestFandoms: list[tuple[int, str]] = []

    print("=== section directories ===")
    for sectionSlug in SECTION_SLUGS:
        url = sectionDirectoryUrl(sectionSlug)
        html = fetch(session, url)
        requireMatches(len(COUNT_PATTERN.findall(html)), f"section {sectionSlug}", url)
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

    print(f"\n  TOTAL     {fandomCount:>6,} fandoms  {storyTotal:>12,} stories")

    print("\n=== crossover directories ===")
    crossoverFandoms: list[tuple[str, str]] = []
    for sectionSlug in SECTION_SLUGS:
        url = crossoverDirectoryUrl(sectionSlug)
        html = fetch(session, url)
        requireMatches(len(CROSSOVER_ENTRY_PATTERN.findall(html)), f"crossovers {sectionSlug}", url)
        found = sorted(set(CROSSOVER_ENTRY_PATTERN.findall(html)))
        crossoverFandoms.extend(found)
        print(f"  {sectionSlug:9} {len(found):>6,} fandoms with crossover archives")
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

    # naiveRequests is the real cost now, not the pessimistic alternative. The
    # short-page shortcut it excludes was removed from the crawler: FFN serves
    # 24-row pages mid-archive when a story has been deleted, so stopping on one
    # marked 1,873 fandoms complete early and lost 2.5M stories. Confirming the
    # end of an archive costs one extra request per surface, and that is the
    # behaviour being budgeted for.
    totalRequests = naiveRequests
    shortcutRequests = fandomRequests + directoryRequests + crossoverRequests
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
    print(f"  requests: TOTAL             {totalRequests:>14,.0f}")
    print(f"            (archives {fandomRequests:,.0f} + crossovers "
          f"{crossoverRequests:,.0f} + directories {directoryRequests:,.0f},")
    print(f"             plus one empty page per surface to prove exhaustion)")
    print()
    print(f"  at {SECONDS_PER_REQUEST:.2f}s/request (robots.txt crawl-delay 5s):")
    print(f"            wall clock        {humanDuration(totalSeconds):>14}")
    print(f"  what the removed short-page shortcut would have cost: "
          f"{shortcutRequests:,.0f} req  "
          f"{humanDuration(shortcutRequests * SECONDS_PER_REQUEST)}")
    print(f"  -- cheaper, and wrong: it lost 2.5M stories. See pipeline.py.")
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
