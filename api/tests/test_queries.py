"""Query-builder tests.

These need no database: they assert on the SQL fragments and bound parameters,
which is where the filter logic actually lives. The point is to pin the
distinctions that are easy to get subtly wrong -- include vs exclude, "overlaps"
vs "contains", and paired-with vs merely-present.
"""
from __future__ import annotations

import pytest

from lodestone_api.queries import (
    SearchFilters,
    SortOrder,
    buildSearchQuery,
    buildWhereClause,
)


def testBaselineExcludesTombstonedStories():
    whereBody, parameters = buildWhereClause(SearchFilters())
    assert whereBody == "s.deleted_at IS NULL"
    assert parameters == {}


def testGenreIncludeUsesOverlap():
    """Asking for Humor or Parody must return either, not only both."""
    whereBody, parameters = buildWhereClause(SearchFilters(genres=["Humor", "Parody"]))
    assert "s.genres && %(genres)s" in whereBody
    assert "NOT (s.genres" not in whereBody
    assert parameters["genres"] == ["Humor", "Parody"]


def testGenreExcludeIsNegated():
    whereBody, parameters = buildWhereClause(SearchFilters(excludedGenres=["Romance"]))
    assert "NOT (s.genres && %(excludedGenres)s)" in whereBody
    assert parameters["excludedGenres"] == ["Romance"]


def testIncludeAndExcludeCoexist():
    """The headline query FFN cannot express: this genre but not that one."""
    whereBody, _ = buildWhereClause(
        SearchFilters(genres=["Humor"], excludedGenres=["Romance"])
    )
    assert "s.genres && %(genres)s" in whereBody
    assert "NOT (s.genres && %(excludedGenres)s)" in whereBody


def testCharacterExclusion():
    whereBody, parameters = buildWhereClause(
        SearchFilters(characters=["Aziraphale"], excludedCharacters=["OC"])
    )
    assert "s.characters && %(characters)s" in whereBody
    assert "NOT (s.characters && %(excludedCharacters)s)" in whereBody
    assert parameters["excludedCharacters"] == ["OC"]


def testShipRequiresMembersInOneBracketGroup():
    """A ship is not 'both characters appear' -- they must share a bracket
    group. This is the distinction FFN's characterid filters cannot draw."""
    whereBody, parameters = buildWhereClause(
        SearchFilters(ship=["A. Crowley", "Aziraphale"])
    )
    assert "jsonb_array_elements(s.ships)" in whereBody
    assert parameters["ship"] == ["A. Crowley", "Aziraphale"]
    assert parameters["shipSize"] == 2


def testShipSizeCountsDistinctMembers():
    """A duplicated member must not inflate the required match count, or the
    query silently returns nothing."""
    _, parameters = buildWhereClause(SearchFilters(ship=["Aziraphale", "Aziraphale"]))
    assert parameters["shipSize"] == 1


def testAbandonedIsDerivedNotStored():
    whereBody, _ = buildWhereClause(SearchFilters(onlyAbandoned=True))
    assert "s.status = 'in_progress'" in whereBody
    assert "interval '2 years'" in whereBody

    excludeBody, _ = buildWhereClause(SearchFilters(excludeAbandoned=True))
    assert excludeBody.count("NOT (s.status = 'in_progress'") == 1


def testCompleteStoriesAreNeverAbandoned():
    """Both flags together is contradictory but must not produce broken SQL."""
    whereBody, _ = buildWhereClause(
        SearchFilters(excludeAbandoned=True, onlyAbandoned=True)
    )
    assert "NOT (" in whereBody  # unsatisfiable, but valid


def testNumericBoundsAreBoundNotInterpolated():
    _, parameters = buildWhereClause(
        SearchFilters(minWords=1000, maxWords=50000, minFavorites=10, minChapters=2)
    )
    assert parameters["minWords"] == 1000
    assert parameters["maxWords"] == 50000
    assert parameters["minFavorites"] == 10
    assert parameters["minChapters"] == 2


def testZeroBoundsSurviveFalsinessChecks():
    """minWords=0 is a real filter and must not be dropped as falsy."""
    whereBody, parameters = buildWhereClause(SearchFilters(minWords=0, minFavorites=0))
    assert "s.word_count >= %(minWords)s" in whereBody
    assert parameters["minWords"] == 0
    assert parameters["minFavorites"] == 0


def testInjectionAttemptStaysAParameter():
    hostileInput = "'; DROP TABLE stories; --"
    sql, parameters = buildSearchQuery(SearchFilters(query=hostileInput))
    assert hostileInput not in sql
    assert parameters["query"] == hostileInput


def testRelevanceSortFallsBackWithoutAQuery():
    """Ranking by relevance with nothing to rank against would order rows
    arbitrarily, so an empty query falls back to recency."""
    sql, _ = buildSearchQuery(SearchFilters(sort=SortOrder.RELEVANCE))
    assert "ts_rank" not in sql
    assert "s.updated_at DESC" in sql


def testRelevanceSortAppliesWithAQuery():
    sql, _ = buildSearchQuery(SearchFilters(sort=SortOrder.RELEVANCE, query="apocalypse"))
    assert "ts_rank" in sql


@pytest.mark.parametrize("sort,expectedFragment", [
    (SortOrder.FAVORITES, "s.favorite_count DESC"),
    (SortOrder.FAVORITES_PER_1K, "s.favorites_per_1k_words DESC"),
    (SortOrder.WORDS, "s.word_count DESC"),
    (SortOrder.PUBLISHED, "s.published_at DESC"),
])
def testSortExpressions(sort, expectedFragment):
    sql, _ = buildSearchQuery(SearchFilters(sort=sort))
    assert expectedFragment in sql


def testPaginationIsClampedAndOffsetCorrectly():
    _, parameters = buildSearchQuery(SearchFilters(page=3, pageSize=25))
    assert parameters["limit"] == 25
    assert parameters["offset"] == 50

    _, clamped = buildSearchQuery(SearchFilters(pageSize=5000))
    assert clamped["limit"] == 100


def testPageZeroDoesNotProduceNegativeOffset():
    _, parameters = buildSearchQuery(SearchFilters(page=0))
    assert parameters["offset"] == 0


def testResultsAlwaysCarryATieBreaker():
    """Without a deterministic tie-break, pagination can repeat or skip rows."""
    sql, _ = buildSearchQuery(SearchFilters())
    assert "s.story_id DESC" in sql
