"""Postgres persistence.

Upserts are idempotent by design: the same page may be crawled many times (the
Just In firehose overlaps itself, and re-crawls revisit pages), so ingesting a
record twice must be a no-op beyond bumping `last_seen_at`.
"""
from __future__ import annotations

import json
import logging
from typing import Iterable, Optional

import psycopg
from psycopg.rows import dict_row

from .models import Fandom, StoryRecord

logger = logging.getLogger(__name__)

UPSERT_STORY_SQL = """
INSERT INTO stories (
    story_id, title, author_id, author_name, summary, is_crossover, rating,
    language, genres, characters, ships, chapter_count, word_count,
    review_count, favorite_count, follow_count, status, published_at,
    updated_at, cover_image_id, last_seen_at
) VALUES (
    %(storyId)s, %(title)s, %(authorId)s, %(authorName)s, %(summary)s,
    %(isCrossover)s, %(rating)s, %(language)s, %(genres)s, %(characters)s,
    %(ships)s, %(chapterCount)s, %(wordCount)s, %(reviewCount)s,
    %(favoriteCount)s, %(followCount)s, %(status)s, %(publishedAt)s,
    %(updatedAt)s, %(coverImageId)s, now()
)
ON CONFLICT (story_id) DO UPDATE SET
    title          = EXCLUDED.title,
    author_name    = EXCLUDED.author_name,
    summary        = EXCLUDED.summary,
    rating         = EXCLUDED.rating,
    language       = EXCLUDED.language,
    genres         = EXCLUDED.genres,
    characters     = EXCLUDED.characters,
    ships          = EXCLUDED.ships,
    chapter_count  = EXCLUDED.chapter_count,
    word_count     = EXCLUDED.word_count,
    review_count   = EXCLUDED.review_count,
    favorite_count = EXCLUDED.favorite_count,
    follow_count   = EXCLUDED.follow_count,
    status         = EXCLUDED.status,
    updated_at     = EXCLUDED.updated_at,
    cover_image_id = EXCLUDED.cover_image_id,
    last_seen_at   = now(),
    -- Seeing a story again resurrects it: FFN restores stories often enough
    -- that a tombstone must not be permanent.
    deleted_at     = NULL,
    -- Only recompute the embedding when the text it was derived from changed.
    summary_embedding = CASE
        WHEN stories.summary IS DISTINCT FROM EXCLUDED.summary THEN NULL
        ELSE stories.summary_embedding
    END
"""

UPSERT_FANDOM_SQL = """
INSERT INTO fandoms (name, section_slug, fandom_slug, category_id)
VALUES (%(name)s, %(sectionSlug)s, %(fandomSlug)s, %(categoryId)s)
ON CONFLICT (name) DO UPDATE SET
    -- Never overwrite a known slug/id with the NULLs a bare listing row carries.
    section_slug = COALESCE(EXCLUDED.section_slug, fandoms.section_slug),
    fandom_slug  = COALESCE(EXCLUDED.fandom_slug,  fandoms.fandom_slug),
    category_id  = COALESCE(EXCLUDED.category_id,  fandoms.category_id)
RETURNING id
"""


class StoryStore:
    def __init__(self, connectionString: str) -> None:
        self._connection = psycopg.connect(connectionString, row_factory=dict_row, autocommit=False)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "StoryStore":
        return self

    def __exit__(self, *exceptionInfo) -> None:
        self.close()

    def upsertFandom(self, fandom: Fandom) -> int:
        with self._connection.cursor() as cursor:
            cursor.execute(UPSERT_FANDOM_SQL, {
                "name": fandom.name,
                "sectionSlug": fandom.sectionSlug,
                "fandomSlug": fandom.fandomSlug,
                "categoryId": fandom.categoryId,
            })
            return cursor.fetchone()["id"]

    def upsertStories(self, records: Iterable[StoryRecord]) -> int:
        """Write a batch of stories and their fandom links in one transaction."""
        recordList = list(records)
        if not recordList:
            return 0

        with self._connection.cursor() as cursor:
            cursor.executemany(UPSERT_STORY_SQL, [
                {
                    "storyId": record.storyId,
                    "title": record.title,
                    "authorId": record.authorId,
                    "authorName": record.authorName,
                    "summary": record.summary,
                    "isCrossover": record.isCrossover,
                    "rating": record.rating,
                    "language": record.language,
                    "genres": record.genres,
                    "characters": record.characters,
                    "ships": json.dumps(record.ships),
                    "chapterCount": record.chapterCount,
                    "wordCount": record.wordCount,
                    "reviewCount": record.reviewCount,
                    "favoriteCount": record.favoriteCount,
                    "followCount": record.followCount,
                    "status": str(record.status),
                    "publishedAt": record.publishedAt,
                    "updatedAt": record.updatedAt,
                    "coverImageId": record.coverImageId,
                }
                for record in recordList
            ])

            for record in recordList:
                for fandom in record.fandoms:
                    fandomId = self.upsertFandom(fandom)
                    cursor.execute(
                        "INSERT INTO story_fandoms (story_id, fandom_id) VALUES (%s, %s) "
                        "ON CONFLICT DO NOTHING",
                        (record.storyId, fandomId),
                    )

        self._connection.commit()
        return len(recordList)

    # -- crawl state -------------------------------------------------------

    def getCrawlState(self, surfaceKey: str) -> Optional[dict]:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT * FROM crawl_state WHERE surface_key = %s", (surfaceKey,))
            return cursor.fetchone()

    def recordCrawlProgress(
        self,
        surfaceKey: str,
        lastPage: int,
        rowsIngested: int,
        isExhausted: bool = False,
        lastError: Optional[str] = None,
    ) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO crawl_state (surface_key, last_page, rows_ingested, is_exhausted, last_error, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (surface_key) DO UPDATE SET
                    last_page     = EXCLUDED.last_page,
                    rows_ingested = crawl_state.rows_ingested + EXCLUDED.rows_ingested,
                    is_exhausted  = EXCLUDED.is_exhausted,
                    last_error    = EXCLUDED.last_error,
                    updated_at    = now()
                """,
                (surfaceKey, lastPage, rowsIngested, isExhausted, lastError),
            )
        self._connection.commit()

    def markStale(self, staleDays: int = 180) -> int:
        """Tombstone stories not re-observed in a long time.

        FFN deletes silently, so this is inference, not fact -- which is why it
        is a reversible `deleted_at` rather than a DELETE.
        """
        with self._connection.cursor() as cursor:
            cursor.execute(
                "UPDATE stories SET deleted_at = now() "
                "WHERE deleted_at IS NULL AND last_seen_at < now() - make_interval(days => %s)",
                (staleDays,),
            )
            affected = cursor.rowcount
        self._connection.commit()
        return affected

    # -- crossover pairs ---------------------------------------------------

    def upsertCrossoverPairs(self, pairs: list[tuple[int, int, str, str]]) -> int:
        """Record discovered crossover pairs.

        Pairs arrive already ordered by ascending category id, which is how FFN
        builds the archive URL. Because each pair is listed on both partners'
        pages, the same tuple is discovered twice and the conflict clause makes
        the second sighting free.
        """
        if not pairs:
            return 0
        with self._connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO crossover_pairs (fandom_id_a, fandom_id_b, slug_a, slug_b) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                pairs,
            )
        self._connection.commit()
        return len(pairs)

    def iterCrossoverPairs(self, limit: Optional[int] = None) -> list[dict]:
        """Pairs whose archive has not yet been walked to exhaustion."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.fandom_id_a, p.fandom_id_b, p.slug_a, p.slug_b
                FROM crossover_pairs p
                LEFT JOIN crawl_state c
                    ON c.surface_key = 'crossover:' || p.fandom_id_a || '/' || p.fandom_id_b
                WHERE c.is_exhausted IS NOT TRUE
                ORDER BY p.fandom_id_a, p.fandom_id_b
                LIMIT %s
                """,
                (limit,),
            )
            return cursor.fetchall()

    def countCrossoverPairs(self) -> int:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM crossover_pairs")
            return cursor.fetchone()["total"]
