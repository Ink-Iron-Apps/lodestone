-- Make the sort indexes actually usable.
--
-- Every ORDER BY in the API says "DESC NULLS LAST", because a story with no
-- update date must not outrank one that has been updated. The indexes were
-- created as plain "DESC", which in Postgres means NULLS FIRST -- a different
-- ordering, so the planner ignored them entirely and fell back to a parallel
-- sequential scan plus a sort of every matching row.
--
-- Invisible on a 2,088-row pilot. At 3.5M rows an unfiltered search took over a
-- minute to return 25 rows.
--
-- The story_id tiebreaker is included so the index satisfies the full ORDER BY
-- rather than just its leading column.
--
-- CONCURRENTLY so the crawler keeps writing while these build. Run each
-- statement separately: CONCURRENTLY cannot run inside a transaction block.

CREATE INDEX CONCURRENTLY IF NOT EXISTS stories_updated_sort_idx
    ON stories (updated_at DESC NULLS LAST, story_id DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS stories_published_sort_idx
    ON stories (published_at DESC NULLS LAST, story_id DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS stories_words_sort_idx
    ON stories (word_count DESC NULLS LAST, story_id DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS stories_ratio_sort_idx
    ON stories (favorites_per_1k_words DESC NULLS LAST, story_id DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS stories_favorites_sort_idx
    ON stories (favorite_count DESC, story_id DESC);

-- Superseded by the NULLS LAST variants above.
DROP INDEX CONCURRENTLY IF EXISTS stories_updated_idx;
DROP INDEX CONCURRENTLY IF EXISTS stories_published_idx;
DROP INDEX CONCURRENTLY IF EXISTS stories_ratio_idx;
DROP INDEX CONCURRENTLY IF EXISTS stories_favorites_idx;
