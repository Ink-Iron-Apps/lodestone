"""Query-builder tests.

These need no database: they assert on the SQL fragments and bound parameters,
which is where the filter logic actually lives. The point is to pin the
distinctions that are easy to get subtly wrong -- include vs exclude, "overlaps"
vs "contains", and paired-with vs merely-present.
"""
from __future__ import annotations

import pytest

from lodestone_api.queries import (
    COUNT_CEILING,
    SearchFilters,
    SortOrder,
    buildCountQuery,
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


# --------------------------------------------------------------------------
# Semantic search
# --------------------------------------------------------------------------

SAMPLE_VECTOR = "[" + ",".join(["0.1"] * 768) + "]"


def testSemanticSearchExcludesUnembeddedStories():
    """Rows crawled before the embedding pass have no vector. Including them
    would drop unranked results into the middle of a ranked list."""
    whereBody, _ = buildWhereClause(SearchFilters(semanticVector=SAMPLE_VECTOR))
    assert "s.summary_embedding IS NOT NULL" in whereBody


def testSemanticSearchUsesHammingPrefilterThenExactRerank():
    """The full-precision index this would need does not fit in RAM, so the
    indexed column is a binary quantization and the exact distance only ever
    reranks the shortlist it returns."""
    sql, parameters = buildSearchQuery(
        SearchFilters(semanticVector=SAMPLE_VECTOR, sort=SortOrder.SEMANTIC)
    )
    assert "summary_embedding_bits <~> binary_quantize" in sql
    assert "WITH candidates AS" in sql
    assert parameters["candidateLimit"] >= 500


def testSemanticFiltersApplyInsideTheCandidateStage():
    """Filtering after the prefilter would let an exclusion empty the page while
    matching stories sat just outside the shortlist."""
    sql, _ = buildSearchQuery(SearchFilters(
        semanticVector=SAMPLE_VECTOR, sort=SortOrder.SEMANTIC, excludedGenres=["Romance"]
    ))
    candidateStage = sql[sql.index("WITH candidates"):sql.index("ORDER BY s.summary_embedding_bits")]
    assert "NOT (s.genres && %(excludedGenres)s)" in candidateStage


def testSemanticSortUsesCosineDistanceAscending():
    sql, parameters = buildSearchQuery(
        SearchFilters(semanticVector=SAMPLE_VECTOR, sort=SortOrder.SEMANTIC)
    )
    assert "s.summary_embedding <=> %(semanticVector)s::halfvec ASC" in sql
    assert parameters["semanticVector"] == SAMPLE_VECTOR


def testSemanticSortFallsBackWithoutAVector():
    """If the embedding server was unreachable the request still has to answer,
    so it degrades to recency rather than emitting invalid SQL."""
    sql, parameters = buildSearchQuery(SearchFilters(sort=SortOrder.SEMANTIC))
    assert "<=>" not in sql
    assert "semanticVector" not in parameters
    assert "s.updated_at DESC" in sql


def testSemanticCombinesWithStructuredFilters():
    """The actual differentiator: meaning-matching intersected with exclusions,
    ships and completion state. Neither FFN nor AO3 can express this."""
    sql, parameters = buildSearchQuery(SearchFilters(
        semanticVector=SAMPLE_VECTOR,
        sort=SortOrder.SEMANTIC,
        excludedGenres=["Romance"],
        ship=["A. Crowley", "Aziraphale"],
        status="complete",
        excludeAbandoned=True,
    ))
    assert "<=>" in sql
    assert "NOT (s.genres && %(excludedGenres)s)" in sql
    assert "jsonb_array_elements(s.ships)" in sql
    assert "s.status = %(status)s::story_status" in sql
    assert parameters["shipSize"] == 2


def testRelevanceSortPrefersSemanticWhenAVectorIsPresent():
    """A user who asked for meaning matching and left sort on Relevance should
    get the vector ranking, not lexical ranking over a query they never gave."""
    sql, _ = buildSearchQuery(
        SearchFilters(semanticVector=SAMPLE_VECTOR, sort=SortOrder.RELEVANCE)
    )
    assert "<=>" in sql
    assert "ts_rank" not in sql


# --------------------------------------------------------------------------
# Scale
# --------------------------------------------------------------------------

def testSearchDoesNotCountInline():
    """A window count over the result set forces a scan of every match and
    stops the planner using the ORDER BY index. At 3.5M rows that took an
    unfiltered search from milliseconds to over a minute."""
    sql, _ = buildSearchQuery(SearchFilters())
    assert "COUNT(*) OVER" not in sql
    assert "total_count" not in sql


def testCountIsCapped():
    """Counting exactly means visiting every match. The inner LIMIT lets
    Postgres stop early, so a broad query costs the ceiling, not the corpus."""
    sql, parameters = buildCountQuery(SearchFilters())
    assert "LIMIT %(countCeiling)s" in sql
    assert parameters["countCeiling"] == COUNT_CEILING


def testCountAppliesTheSameFilters():
    """The count and the page must agree, or the header contradicts the list."""
    _, searchParameters = buildSearchQuery(
        SearchFilters(excludedGenres=["Romance"], status="complete"))
    countSql, countParameters = buildCountQuery(
        SearchFilters(excludedGenres=["Romance"], status="complete"))
    assert "NOT (s.genres && %(excludedGenres)s)" in countSql
    assert countParameters["excludedGenres"] == searchParameters["excludedGenres"]
    assert countParameters["status"] == searchParameters["status"]


def testSortsRequestNullsLastConsistently():
    """Every DESC sort must say NULLS LAST, and the indexes must be declared the
    same way -- a plain DESC index is NULLS FIRST, a different ordering the
    planner will refuse to use. See db/migrations/007_sortable_indexes.sql."""
    for sort in (SortOrder.UPDATED, SortOrder.PUBLISHED, SortOrder.WORDS,
                 SortOrder.FAVORITES_PER_1K):
        sql, _ = buildSearchQuery(SearchFilters(sort=sort))
        orderBy = sql[sql.rindex("ORDER BY"):]
        assert "NULLS LAST" in orderBy, f"{sort} lost its NULLS LAST"
