"""Search query construction.

Every filter here is one FanFiction.Net cannot express. Its own browse offers
include-only character and genre pickers, a binary completion flag, and three
sort orders; there is no exclusion of anything, no cross-fandom search, no way
to query a relationship as opposed to a cast list, and no notion of a story
having been abandoned.

Filters are composed as parameterized SQL fragments rather than string
interpolation -- user input reaches Postgres only as bound values.
"""
from __future__ import annotations

import dataclasses
import enum
from typing import Any, Optional

MAX_PAGE_SIZE = 100

# Counting matches exactly means visiting every matching row. An earlier version
# rode a COUNT(*) OVER () along with the page, which was free on a 2,088-row
# pilot and catastrophic at 3.5M: it forced a sequential scan on every search
# and stopped the planner using the ORDER BY index, so an unfiltered query took
# over a minute to return 25 rows.
#
# Counting up to a cap instead bounds the work. Past the cap the UI says
# "10,000+", which is all a result header can usefully convey anyway.
COUNT_CEILING = 10_000


class SortOrder(enum.StrEnum):
    RELEVANCE = "relevance"
    # Nearest-neighbour on the summary embedding. Only meaningful alongside a
    # semantic query, and falls back to recency without one.
    SEMANTIC = "semantic"
    UPDATED = "updated"
    PUBLISHED = "published"
    FAVORITES = "favorites"
    REVIEWS = "reviews"
    FOLLOWS = "follows"
    WORDS = "words"
    # The one FFN structurally cannot offer, because it never computes it.
    FAVORITES_PER_1K = "favorites_per_1k"


SORT_EXPRESSIONS = {
    SortOrder.UPDATED: "s.updated_at DESC NULLS LAST",
    SortOrder.PUBLISHED: "s.published_at DESC NULLS LAST",
    SortOrder.FAVORITES: "s.favorite_count DESC",
    SortOrder.REVIEWS: "s.review_count DESC",
    SortOrder.FOLLOWS: "s.follow_count DESC",
    SortOrder.WORDS: "s.word_count DESC NULLS LAST",
    SortOrder.FAVORITES_PER_1K: "s.favorites_per_1k_words DESC NULLS LAST",
}

# A story counts as abandoned when it is still open and has not been touched in
# this long. Two years is the threshold readers use in practice.
ABANDONED_AFTER = "2 years"

# How many rows the Hamming prefilter hands to the exact reranker. Larger means
# better recall and more halfvec distance computations; 500 is comfortably
# enough for a 25-row page.
SEMANTIC_CANDIDATE_LIMIT = 500

STORY_COLUMNS = """
            s.story_id, s.title, s.author_id, s.author_name, s.summary,
            s.rating, s.language, s.genres, s.characters, s.ships,
            s.chapter_count, s.word_count, s.review_count, s.favorite_count,
            s.follow_count, s.status, s.published_at, s.updated_at,
            s.is_crossover, s.favorites_per_1k_words,
            (s.status = 'in_progress'
             AND s.updated_at < now() - interval '2 years') AS is_abandoned,
            COALESCE(
                (SELECT array_agg(f.name ORDER BY f.name)
                 FROM story_fandoms sf JOIN fandoms f ON f.id = sf.fandom_id
                 WHERE sf.story_id = s.story_id),
                '{}'
            ) AS fandoms"""


@dataclasses.dataclass(slots=True)
class SearchFilters:
    query: Optional[str] = None

    # Free text matched by meaning rather than by word. This is the query FFN
    # cannot express at all: nothing about "competent time-travel Harry who is
    # not a Gary Stu" need appear literally in the summary of a story that is
    # exactly that. Carries the already-embedded vector, not the text, so the
    # API layer owns the model call.
    semanticVector: Optional[str] = None

    fandoms: list[str] = dataclasses.field(default_factory=list)
    ratings: list[str] = dataclasses.field(default_factory=list)
    languages: list[str] = dataclasses.field(default_factory=list)

    genres: list[str] = dataclasses.field(default_factory=list)
    excludedGenres: list[str] = dataclasses.field(default_factory=list)

    characters: list[str] = dataclasses.field(default_factory=list)
    excludedCharacters: list[str] = dataclasses.field(default_factory=list)

    # A ship matches when the story declares a bracket group containing every
    # named member -- "these two are together", not "both appear".
    ship: list[str] = dataclasses.field(default_factory=list)

    status: Optional[str] = None
    isCrossover: Optional[bool] = None
    excludeAbandoned: bool = False
    onlyAbandoned: bool = False

    minWords: Optional[int] = None
    maxWords: Optional[int] = None
    minFavorites: Optional[int] = None
    minChapters: Optional[int] = None

    sort: SortOrder = SortOrder.UPDATED
    page: int = 1
    pageSize: int = 25


def buildWhereClause(filters: SearchFilters) -> tuple[str, dict[str, Any]]:
    """Return an SQL WHERE body plus its bound parameters."""
    conditions = ["s.deleted_at IS NULL"]
    parameters: dict[str, Any] = {}

    if filters.semanticVector:
        # Stories crawled before the embedding pass have no vector; including
        # them would put unranked rows in the middle of a ranked list.
        conditions.append("s.summary_embedding IS NOT NULL")

    if filters.query:
        # websearch_to_tsquery gives users quoted phrases and OR/- operators for
        # free, and never raises on malformed input the way to_tsquery does.
        conditions.append("s.search_vector @@ websearch_to_tsquery('english', %(query)s)")
        parameters["query"] = filters.query

    if filters.fandoms:
        conditions.append("""EXISTS (
            SELECT 1 FROM story_fandoms sf
            JOIN fandoms f ON f.id = sf.fandom_id
            WHERE sf.story_id = s.story_id AND f.name = ANY(%(fandoms)s)
        )""")
        parameters["fandoms"] = filters.fandoms

    if filters.ratings:
        conditions.append("s.rating = ANY(%(ratings)s)")
        parameters["ratings"] = filters.ratings

    if filters.languages:
        conditions.append("s.language = ANY(%(languages)s)")
        parameters["languages"] = filters.languages

    # && is "overlaps" (any of), @> would be "contains all". Readers asking for
    # Humor or Parody want either, not both.
    if filters.genres:
        conditions.append("s.genres && %(genres)s")
        parameters["genres"] = filters.genres

    if filters.excludedGenres:
        conditions.append("NOT (s.genres && %(excludedGenres)s)")
        parameters["excludedGenres"] = filters.excludedGenres

    if filters.characters:
        conditions.append("s.characters && %(characters)s")
        parameters["characters"] = filters.characters

    if filters.excludedCharacters:
        conditions.append("NOT (s.characters && %(excludedCharacters)s)")
        parameters["excludedCharacters"] = filters.excludedCharacters

    if filters.ship:
        # Every named member must appear inside one and the same bracket group.
        conditions.append("""EXISTS (
            SELECT 1 FROM jsonb_array_elements(s.ships) AS ship_group
            WHERE (
                SELECT COUNT(DISTINCT member)
                FROM jsonb_array_elements_text(ship_group) AS member
                WHERE member = ANY(%(ship)s)
            ) = %(shipSize)s
        )""")
        parameters["ship"] = filters.ship
        parameters["shipSize"] = len(set(filters.ship))

    if filters.status:
        conditions.append("s.status = %(status)s::story_status")
        parameters["status"] = filters.status

    if filters.isCrossover is not None:
        conditions.append("s.is_crossover = %(isCrossover)s")
        parameters["isCrossover"] = filters.isCrossover

    # Abandonment is derived, not stored: open, and untouched for a long time.
    abandonedCondition = (
        f"(s.status = 'in_progress' AND s.updated_at < now() - interval '{ABANDONED_AFTER}')"
    )
    if filters.excludeAbandoned:
        conditions.append(f"NOT {abandonedCondition}")
    if filters.onlyAbandoned:
        conditions.append(abandonedCondition)

    if filters.minWords is not None:
        conditions.append("s.word_count >= %(minWords)s")
        parameters["minWords"] = filters.minWords

    if filters.maxWords is not None:
        conditions.append("s.word_count <= %(maxWords)s")
        parameters["maxWords"] = filters.maxWords

    if filters.minFavorites is not None:
        conditions.append("s.favorite_count >= %(minFavorites)s")
        parameters["minFavorites"] = filters.minFavorites

    if filters.minChapters is not None:
        conditions.append("s.chapter_count >= %(minChapters)s")
        parameters["minChapters"] = filters.minChapters

    return " AND ".join(conditions), parameters


def buildCountQuery(filters: SearchFilters) -> tuple[str, dict[str, Any]]:
    """Count matches, stopping at COUNT_CEILING.

    The inner LIMIT lets Postgres stop early, so a broad query costs the ceiling
    rather than the corpus.
    """
    whereBody, parameters = buildWhereClause(filters)
    parameters["countCeiling"] = COUNT_CEILING
    sql = f"""
        SELECT count(*) AS matched FROM (
            SELECT 1 FROM stories s WHERE {whereBody} LIMIT %(countCeiling)s
        ) AS capped
    """
    return sql, parameters


def buildSearchQuery(filters: SearchFilters) -> tuple[str, dict[str, Any]]:
    whereBody, parameters = buildWhereClause(filters)

    if filters.semanticVector and filters.sort in (SortOrder.SEMANTIC, SortOrder.RELEVANCE):
        # Exact cosine distance on the halfvec column. Small is close, so
        # ascending. The Hamming prefilter in buildSearchQuery narrows the
        # candidate set first; this only ever reranks that shortlist.
        orderBy = "s.summary_embedding <=> %(semanticVector)s::halfvec ASC"
        parameters["semanticVector"] = filters.semanticVector
    elif filters.sort is SortOrder.RELEVANCE and filters.query:
        orderBy = "ts_rank(s.search_vector, websearch_to_tsquery('english', %(query)s)) DESC"
    else:
        # Relevance and semantic are both meaningless with nothing to rank
        # against, so fall back to recency.
        orderBy = SORT_EXPRESSIONS.get(filters.sort, SORT_EXPRESSIONS[SortOrder.UPDATED])

    pageSize = min(filters.pageSize, MAX_PAGE_SIZE)
    parameters["limit"] = pageSize
    parameters["offset"] = (max(filters.page, 1) - 1) * pageSize

    # Two-phase semantic search. The full-precision index this would otherwise
    # need does not fit in this machine's RAM, so the indexed column is a
    # binary quantization: Hamming distance over bit(768) narrows millions of
    # rows to a shortlist using an index that costs 96 bytes per story, and the
    # exact halfvec distance then reranks only that shortlist. Recall is close
    # to a full-precision index at a fraction of the memory.
    if filters.semanticVector and filters.sort in (SortOrder.SEMANTIC, SortOrder.RELEVANCE):
        parameters["candidateLimit"] = max(pageSize * 10, SEMANTIC_CANDIDATE_LIMIT)
        sql = f"""
            WITH candidates AS (
                SELECT s.story_id
                FROM stories s
                WHERE {whereBody}
                ORDER BY s.summary_embedding_bits <~> binary_quantize(%(semanticVector)s::halfvec)
                LIMIT %(candidateLimit)s
            )
            SELECT
                {STORY_COLUMNS}
            FROM stories s
            JOIN candidates c ON c.story_id = s.story_id
            ORDER BY {orderBy}, s.story_id DESC
            LIMIT %(limit)s OFFSET %(offset)s
        """
        return sql, parameters

    sql = f"""
        SELECT
            {STORY_COLUMNS}
        FROM stories s
        WHERE {whereBody}
        ORDER BY {orderBy}, s.story_id DESC
        LIMIT %(limit)s OFFSET %(offset)s
    """
    return sql, parameters


# Facet queries power the filter sidebar. Each is scoped to the whole corpus
# rather than the current result set -- cheap, and it keeps options from
# vanishing as the user narrows down, which is the usual complaint about
# result-scoped facets.

FACET_QUERIES = {
    name: (
        "SELECT value, count FROM facet_counts WHERE facet = %(facet)s "
        f"ORDER BY count DESC, value LIMIT {limit}"
    )
    for name, limit in [
        ("fandoms", 200), ("genres", 50), ("characters", 300),
        ("languages", 50), ("ratings", 10), ("ships", 100),
    ]
}

STATS_QUERY = "SELECT * FROM corpus_stats"
