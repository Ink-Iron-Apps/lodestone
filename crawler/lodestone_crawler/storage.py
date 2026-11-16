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

# A fandom is identified by its archive URL, not by its title. FFN lists the
# same title in several sections as separate archives -- "Teen Titans" is 1,800
# comic stories and 41,300 cartoon ones -- so keying on name merged them and
# left the loser uncrawled. See db/migrations/009_fandom_identity.sql.
UPSERT_FANDOM_SQL = """
INSERT INTO fandoms (name, section_slug, fandom_slug, category_id, story_count)
VALUES (%(name)s, %(sectionSlug)s, %(fandomSlug)s, %(categoryId)s, %(storyCount)s)
ON CONFLICT (section_slug, fandom_slug) WHERE fandom_slug IS NOT NULL DO UPDATE SET
    name        = COALESCE(EXCLUDED.name, fandoms.name),
    -- Never overwrite a known id/count with the NULLs a bare listing row carries.
    category_id = COALESCE(EXCLUDED.category_id, fandoms.category_id),
    story_count = COALESCE(EXCLUDED.story_count, fandoms.story_count)
RETURNING id
"""

# A listing row may name a fandom we have never walked, and carries no slug for
# it. Name is then the only thing to deduplicate on, so this is scoped to rows
# that have no slug -- it can never again merge two real archives.
UPSERT_UNSLUGGED_FANDOM_SQL = """
INSERT INTO fandoms (name, category_id, story_count)
VALUES (%(name)s, %(categoryId)s, %(storyCount)s)
ON CONFLICT (name) WHERE fandom_slug IS NULL DO UPDATE SET
    category_id = COALESCE(EXCLUDED.category_id, fandoms.category_id),
    story_count = COALESCE(EXCLUDED.story_count, fandoms.story_count)
RETURNING id
"""


class StoryStore:
    def __init__(self, connectionString: str) -> None:
        self._connection = psycopg.connect(connectionString, row_factory=dict_row, autocommit=False)

    def close(self) -> None:
        self._connection.close()

    def commit(self) -> None:
        """Flush pending work.

        `upsertFandom` deliberately does not commit -- it is called inside
        `upsertStories`, whose transaction must stay atomic. That makes it a
        footgun for any caller using it on its own: the connection is not
        autocommit, so uncommitted rows are discarded on close. Callers that
        only touch fandoms must call this.
        """
        self._connection.commit()

    def __enter__(self) -> "StoryStore":
        return self

    def __exit__(self, *exceptionInfo) -> None:
        self.close()

    def upsertFandom(self, fandom: Fandom) -> int:
        """Write a fandom and return its id, keyed on the archive URL.

        A sighting with no slug takes the name-scoped path instead. Resolving by
        category id first is what keeps the crossover walk from inventing a
        second row for a fandom it already knows: the ids in a crossover URL are
        the only identifier that walk has.
        """
        if fandom.fandomSlug is None and fandom.categoryId is not None:
            known = self.getFandomByCategoryId(fandom.categoryId)
            if known is not None:
                return known["id"]

        statement = (UPSERT_FANDOM_SQL if fandom.fandomSlug is not None
                     else UPSERT_UNSLUGGED_FANDOM_SQL)
        parameters = {
            "name": fandom.name,
            "categoryId": fandom.categoryId,
            "storyCount": fandom.storyCount,
        }
        if fandom.fandomSlug is not None:
            parameters["sectionSlug"] = fandom.sectionSlug
            parameters["fandomSlug"] = fandom.fandomSlug

        with self._connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchone()["id"]

    def getFandomByCategoryId(self, categoryId: int) -> Optional[dict]:
        """The fandom FFN numbers `categoryId`, if we have walked or listed it.

        This is the lookup the crossover walk needs and did not have. Without
        it, backfillCrossoverPair built a parent fandom by de-slugging the URL,
        which produced 4,509 near-miss duplicate rows holding 166,463 story
        links -- "Star Wars The Clone Wars" beside the real "Star Wars: The
        Clone Wars".
        """
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM fandoms WHERE category_id = %s", (categoryId,))
            return cursor.fetchone()

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

    def upsertCrossoverPairs(self, pairs: list[tuple[int, int, str]]) -> int:
        """Record discovered crossover pairs.

        Pairs arrive as (idA, idB, urlSlug) already ordered by ascending
        category id, which is how FFN builds the archive URL. Because each pair
        is listed on both partners' pages, the same tuple is discovered twice
        and the conflict clause makes the second sighting free.

        The per-fandom slugs are taken from the fandoms table by id rather than
        cut out of `urlSlug`, which cannot be divided reliably -- see
        parser.CROSSOVER_PAIR_PATTERN. Either may be NULL for a fandom we have
        not walked; the archive URL is routed on the ids regardless.
        """
        if not pairs:
            return 0
        with self._connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO crossover_pairs (fandom_id_a, fandom_id_b, slug_a, slug_b)
                VALUES (
                    %s, %s,
                    (SELECT fandom_slug FROM fandoms WHERE category_id = %s),
                    (SELECT fandom_slug FROM fandoms WHERE category_id = %s)
                )
                ON CONFLICT DO NOTHING
                """,
                [(idA, idB, idA, idB) for idA, idB, _ in pairs],
            )
        self._connection.commit()
        return len(pairs)

    def iterCrossoverPairs(self, limit: Optional[int] = None) -> list[dict]:
        """Pairs whose archive has not yet been walked to exhaustion."""
        with self._connection.cursor() as cursor:
            # Slugs come from the fandoms table, falling back to whatever was
            # stored with the pair. The stored copy is only a cache: the ids are
            # the identity, and a slug repaired in fandoms should take effect
            # here without rewriting crossover_pairs.
            cursor.execute(
                """
                SELECT p.fandom_id_a, p.fandom_id_b,
                       COALESCE(fa.fandom_slug, p.slug_a) AS slug_a,
                       COALESCE(fb.fandom_slug, p.slug_b) AS slug_b,
                       fa.id AS id_a, fb.id AS id_b,
                       fa.name AS name_a, fb.name AS name_b
                FROM crossover_pairs p
                LEFT JOIN crawl_state c
                    ON c.surface_key = 'crossover:' || p.fandom_id_a || '/' || p.fandom_id_b
                LEFT JOIN fandoms fa ON fa.category_id = p.fandom_id_a
                LEFT JOIN fandoms fb ON fb.category_id = p.fandom_id_b
                WHERE c.is_exhausted IS NOT TRUE
                ORDER BY p.fandom_id_a, p.fandom_id_b
                LIMIT %s
                """,
                (limit,),
            )
            return cursor.fetchall()

    def iterPendingFandoms(self, limit: Optional[int] = None) -> list[dict]:
        """Crawlable fandoms not yet exhausted, smallest first.

        Smallest-first because the corpus becomes useful long before it becomes
        complete: roughly 12,000 of 13,117 fandoms finish in the first few days,
        while Harry Potter alone is over two days of crawling on its own.
        Unknown counts sort last rather than first, so an unmeasured fandom
        cannot stall the head of the queue.
        """
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT f.name, f.section_slug, f.fandom_slug, f.story_count
                FROM fandoms f
                LEFT JOIN crawl_state c
                    ON c.surface_key = 'browse:' || f.section_slug || '/' || f.fandom_slug
                WHERE f.section_slug IS NOT NULL
                  AND f.fandom_slug IS NOT NULL
                  AND c.is_exhausted IS NOT TRUE
                ORDER BY f.story_count NULLS LAST, f.name
                LIMIT %s
                """,
                (limit,),
            )
            return cursor.fetchall()

    def countUnembedded(self) -> int:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) AS pending FROM stories "
                "WHERE summary_embedding IS NULL AND deleted_at IS NULL"
            )
            return cursor.fetchone()["pending"]

    def countCrossoverPairs(self) -> int:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM crossover_pairs")
            return cursor.fetchone()["total"]
