"""URL builders for the four crawlable FanFiction.Net surfaces.

Surface        Path                                       Rows  Deep paging
-------------  -----------------------------------------  ----  -----------
Just In        /j/0/0/0/                                   100  no (newest 100 only)
Fandom browse  /<section>/<Fandom>/?srt=..&p=N              25  yes (verified to p=3000)
Crossover      /<A>-and-<B>-Crossovers/<idA>/<idB>/         25  yes
Search         /search/?keywords=..&ppage=N                 50  yes

Backfill runs on fandom browse: ~12M stories at 25 rows/page is ~480K requests,
versus ~14.6M if the story-id space were enumerated directly. Incremental runs
on Just In, which surfaces every new story site-wide at 100 per request.
"""
from __future__ import annotations

import enum
import urllib.parse

from .fetcher import FFN_ORIGIN

# FFN's top-level archive sections. Each has a directory page listing its fandoms.
SECTION_SLUGS = (
    "anime", "book", "cartoon", "comic", "game", "misc", "movie", "play", "tv",
)


class BrowseSort(enum.IntEnum):
    """`srt` values, lifted from the browse filter form."""

    UPDATE_DATE = 1
    PUBLISH_DATE = 2
    REVIEWS = 3
    FAVORITES = 4
    FOLLOWS = 5


class BrowseStatus(enum.IntEnum):
    ALL = 0
    IN_PROGRESS = 1
    COMPLETE = 2


def justInUrl() -> str:
    """The global firehose: newest 100 stories site-wide, every fandom.

    Not paginated -- ?p=N returns nothing -- so this is a poll-often surface,
    not a backfill one. FFN publishes on the order of a thousand stories a day,
    so hourly polling has a wide safety margin.
    """
    return f"{FFN_ORIGIN}/j/0/0/0/"


def sectionDirectoryUrl(sectionSlug: str) -> str:
    """Directory of every fandom in a section, with abbreviated story counts."""
    if sectionSlug not in SECTION_SLUGS:
        raise ValueError(f"unknown section {sectionSlug!r}; expected one of {SECTION_SLUGS}")
    return f"{FFN_ORIGIN}/{sectionSlug}/"


def crossoverDirectoryUrl(sectionSlug: str) -> str:
    """Directory of fandoms that have crossover archives, with their category ids."""
    return f"{FFN_ORIGIN}/crossovers/{sectionSlug}/"


def fandomBrowseUrl(
    sectionSlug: str,
    fandomSlug: str,
    pageNumber: int = 1,
    sort: BrowseSort = BrowseSort.PUBLISH_DATE,
    status: BrowseStatus = BrowseStatus.ALL,
    languageId: int = 0,
    ratingId: int = 10,
) -> str:
    """One page of a fandom archive.

    Backfill sorts by PUBLISH_DATE, not UPDATE_DATE: publish order is stable, so
    a crawl that takes days will not have rows shuffling underneath it between
    page 400 and page 401. Update order is the right choice only for the
    incremental re-crawl, where churn is the point.

    `ratingId=10` is FFN's "all ratings" -- the site's own default hides M, and
    inheriting that default would silently truncate the index.
    """
    query = {
        "srt": int(sort),
        "r": ratingId,
        "p": pageNumber,
    }
    if status is not BrowseStatus.ALL:
        query["s"] = int(status)
    if languageId:
        query["lan"] = languageId
    return f"{FFN_ORIGIN}/{sectionSlug}/{fandomSlug}/?{urllib.parse.urlencode(query)}"


def crossoverArchiveUrl(
    fandomSlugA: str,
    categoryIdA: int,
    fandomSlugB: str,
    categoryIdB: int,
    pageNumber: int = 1,
    sort: BrowseSort = BrowseSort.PUBLISH_DATE,
) -> str:
    """One page of an A-x-B crossover archive.

    Crossover stories live only here -- they do not appear in either parent
    fandom's own archive, which is precisely why FFN's browse makes them so hard
    to find and why indexing them is one of the bigger wins on offer.
    """
    query = {"srt": int(sort), "r": 10, "p": pageNumber}
    path = f"{fandomSlugA}-and-{fandomSlugB}-Crossovers/{categoryIdA}/{categoryIdB}"
    return f"{FFN_ORIGIN}/{path}/?{urllib.parse.urlencode(query)}"


def searchUrl(keywords: str, pageNumber: int = 1, categoryId: int = 0) -> str:
    """FFN's own search, used only as a cross-check on crawl coverage.

    It matches title and summary text alone -- there is no full-text index
    behind it -- so it is not a viable enumeration surface. It is useful for
    spot-checking that our index has not missed stories a plain user could find.
    """
    query = {"keywords": keywords, "ready": 1, "type": "story", "ppage": pageNumber}
    if categoryId:
        query["categoryid"] = categoryId
    return f"{FFN_ORIGIN}/search/?{urllib.parse.urlencode(query)}"


def storyUrl(storyId: int, chapterNumber: int = 1) -> str:
    """A single story page.

    Only needed for the fields listings omit (chapter titles, cover art) and for
    tombstone checks -- never for bulk metadata, which listings already carry.
    """
    return f"{FFN_ORIGIN}/s/{storyId}/{chapterNumber}/"
