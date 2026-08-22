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
    FACET_QUERIES,
    STATS_QUERY,
    SearchFilters,
    SortOrder,
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


def runQuery(sql: str, parameters: dict[str, Any] | None = None) -> list[dict]:
    if connectionPool is None:
        raise HTTPException(status_code=503, detail="database pool not ready")
    try:
        with connectionPool.connection() as connection, connection.cursor() as cursor:
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
    sort: Annotated[SortOrder, Query()] = SortOrder.UPDATED,
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
    rows = runQuery(sql, parameters)

    # COUNT(*) OVER () rides along on every row, so the total costs no extra
    # round trip -- but it is absent when the result set is empty.
    totalCount = rows[0]["total_count"] if rows else 0
    for row in rows:
        row.pop("total_count", None)
        # Every result links back to fanfiction.net. Lodestone indexes; it does
        # not host.
        row["url"] = f"https://www.fanfiction.net/s/{row['story_id']}/1/"

    return {
        "total": totalCount,
        "page": filters.page,
        "pageSize": filters.pageSize,
        # Makes a degraded semantic search visible to the caller instead of
        # silently returning keyword results that look like semantic ones.
        "semantic": filters.semanticVector is not None,
        "results": rows,
    }


@app.get("/api/facets")
def facets() -> dict:
    """Filter vocabulary with corpus-wide counts."""
    return {name: runQuery(sql) for name, sql in FACET_QUERIES.items()}


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
