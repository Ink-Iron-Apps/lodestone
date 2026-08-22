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
from .parser import iterFandomDirectory, parseListingPage
from .storage import StoryStore
from .surfaces import (
    SECTION_SLUGS,
    BrowseSort,
    fandomBrowseUrl,
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
