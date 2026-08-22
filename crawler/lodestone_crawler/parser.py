"""Parsers for FanFiction.Net's HTML.

FFN exposes no API, no JSON and no microdata. Every fact about a story lives in
one pipe-delimited human-readable string inside `div.z-padtop2.xgray`, and the
grammar of that string is positional, optional-heavy and inconsistent between
surfaces. This module is the whole ballgame -- everything downstream is
ordinary engineering.

Grammar observed across all four surfaces (2026-08):

  listing row  [<Fandom> - ] Rated: <r> - <lang> [- <genres>] - Chapters: N
               - Words: N [- Reviews: N] [- Favs: N] [- Follows: N]
               [- Updated: <ts>] - Published: <ts> [- <characters>] [- Complete]

  story page   Rated: Fiction <r> - <lang> [- <genres>] [- <characters>]
               - Chapters: N - Words: N ... - Published: <ts> [- Status: Complete]
               - id: N

Note the character list moves: listings put it after Published, story pages put
it before Chapters. Both orders are handled by classifying tokens rather than
reading them positionally.
"""
from __future__ import annotations

import datetime
import html as htmllib
import re
from typing import Iterator, Optional

from .models import Fandom, StoryRecord, StoryStatus

# FFN's fixed genre vocabulary. "Hurt/Comfort" contains a slash, which is why a
# naive split("/") on the genre token mangles "Hurt/Comfort/Adventure".
FFN_GENRES = frozenset({
    "General", "Romance", "Humor", "Drama", "Poetry", "Adventure", "Mystery",
    "Horror", "Parody", "Angst", "Supernatural", "Suspense", "Sci-Fi",
    "Fantasy", "Spiritual", "Tragedy", "Western", "Crime", "Family",
    "Hurt/Comfort", "Friendship",
})

LISTING_ROW_PATTERN = re.compile(r"<div\s+class='z-list[^']*'.*?</div>\s*</div>\s*</div>", re.S)
STORY_LINK_PATTERN = re.compile(r'<a\s+class=stitle\s+href="/s/(\d+)/1/[^"]*">(.*?)</a>', re.S)
AUTHOR_LINK_PATTERN = re.compile(r'<a\s+href="/u/(\d+)/([^"]*)">')
COVER_IMAGE_PATTERN = re.compile(r"data-original='/image/(\d+)/")
SUMMARY_PATTERN = re.compile(r"<div class='z-indent z-padtop'>(.*?)<div class='z-padtop2 xgray'>", re.S)
META_BLOCK_PATTERN = re.compile(r"<div class='z-padtop2 xgray'>(.*?)</div>", re.S)
XUTIME_PATTERN = re.compile(r"<span[^>]*data-xutime='(\d+)'[^>]*>.*?</span>", re.S)
TAG_PATTERN = re.compile(r"<[^>]+>")

COUNT_FIELDS = {
    "Chapters": "chapterCount",
    "Words": "wordCount",
    "Reviews": "reviewCount",
    "Favs": "favoriteCount",
    "Follows": "followCount",
}


def stripTags(fragment: str) -> str:
    """Drop markup and normalize whitespace.

    Search-result rows bold the matched terms inside the title and summary, so
    tag-stripping is not optional even for fields that look like plain text.
    """
    return re.sub(r"\s+", " ", htmllib.unescape(TAG_PATTERN.sub("", fragment))).strip()


def parseGenres(token: str) -> Optional[list[str]]:
    """Split a genre token, respecting the slash inside 'Hurt/Comfort'.

    Returns None when the token is not a genre token at all -- that is how the
    caller distinguishes 'Hurt/Comfort/Adventure' from a character list.
    """
    if token in FFN_GENRES:
        return [token]

    parts = token.split("/")
    # FFN allows at most two genres, so try every way of cutting the token into
    # two halves and accept the first where both halves are real genres.
    for splitIndex in range(1, len(parts)):
        first = "/".join(parts[:splitIndex])
        second = "/".join(parts[splitIndex:])
        if first in FFN_GENRES and second in FFN_GENRES:
            return [first, second]
    return None


def parseCharacters(token: str) -> tuple[list[str], list[list[str]]]:
    """Split a character token into (all characters, ships).

    '[Draco M., Percy J.] Anthony G., OC' means Draco/Percy are a couple and the
    other two merely appear. Multiple bracket groups mean multiple ships.
    """
    ships: list[list[str]] = []
    for shipBody in re.findall(r"\[([^\]]*)\]", token):
        members = [name.strip() for name in shipBody.split(",") if name.strip()]
        if members:
            ships.append(members)

    remainder = re.sub(r"\[[^\]]*\]", " ", token)
    looseCharacters = [name.strip() for name in remainder.split(",") if name.strip()]

    allCharacters: list[str] = []
    for name in [member for ship in ships for member in ship] + looseCharacters:
        if name not in allCharacters:
            allCharacters.append(name)
    return allCharacters, ships


def toUtc(unixTimestamp: str) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(int(unixTimestamp), tz=datetime.UTC)


def parseMetaLine(metaBlockHtml: str, record: StoryRecord) -> None:
    """Populate `record` from one `div.z-padtop2.xgray` block.

    Timestamps are read from the `data-xutime` attributes rather than the
    rendered date, because the rendered form is lossy ('10h', '8/15/2012') while
    the attribute is always an exact unix time.
    """
    timestamps = XUTIME_PATTERN.findall(metaBlockHtml)
    dateLabels = re.findall(r"(Updated|Published):\s*<span", metaBlockHtml)
    for label, timestamp in zip(dateLabels, timestamps):
        if label == "Updated":
            record.updatedAt = toUtc(timestamp)
        else:
            record.publishedAt = toUtc(timestamp)
    if record.updatedAt is None:
        record.updatedAt = record.publishedAt

    flattened = stripTags(XUTIME_PATTERN.sub("", metaBlockHtml))

    # Everything before "Rated:" is the fandom. It cannot be tokenized on " - "
    # because fandom names contain that separator (e.g. "Hetalia - Axis Powers").
    ratedIndex = flattened.find("Rated:")
    if ratedIndex > 0:
        fandomText = flattened[:ratedIndex].rstrip(" -").strip()
        if fandomText:
            record.isCrossover = "Crossover" in fandomText
            cleaned = re.sub(r"\s*Crossover$", "", fandomText)
            names = [part.strip() for part in re.split(r"\s+[+&]\s+", cleaned) if part.strip()]
            record.fandoms = [Fandom(name=name) for name in names]
        flattened = flattened[ratedIndex:]

    for rawToken in flattened.split(" - "):
        token = rawToken.strip().rstrip(" -")
        if not token:
            continue

        if token.startswith("Rated:"):
            # Story pages say "Rated: Fiction T"; listings say "Rated: T".
            record.rating = token.removeprefix("Rated:").replace("Fiction", "").strip()
            continue

        countMatch = re.fullmatch(r"(Chapters|Words|Reviews|Favs|Follows):\s*([\d,]+)", token)
        if countMatch:
            setattr(record, COUNT_FIELDS[countMatch.group(1)], int(countMatch.group(2).replace(",", "")))
            continue

        if token in ("Complete", "Status: Complete"):
            record.status = StoryStatus.COMPLETE
            continue

        if re.fullmatch(r"id:\s*\d+", token) or token.startswith(("Updated:", "Published:")):
            continue  # id is already known from the URL; dates came from xutime

        if record.language is None:
            # Positionally guaranteed: language is always the token straight
            # after the rating, and the vocabulary is too large to whitelist.
            record.language = token
            continue

        if not record.genres:
            parsedGenres = parseGenres(token)
            if parsedGenres:
                record.genres = parsedGenres
                continue

        if not record.characters:
            record.characters, record.ships = parseCharacters(token)


def parseListingRow(rowHtml: str, defaultFandoms: Optional[list[Fandom]] = None) -> Optional[StoryRecord]:
    """Parse one `div.z-list` row into a full StoryRecord.

    Listing rows carry the identical metadata line as the story page, which is
    what makes a full-corpus crawl affordable: 25-100 complete records per
    request instead of one.
    """
    storyMatch = STORY_LINK_PATTERN.search(rowHtml)
    authorMatch = AUTHOR_LINK_PATTERN.search(rowHtml)
    metaMatch = META_BLOCK_PATTERN.search(rowHtml)
    if not (storyMatch and authorMatch and metaMatch):
        return None

    summaryMatch = SUMMARY_PATTERN.search(rowHtml)
    coverMatch = COVER_IMAGE_PATTERN.search(rowHtml)

    record = StoryRecord(
        storyId=int(storyMatch.group(1)),
        title=stripTags(storyMatch.group(2)),
        authorId=int(authorMatch.group(1)),
        authorName=htmllib.unescape(authorMatch.group(2)),
        summary=stripTags(summaryMatch.group(1)) if summaryMatch else "",
        coverImageId=int(coverMatch.group(1)) if coverMatch else None,
    )
    parseMetaLine(metaMatch.group(1), record)

    # Fandom-scoped archives omit the fandom from the row (it is implied by the
    # URL), so the caller supplies it from the crawl context.
    if not record.fandoms and defaultFandoms:
        record.fandoms = list(defaultFandoms)
        record.isCrossover = len(defaultFandoms) > 1
    return record


def parseListingPage(html: str, defaultFandoms: Optional[list[Fandom]] = None) -> list[StoryRecord]:
    records = []
    for rowMatch in LISTING_ROW_PATTERN.finditer(html):
        record = parseListingRow(rowMatch.group(0), defaultFandoms)
        if record is not None:
            records.append(record)
    return records


def parsePageCount(html: str) -> Optional[int]:
    """Highest page number reachable from the pager, for crawl planning."""
    pageNumbers = [int(n.replace(",", "")) for n in re.findall(r"[?&]p=(\d[\d,]*)", html)]
    return max(pageNumbers) if pageNumbers else None


def iterFandomDirectory(html: str, sectionSlug: str) -> Iterator[Fandom]:
    """Yield every fandom listed in a section directory such as /book/.

    The story counts beside each entry are abbreviated ('852K'), so they are
    useful for prioritizing the crawl but not as ground truth.
    """
    pattern = re.compile(r'<a href="/' + re.escape(sectionSlug) + r'/([^/"]+)/"[^>]*>(.*?)</a>')
    for match in pattern.finditer(html):
        yield Fandom(name=stripTags(match.group(2)), sectionSlug=sectionSlug, fandomSlug=match.group(1))


# ---------------------------------------------------------------------------
# Crossovers
# ---------------------------------------------------------------------------

CROSSOVER_ENTRY_PATTERN = re.compile(
    r'<a href="/crossovers/([^/"]+)/(\d+)/"[^>]*>(.*?)</a>'
)
CROSSOVER_PAIR_PATTERN = re.compile(
    r'<a href="/([A-Za-z0-9._-]+)-and-([A-Za-z0-9._-]+)-Crossovers/(\d+)/(\d+)/"'
)


def iterCrossoverDirectory(html: str) -> Iterator[Fandom]:
    """Yield fandoms that have a crossover archive, from /crossovers/<section>/.

    This is the only place FFN exposes a fandom's numeric category id, which the
    crossover archive URLs are built from.
    """
    for match in CROSSOVER_ENTRY_PATTERN.finditer(html):
        yield Fandom(
            name=stripTags(match.group(3)),
            fandomSlug=match.group(1),
            categoryId=int(match.group(2)),
        )


def parseCrossoverPairs(html: str) -> list[tuple[int, int, str, str]]:
    """Extract (idA, idB, slugA, slugB) pairs from a fandom's partner list.

    FFN orders the pair URL by ascending category id regardless of which
    fandom's page you are on, so the tuple is already canonical and the same
    pair discovered from either side deduplicates naturally.

    That symmetry matters: the partner list for the very largest fandoms
    (Harry Potter among them) faults server-side with FFN's own "Error Type 1",
    but since every pair is listed on both partners' pages, nothing is lost as
    long as the other side is crawled.
    """
    pairs = []
    for match in CROSSOVER_PAIR_PATTERN.finditer(html):
        slugA, slugB, idA, idB = match.group(1), match.group(2), int(match.group(3)), int(match.group(4))
        if idA < idB:
            pairs.append((idA, idB, slugA, slugB))
    return pairs
