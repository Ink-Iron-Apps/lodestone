"""Crawl orchestration.

Three jobs, with different shapes:

* `backfillFandom`  -- walk one fandom archive to exhaustion. Resumable, because
                       a full corpus pass is days of wall-clock and *will* be
                       interrupted.
* `pollJustIn`      -- ingest the global firehose. Cheap, run hourly, catches
                       every new story site-wide.
* `refreshFandom`   -- re-crawl the recently-updated head of a fandom to pick up
                       chapter additions, review counts and completions.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Optional

from .fetcher import BlockedError, FanFictionFetcher, FetchError
from .models import Fandom
from .parser import (
    iterCrossoverDirectory,
    iterFandomDirectory,
    parseCrossoverPairs,
    parseListingPage,
)
from .storage import StoryStore
# A listing page that comes back short is the last page: FFN fills pages
# completely until it runs out. Detecting exhaustion this way, rather than by
# fetching the next (empty) page, saves one request per archive. Across ~13K
# fandoms and ~158K crossover pairs that is ~165K requests -- roughly a quarter
# of a full backfill, or eleven days of wall clock at the robots.txt crawl
# delay. See scripts/estimate_backfill.py.
FANDOM_PAGE_SIZE = 25

from .surfaces import (
    SECTION_SLUGS,
    BrowseSort,
    crossoverArchiveUrl,
    crossoverDirectoryUrl,
    fandomBrowseUrl,
    fandomCrossoverPartnersUrl,
    justInUrl,
    sectionDirectoryUrl,
)

logger = logging.getLogger(__name__)


@dataclasses.dataclass(slots=True)
class CrawlOutcome:
    surfaceKey: str
    pagesFetched: int = 0
    rowsIngested: int = 0
    isExhausted: bool = False
    stoppedReason: str = ""


def backfillFandom(
    fetcher: FanFictionFetcher,
    store: StoryStore,
    fandom: Fandom,
    maxPages: Optional[int] = None,
) -> CrawlOutcome:
    """Walk a fandom archive from where we left off until it runs out of rows.

    Sorted by publish date rather than update date: publish order is immutable,
    so pages do not reshuffle underneath a crawl that spans days. Sorting by
    update date here would cause stories to migrate between pages mid-walk and
    silently skip records.
    """
    if not (fandom.sectionSlug and fandom.fandomSlug):
        raise ValueError(f"fandom {fandom.name!r} has no archive path to crawl")

    surfaceKey = f"browse:{fandom.sectionSlug}/{fandom.fandomSlug}"
    state = store.getCrawlState(surfaceKey)
    if state and state["is_exhausted"]:
        return CrawlOutcome(surfaceKey, isExhausted=True, stoppedReason="already exhausted")

    startPage = (state["last_page"] if state else 0) + 1
    outcome = CrawlOutcome(surfaceKey)
    pageNumber = startPage

    # `recordCrawlProgress` accumulates rows, and the per-page call below already
    # contributes every row. The terminal calls exist only to persist the
    # exhausted/error flag, so they must report zero new rows or the running
    # total gets counted twice.
    NO_NEW_ROWS = 0

    while maxPages is None or outcome.pagesFetched < maxPages:
        url = fandomBrowseUrl(
            fandom.sectionSlug, fandom.fandomSlug,
            pageNumber=pageNumber, sort=BrowseSort.PUBLISH_DATE,
        )
        try:
            html = fetcher.fetch(url)
        except BlockedError:
            # Egress has lost Cloudflare's trust. Stop everything -- retrying
            # only deepens the block and there is nothing to gain by continuing.
            store.recordCrawlProgress(surfaceKey, pageNumber - 1, NO_NEW_ROWS,
                                      lastError="blocked")
            outcome.stoppedReason = "blocked"
            raise
        except FetchError as error:
            store.recordCrawlProgress(surfaceKey, pageNumber - 1, NO_NEW_ROWS,
                                      lastError=str(error))
            outcome.stoppedReason = f"fetch failed: {error}"
            return outcome

        records = parseListingPage(html, defaultFandoms=[fandom])
        outcome.pagesFetched += 1

        if not records:
            # An empty page past page 1 means the archive is exhausted. On page 1
            # it means the fandom is empty, which is equally terminal.
            outcome.isExhausted = True
            outcome.stoppedReason = "no rows"
            store.recordCrawlProgress(surfaceKey, pageNumber - 1, NO_NEW_ROWS, isExhausted=True)
            return outcome

        outcome.rowsIngested += store.upsertStories(records)

        if len(records) < FANDOM_PAGE_SIZE:
            outcome.isExhausted = True
            outcome.stoppedReason = "short page"
            store.recordCrawlProgress(surfaceKey, pageNumber, len(records), isExhausted=True)
            logger.info("%s p=%d -> %d rows (%d total, exhausted)", surfaceKey,
                        pageNumber, len(records), outcome.rowsIngested)
            return outcome

        store.recordCrawlProgress(surfaceKey, pageNumber, len(records))
        logger.info("%s p=%d -> %d rows (%d total)", surfaceKey, pageNumber,
                    len(records), outcome.rowsIngested)
        pageNumber += 1

    outcome.stoppedReason = "page budget reached"
    return outcome


def pollJustIn(fetcher: FanFictionFetcher, store: StoryStore) -> CrawlOutcome:
    """Ingest the newest 100 stories site-wide.

    This surface is not paginated, so it cannot backfill -- its whole job is to
    guarantee nothing published after the backfill started gets missed. At FFN's
    publication rate, hourly polling leaves an order of magnitude of headroom.
    """
    outcome = CrawlOutcome("justin")
    html = fetcher.fetch(justInUrl())
    records = parseListingPage(html)
    outcome.pagesFetched = 1
    outcome.rowsIngested = store.upsertStories(records)
    store.recordCrawlProgress("justin", 1, outcome.rowsIngested)
    logger.info("just-in -> %d rows", outcome.rowsIngested)
    return outcome


def refreshFandom(
    fetcher: FanFictionFetcher,
    store: StoryStore,
    fandom: Fandom,
    headPages: int = 20,
) -> CrawlOutcome:
    """Re-crawl the recently-updated head of a fandom.

    Sorted by update date, so the first N pages hold everything that has changed
    since the last pass. This is what keeps chapter counts, review counts and
    completion status from going stale -- a backfill alone produces an index
    that is correct once and wrong forever after.
    """
    surfaceKey = f"refresh:{fandom.sectionSlug}/{fandom.fandomSlug}"
    outcome = CrawlOutcome(surfaceKey)

    for pageNumber in range(1, headPages + 1):
        url = fandomBrowseUrl(
            fandom.sectionSlug, fandom.fandomSlug,
            pageNumber=pageNumber, sort=BrowseSort.UPDATE_DATE,
        )
        html = fetcher.fetch(url)
        records = parseListingPage(html, defaultFandoms=[fandom])
        outcome.pagesFetched += 1
        if not records:
            break
        outcome.rowsIngested += store.upsertStories(records)

    store.recordCrawlProgress(surfaceKey, outcome.pagesFetched, outcome.rowsIngested)
    return outcome


def discoverFandoms(fetcher: FanFictionFetcher, store: StoryStore) -> int:
    """Enumerate every fandom in every section directory.

    Run once before the first backfill, then occasionally -- FFN adds fandoms
    steadily and a fandom we have never enumerated is a fandom we will never
    crawl.
    """
    discoveredCount = 0
    for sectionSlug in SECTION_SLUGS:
        html = fetcher.fetch(sectionDirectoryUrl(sectionSlug))
        for fandom in iterFandomDirectory(html, sectionSlug):
            store.upsertFandom(fandom)
            discoveredCount += 1
        logger.info("section %s -> %d fandoms cumulative", sectionSlug, discoveredCount)
    return discoveredCount


# ---------------------------------------------------------------------------
# Crossovers
# ---------------------------------------------------------------------------


def discoverCrossoverPairs(
    fetcher: FanFictionFetcher,
    store: StoryStore,
    maxFandoms: Optional[int] = None,
) -> int:
    """Enumerate every A-x-B crossover archive FFN has.

    Two hops: each section's crossover directory gives the fandoms that have
    crossovers along with their numeric category ids, then each fandom's own
    partner page gives its pairs.

    The largest fandoms' partner pages fault with FFN's own "Error Type 1", but
    because every pair is listed on both partners' pages, those pairs are still
    recovered from the other side. A failure here is logged and skipped rather
    than aborting the sweep.
    """
    crossoverFandoms: list[Fandom] = []
    for sectionSlug in SECTION_SLUGS:
        try:
            html = fetcher.fetch(crossoverDirectoryUrl(sectionSlug))
        except FetchError as error:
            logger.warning("crossover directory %s failed: %s", sectionSlug, error)
            continue
        sectionFandoms = list(iterCrossoverDirectory(html))
        for fandom in sectionFandoms:
            fandom.sectionSlug = sectionSlug
            store.upsertFandom(fandom)
        crossoverFandoms.extend(sectionFandoms)
        logger.info("crossover directory %s -> %d fandoms", sectionSlug, len(sectionFandoms))

    if maxFandoms is not None:
        crossoverFandoms = crossoverFandoms[:maxFandoms]

    faultedFandoms = 0
    for index, fandom in enumerate(crossoverFandoms, start=1):
        try:
            html = fetcher.fetch(fandomCrossoverPartnersUrl(fandom.fandomSlug, fandom.categoryId))
        except FetchError as error:
            # Expected for the biggest fandoms; their pairs come from partners.
            faultedFandoms += 1
            logger.warning("partner list for %s faulted (recoverable): %s", fandom.name, error)
            continue

        pairs = parseCrossoverPairs(html)
        store.upsertCrossoverPairs(pairs)
        if index % 25 == 0:
            logger.info("partner lists %d/%d -> %d pairs known",
                        index, len(crossoverFandoms), store.countCrossoverPairs())

    totalPairs = store.countCrossoverPairs()
    logger.info("crossover discovery done: %d pairs, %d partner lists faulted",
                totalPairs, faultedFandoms)
    return totalPairs


def discoverPairsForFandom(
    fetcher: FanFictionFetcher,
    store: StoryStore,
    fandomSlug: str,
    categoryId: int,
) -> int:
    """Discover crossover pairs for one fandom.

    Useful on its own for retrying a fandom whose partner list faulted, and for
    extending the corpus without sweeping every section.
    """
    html = fetcher.fetch(fandomCrossoverPartnersUrl(fandomSlug, categoryId))
    pairs = parseCrossoverPairs(html)
    store.upsertCrossoverPairs(pairs)
    logger.info("%s -> %d crossover pairs", fandomSlug, len(pairs))
    return len(pairs)


def backfillCrossoverPair(
    fetcher: FanFictionFetcher,
    store: StoryStore,
    categoryIdA: int,
    categoryIdB: int,
    slugA: str,
    slugB: str,
    maxPages: Optional[int] = None,
) -> CrawlOutcome:
    """Walk one A-x-B crossover archive to exhaustion.

    Rows here carry no fandom prefix, so both parent fandoms come from the crawl
    context -- and supplying two of them is what marks the story as a crossover.
    """
    surfaceKey = f"crossover:{categoryIdA}/{categoryIdB}"
    state = store.getCrawlState(surfaceKey)
    if state and state["is_exhausted"]:
        return CrawlOutcome(surfaceKey, isExhausted=True, stoppedReason="already exhausted")

    contextFandoms = [
        Fandom(name=slugA.replace("-", " "), fandomSlug=slugA, categoryId=categoryIdA),
        Fandom(name=slugB.replace("-", " "), fandomSlug=slugB, categoryId=categoryIdB),
    ]

    outcome = CrawlOutcome(surfaceKey)
    pageNumber = (state["last_page"] if state else 0) + 1

    while maxPages is None or outcome.pagesFetched < maxPages:
        url = crossoverArchiveUrl(slugA, categoryIdA, slugB, categoryIdB,
                                  pageNumber=pageNumber, sort=BrowseSort.PUBLISH_DATE)
        try:
            html = fetcher.fetch(url)
        except BlockedError:
            store.recordCrawlProgress(surfaceKey, pageNumber - 1, 0, lastError="blocked")
            raise
        except FetchError as error:
            store.recordCrawlProgress(surfaceKey, pageNumber - 1, 0, lastError=str(error))
            outcome.stoppedReason = f"fetch failed: {error}"
            return outcome

        records = parseListingPage(html, defaultFandoms=contextFandoms)
        outcome.pagesFetched += 1

        if not records:
            outcome.isExhausted = True
            outcome.stoppedReason = "no rows"
            store.recordCrawlProgress(surfaceKey, pageNumber - 1, 0, isExhausted=True)
            return outcome

        outcome.rowsIngested += store.upsertStories(records)

        if len(records) < FANDOM_PAGE_SIZE:
            # Most crossover archives hold a handful of stories, so this is the
            # common case rather than an edge case.
            outcome.isExhausted = True
            outcome.stoppedReason = "short page"
            store.recordCrawlProgress(surfaceKey, pageNumber, len(records), isExhausted=True)
            return outcome

        store.recordCrawlProgress(surfaceKey, pageNumber, len(records))
        pageNumber += 1

    outcome.stoppedReason = "page budget reached"
    return outcome


def backfillCrossovers(
    fetcher: FanFictionFetcher,
    store: StoryStore,
    maxPairs: Optional[int] = None,
    maxPagesPerPair: Optional[int] = None,
) -> tuple[int, int]:
    """Walk every known crossover archive that is not yet exhausted."""
    pairsCrawled = 0
    rowsIngested = 0
    for pair in store.iterCrossoverPairs(limit=maxPairs):
        outcome = backfillCrossoverPair(
            fetcher, store,
            pair["fandom_id_a"], pair["fandom_id_b"],
            pair["slug_a"], pair["slug_b"],
            maxPages=maxPagesPerPair,
        )
        pairsCrawled += 1
        rowsIngested += outcome.rowsIngested
        logger.info("%s -> %d rows (%s)", outcome.surfaceKey,
                    outcome.rowsIngested, outcome.stoppedReason)
    return pairsCrawled, rowsIngested
