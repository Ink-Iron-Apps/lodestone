"""Parser tests.

Two layers:

1. Grammar tests over hand-written snippets -- these pin the awkward cases
   (slash genres, ship brackets, fandoms containing " - ", missing fields) and
   never change.
2. Invariant tests over real snapshotted pages -- these catch FFN changing its
   markup. Re-run `scripts/save_fixtures.py` to refresh them; the assertions
   are deliberately shape-based so a fresh snapshot does not break them.
"""
from __future__ import annotations

import datetime
import pathlib

import pytest

from lodestone_crawler.models import Fandom, StoryStatus
from lodestone_crawler.surfaces import crossoverArchiveUrl
from lodestone_crawler.parser import (
    FFN_GENRES,
    iterCrossoverDirectory,
    iterFandomDirectory,
    parseCrossoverPairs,
    parseCharacters,
    parseGenres,
    parseListingPage,
    parseListingRow,
)

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"

LISTING_FIXTURES = [
    "just_in.html",
    "browse_fandom.html",
    "browse_deep.html",
    "browse_small_fandom.html",
    "search_results.html",
]


def loadFixture(name: str) -> str:
    """Load a snapshotted page, skipping the test if it is not present.

    Fixtures are verbatim FanFiction.Net HTML, so they are gitignored rather
    than committed to a public repo. Run `python scripts/save_fixtures.py` to
    generate them locally; without them the grammar tests still run and the
    snapshot invariants skip.
    """
    path = FIXTURE_DIR / name
    if not path.exists():
        pytest.skip(f"missing fixture {name}; run scripts/save_fixtures.py")
    return path.read_text(encoding="utf-8")


def buildRow(metaLine: str, summary: str = "A summary.") -> str:
    """Wrap a metadata line in the surrounding row markup FFN emits."""
    return (
        "<div class='z-list zhover zpointer ' style='min-height:77px;'>"
        '<a  class=stitle href="/s/12345678/1/Some-Story">'
        "<img class='lazy cimage ' data-original='/image/999/75/' width=50 height=66>Some Story</a>"
        '  by <a href="/u/87654321/SomeAuthor">SomeAuthor</a> '
        "<div class='z-indent z-padtop'>" + summary +
        "<div class='z-padtop2 xgray'>" + metaLine + "</div></div></div>"
    )


# --------------------------------------------------------------------------
# 1. Grammar
# --------------------------------------------------------------------------

@pytest.mark.parametrize("token,expected", [
    ("Romance", ["Romance"]),
    ("Hurt/Comfort", ["Hurt/Comfort"]),
    ("Mystery/Romance", ["Mystery", "Romance"]),
    ("Hurt/Comfort/Adventure", ["Hurt/Comfort", "Adventure"]),
    ("Adventure/Hurt/Comfort", ["Adventure", "Hurt/Comfort"]),
    ("Sci-Fi/Fantasy", ["Sci-Fi", "Fantasy"]),
    ("Harry P., Ginny W.", None),
    ("[Draco M., Percy J.] Anthony G.", None),
])
def testParseGenres(token, expected):
    assert parseGenres(token) == expected


def testParseCharactersSplitsShipsFromBystanders():
    characters, ships = parseCharacters("[Draco M., Percy J.] Anthony G., OC")
    assert ships == [["Draco M.", "Percy J."]]
    assert characters == ["Draco M.", "Percy J.", "Anthony G.", "OC"]


def testParseCharactersHandlesMultipleShips():
    characters, ships = parseCharacters("[Harry P., Ginny W.] [Ron W., Hermione G.] Neville L.")
    assert ships == [["Harry P., Ginny W."][0].split(", "), ["Ron W.", "Hermione G."]]
    assert characters[-1] == "Neville L."


def testParseCharactersWithNoShips():
    characters, ships = parseCharacters("Sebastian M., Ciel P.")
    assert ships == []
    assert characters == ["Sebastian M.", "Ciel P."]


def testFullListingRowWithEveryField():
    row = buildRow(
        "Rated: T - English - Hurt/Comfort/Adventure - Chapters: 50 - Words: 449,697"
        " - Reviews: 496 - Favs: 727 - Follows: 894"
        " - Updated: <span data-xutime='1787288906'>2h</span>"
        " - Published: <span data-xutime='1717853402'>6/8/2024</span>"
        " - [Harry P., Artemis] Ron W. - Complete"
    )
    record = parseListingRow(row)

    assert record.storyId == 12345678
    assert record.title == "Some Story"
    assert record.authorId == 87654321
    assert record.authorName == "SomeAuthor"
    assert record.rating == "T"
    assert record.language == "English"
    assert record.genres == ["Hurt/Comfort", "Adventure"]
    assert record.chapterCount == 50
    assert record.wordCount == 449697
    assert record.reviewCount == 496
    assert record.favoriteCount == 727
    assert record.followCount == 894
    assert record.ships == [["Harry P.", "Artemis"]]
    assert record.characters == ["Harry P.", "Artemis", "Ron W."]
    assert record.status is StoryStatus.COMPLETE
    assert record.coverImageId == 999
    assert record.publishedAt == datetime.datetime.fromtimestamp(1717853402, tz=datetime.UTC)
    assert record.updatedAt == datetime.datetime.fromtimestamp(1787288906, tz=datetime.UTC)


def testMinimalRowOmitsOptionalFields():
    """A brand-new oneshot has no genre, no characters, no engagement counts and
    no update date -- every one of those is optional in the grammar."""
    row = buildRow(
        "Rated: M - English - Chapters: 1 - Words: 1,814"
        " - Published: <span data-xutime='1787322773'>4m</span>"
    )
    record = parseListingRow(row)

    assert record.genres == []
    assert record.characters == []
    assert record.reviewCount == 0
    assert record.status is StoryStatus.IN_PROGRESS
    # No "Updated:" means the story has never been updated; fall back to published
    # so sorting by recency does not drop these rows.
    assert record.updatedAt == record.publishedAt


def testFandomPrefixWithSeparatorInItsName():
    """'Hetalia - Axis Powers' contains the token separator. Splitting the meta
    line on ' - ' before extracting the fandom would shred it."""
    row = buildRow(
        "Hetalia - Axis Powers - Rated: K+ - English - Humor - Chapters: 1 - Words: 500"
        " - Published: <span data-xutime='1787320399'>1h</span>"
    )
    record = parseListingRow(row)

    assert [fandom.name for fandom in record.fandoms] == ["Hetalia - Axis Powers"]
    assert record.isCrossover is False
    assert record.genres == ["Humor"]


def testCrossoverPrefixYieldsBothFandoms():
    row = buildRow(
        "Harry Potter + Percy Jackson and the Olympians Crossover - Rated: T - English"
        " - Adventure - Chapters: 3 - Words: 9,000"
        " - Published: <span data-xutime='1787320399'>1h</span>"
    )
    record = parseListingRow(row)

    assert record.isCrossover is True
    assert [fandom.name for fandom in record.fandoms] == [
        "Harry Potter", "Percy Jackson and the Olympians",
    ]


def testFandomScopedArchiveInheritsFandomFromContext():
    """Rows inside /book/Harry-Potter/ omit the fandom, so the crawl context
    supplies it."""
    row = buildRow(
        "Rated: T - English - Drama - Chapters: 2 - Words: 4,000"
        " - Published: <span data-xutime='1787320399'>1h</span>"
    )
    record = parseListingRow(row, defaultFandoms=[Fandom(name="Harry Potter", sectionSlug="book")])

    assert [fandom.name for fandom in record.fandoms] == ["Harry Potter"]
    assert record.isCrossover is False


def testSearchResultBoldTagsAreStripped():
    row = (
        "<div class='z-list ' style='min-height:77px;'>"
        '<a  class=stitle href="/s/8432465/1/Time-Traveler"><b>Time</b> <b>Traveler</b></a>'
        '  by <a href="/u/3254115/missrynne">missrynne</a> '
        "<div class='z-indent z-padtop'>Sebastian is a <b>time</b> <b>traveler</b>."
        "<div class='z-padtop2 xgray'>Black Butler - Rated: T - English - Chapters: 1"
        " - Words: 772 - Published: <span data-xutime='1345061780'>8/15/2012</span>"
        " - Sebastian M., Ciel P. - Complete</div></div></div>"
    )
    record = parseListingRow(row)

    assert record.title == "Time Traveler"
    assert record.summary == "Sebastian is a time traveler."
    assert record.genres == []
    assert record.characters == ["Sebastian M.", "Ciel P."]


def testDerivedQualitySignals():
    row = buildRow(
        "Rated: T - English - Drama - Chapters: 1 - Words: 2,000 - Favs: 300"
        " - Updated: <span data-xutime='1500000000'>2017</span>"
        " - Published: <span data-xutime='1400000000'>2014</span>"
    )
    record = parseListingRow(row)

    assert record.favoritesPerThousandWords == 150.0
    assert record.isProbablyAbandoned is True


def testCompleteStoriesAreNeverFlaggedAbandoned():
    row = buildRow(
        "Rated: T - English - Drama - Chapters: 1 - Words: 2,000"
        " - Updated: <span data-xutime='1400000000'>2014</span>"
        " - Published: <span data-xutime='1400000000'>2014</span> - Complete"
    )
    assert parseListingRow(row).isProbablyAbandoned is False


# --------------------------------------------------------------------------
# 2. Invariants over real snapshots
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fixtureName", LISTING_FIXTURES)
def testFixtureYieldsExpectedRowCount(fixtureName):
    records = parseListingPage(loadFixture(fixtureName))
    expectedRowCount = {"just_in.html": 100, "search_results.html": 50}.get(fixtureName, 25)
    assert len(records) == expectedRowCount


@pytest.mark.parametrize("fixtureName", LISTING_FIXTURES)
def testEveryRealRowHasCoreFields(fixtureName):
    for record in parseListingPage(loadFixture(fixtureName)):
        assert record.storyId > 0
        assert record.title
        assert record.authorName
        assert record.authorId > 0
        assert record.rating
        assert record.language
        assert record.chapterCount and record.chapterCount >= 1
        assert record.wordCount is not None
        assert record.publishedAt is not None
        assert record.updatedAt is not None
        assert record.updatedAt >= record.publishedAt


@pytest.mark.parametrize("fixtureName", LISTING_FIXTURES)
def testParsedGenresAreAlwaysRealGenres(fixtureName):
    """The single most likely silent-corruption bug: a character list or a
    fandom name getting classified as a genre."""
    for record in parseListingPage(loadFixture(fixtureName)):
        assert len(record.genres) <= 2
        for genre in record.genres:
            assert genre in FFN_GENRES, f"story {record.storyId} got bogus genre {genre!r}"


@pytest.mark.parametrize("fixtureName", LISTING_FIXTURES)
def testCharactersNeverLeakMarkupOrCounts(fixtureName):
    for record in parseListingPage(loadFixture(fixtureName)):
        for name in record.characters:
            assert "<" not in name and ">" not in name
            assert not name.startswith(("Chapters:", "Words:", "Reviews:", "Favs:", "Follows:"))
            assert name != "Complete"


def testJustInRowsCarryTheirOwnFandom():
    """The global firehose is the one surface where every row names its fandom."""
    records = parseListingPage(loadFixture("just_in.html"))
    assert all(record.fandoms for record in records)


def testFandomDirectoryEnumeration():
    fandoms = list(iterFandomDirectory(loadFixture("section_directory.html"), "book"))
    names = {fandom.name for fandom in fandoms}

    assert len(fandoms) > 2000
    assert "Harry Potter" in names
    assert all(fandom.sectionSlug == "book" for fandom in fandoms)
    assert all(fandom.fandomSlug for fandom in fandoms)


# --------------------------------------------------------------------------
# 3. Crossovers
# --------------------------------------------------------------------------

def testCrossoverDirectoryYieldsCategoryIds():
    """The crossover directory is the only place FFN exposes a fandom's numeric
    category id, which every crossover archive URL is built from."""
    html = (
        '<div id="list_output">'
        '<div><a href="/crossovers/Harry-Potter/224/" title="Harry Potter">Harry Potter</a></div>'
        '<div><a href="/crossovers/Naruto/1402/" title="Naruto">Naruto</a></div>'
        "</div>"
    )
    fandoms = list(iterCrossoverDirectory(html))

    assert [(f.name, f.fandomSlug, f.categoryId) for f in fandoms] == [
        ("Harry Potter", "Harry-Potter", 224),
        ("Naruto", "Naruto", 1402),
    ]


def testCrossoverPairsAreCanonicallyOrdered():
    """FFN builds the pair URL by ascending category id regardless of which
    partner's page you came from, so the parsed tuple is already canonical."""
    html = (
        '<a href="/Harry-Potter-and-Naruto-Crossovers/224/1402/">Harry Potter</a>'
        '<a href="/Naruto-and-Bleach-Crossovers/1402/1758/">Bleach</a>'
    )
    pairs = parseCrossoverPairs(html)

    assert pairs == [
        (224, 1402, "Harry-Potter", "Naruto"),
        (1402, 1758, "Naruto", "Bleach"),
    ]
    for idA, idB, _, _ in pairs:
        assert idA < idB


def testSamePairFromBothSidesDeduplicates():
    """Every pair is listed on both partners' pages. Because both sides yield
    the identical canonical tuple, the largest fandoms' partner pages faulting
    server-side costs no coverage."""
    fromNaruto = parseCrossoverPairs('<a href="/Harry-Potter-and-Naruto-Crossovers/224/1402/">HP</a>')
    fromHarryPotter = parseCrossoverPairs('<a href="/Harry-Potter-and-Naruto-Crossovers/224/1402/">Naruto</a>')
    assert fromNaruto == fromHarryPotter
    assert len(set(fromNaruto) | set(fromHarryPotter)) == 1


def testCrossoverArchiveUrlNormalizesPairOrder():
    """Passing the partners in the wrong order must still produce the URL FFN
    actually serves -- the reversed form 404s."""
    forward = crossoverArchiveUrl("Harry-Potter", 224, "Naruto", 1402)
    reversed_ = crossoverArchiveUrl("Naruto", 1402, "Harry-Potter", 224)

    assert "/Harry-Potter-and-Naruto-Crossovers/224/1402/" in forward
    assert forward == reversed_


def testCrossoverRowsTakeBothFandomsFromContext():
    """Crossover archive rows carry no fandom prefix, so both parents come from
    the crawl context -- and having two of them is what marks the crossover."""
    row = buildRow(
        "Rated: T - English - Adventure - Chapters: 5 - Words: 12,000"
        " - Published: <span data-xutime='1717853402'>6/8/2024</span>"
    )
    record = parseListingRow(row, defaultFandoms=[
        Fandom(name="Harry Potter", fandomSlug="Harry-Potter", categoryId=224),
        Fandom(name="Naruto", fandomSlug="Naruto", categoryId=1402),
    ])

    assert record.isCrossover is True
    assert [f.name for f in record.fandoms] == ["Harry Potter", "Naruto"]
