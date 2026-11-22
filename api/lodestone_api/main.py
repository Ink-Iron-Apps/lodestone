"""Lodestone read API.

Queries Postgres directly rather than Meilisearch. At pilot scale the GIN and
btree indexes answer every filter in single-digit milliseconds, and going
straight to the system of record means there is no projection to keep in sync
and no way for the index to disagree with the truth. Meilisearch earns its place
when the corpus reaches a size where Postgres FTS ranking stops being good
enough -- not before.

Read-only by design: this service issues no writes, so it can be exposed
publicly while the crawler stays on a private residential connection.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any, Optional

import psycopg
import psycopg_pool
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row

from .embedding import EmbeddingUnavailable, embedQuery, toPgVector
from .queries import (
    COUNT_CEILING,
    FACET_QUERIES,
    STATS_QUERY,
    SearchFilters,
    SortOrder,
    buildCountQuery,
    buildSearchQuery,
)

DATABASE_DSN = os.environ.get(
    "LODESTONE_DSN", "postgresql://lodestone:lodestone@localhost:5433/lodestone"
)
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

logger = logging.getLogger(__name__)

connectionPool: Optional[psycopg_pool.ConnectionPool] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global connectionPool
    connectionPool = psycopg_pool.ConnectionPool(
        DATABASE_DSN, min_size=1, max_size=8, kwargs={"row_factory": dict_row}
    )
    connectionPool.wait(timeout=30)
    yield
    connectionPool.close()


app = FastAPI(
    title="Lodestone",
    description="Search FanFiction.Net the way its own search cannot.",
    version="0.1.0",
    lifespan=lifespan,
)


def runQuery(
    sql: str,
    parameters: dict[str, Any] | None = None,
    efSearch: Optional[int] = None,
) -> list[dict]:
    if connectionPool is None:
        raise HTTPException(status_code=503, detail="database pool not ready")
    try:
        with connectionPool.connection() as connection, connection.cursor() as cursor:
            if efSearch is not None:
                # How many candidates the HNSW graph is allowed to consider.
                # The default is 40, which silently caps the prefilter: asking
                # the index for 500 nearest neighbours returned 40 and the
                # exact reranker then reranked those 40, so the candidate limit
                # was decorative. set_config with is_local true, because SET
                # LOCAL takes no bind parameter -- it reaches the server as
                # `SET LOCAL hnsw.ef_search = $1` and fails to parse -- and
                # because local scope keeps the setting inside this transaction
                # rather than leaving it on a pooled connection for the next
                # borrower.
                cursor.execute(
                    "SELECT set_config('hnsw.ef_search', %s, true)",
                    (str(min(efSearch, 1000)),),
                )
            cursor.execute(sql, parameters or {})
            return cursor.fetchall()
    except psycopg.Error as error:
        raise HTTPException(status_code=500, detail=f"query failed: {error}") from error


def parseFilters(
    q: Annotated[Optional[str], Query(description="Full-text query over title, author and summary")] = None,
    semantic: Annotated[Optional[str], Query(description="Match summaries by meaning rather than by word")] = None,
    fandom: Annotated[list[str], Query()] = [],
    rating: Annotated[list[str], Query()] = [],
    language: Annotated[list[str], Query()] = [],
    genre: Annotated[list[str], Query()] = [],
    excludeGenre: Annotated[list[str], Query()] = [],
    character: Annotated[list[str], Query()] = [],
    excludeCharacter: Annotated[list[str], Query()] = [],
    ship: Annotated[list[str], Query(description="All members must share one bracket group")] = [],
    status: Annotated[Optional[str], Query(pattern="^(in_progress|complete)$")] = None,
    crossover: Annotated[Optional[bool], Query()] = None,
    excludeAbandoned: Annotated[bool, Query()] = False,
    onlyAbandoned: Annotated[bool, Query()] = False,
    minWords: Annotated[Optional[int], Query(ge=0)] = None,
    maxWords: Annotated[Optional[int], Query(ge=0)] = None,
    minFavorites: Annotated[Optional[int], Query(ge=0)] = None,
    minChapters: Annotated[Optional[int], Query(ge=1)] = None,
    # No default, so that a caller who said nothing about ordering can be told
    # apart from one who asked for recency. `?semantic=...` alone used to mean
    # the second: the vector was computed and then never ranked against,
    # because the semantic branch of the query builder only fires for
    # sort=semantic or sort=relevance. The response still reported
    # "semantic": true, so the results looked like nearest neighbours and were
    # the most recently updated stories that happen to have a vector -- which
    # is every story. Resolved below instead.
    sort: Annotated[Optional[SortOrder], Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=100)] = 25,
) -> SearchFilters:
    semanticVector = None
    if semantic:
        try:
            semanticVector = toPgVector(embedQuery(semantic))
        except EmbeddingUnavailable as error:
            # Semantic search is an enhancement, not the core product. Degrade
            # to the other filters rather than failing the whole request.
            logger.warning("semantic search unavailable, falling back: %s", error)

    if sort is None:
        # A semantic query with no sort asked for means rank by meaning. Any
        # other unsorted search keeps the recency default it has always had.
        sort = SortOrder.SEMANTIC if semanticVector else SortOrder.UPDATED

    return SearchFilters(
        query=q, semanticVector=semanticVector,
        fandoms=fandom, ratings=rating, languages=language,
        genres=genre, excludedGenres=excludeGenre,
        characters=character, excludedCharacters=excludeCharacter,
        ship=ship, status=status, isCrossover=crossover,
        excludeAbandoned=excludeAbandoned, onlyAbandoned=onlyAbandoned,
        minWords=minWords, maxWords=maxWords,
        minFavorites=minFavorites, minChapters=minChapters,
        sort=sort, page=page, pageSize=pageSize,
    )


@app.get("/api/search")
def search(filters: Annotated[SearchFilters, Depends(parseFilters)]) -> dict:
    sql, parameters = buildSearchQuery(filters)
    rows = runQuery(sql, parameters, efSearch=parameters.get("candidateLimit"))

    # Counted separately and capped. Riding a window count along with the page
    # looked free but forced a full scan of every match; see COUNT_CEILING.
    countSql, countParameters = buildCountQuery(filters)
    totalCount = runQuery(countSql, countParameters)[0]["matched"]

    for row in rows:
        # Every result links back to fanfiction.net. Lodestone indexes; it does
        # not host.
        row["url"] = f"https://www.fanfiction.net/s/{row['story_id']}/1/"

    return {
        "total": totalCount,
        "totalIsCapped": totalCount >= COUNT_CEILING,
        "page": filters.page,
        "pageSize": filters.pageSize,
        # Makes a degraded semantic search visible to the caller instead of
        # silently returning keyword results that look like semantic ones. True
        # only when the vector was actually ranked against: having embedded the
        # query is not the same as having used it, and claiming the one for the
        # other is how a sort bug stayed invisible.
        "semantic": filters.ranksBySimilarity,
        "results": rows,
    }


@app.get("/api/facets")
def facets() -> dict:
    """Filter vocabulary with corpus-wide counts."""
    return {name: runQuery(sql, {"facet": name}) for name, sql in FACET_QUERIES.items()}


@app.get("/api/stats")
def stats() -> dict:
    return runQuery(STATS_QUERY)[0]


@app.get("/api/healthz")
def healthz() -> dict:
    runQuery("SELECT 1 AS ok")
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
