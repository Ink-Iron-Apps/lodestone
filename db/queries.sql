-- Queries FanFiction.Net's own search cannot express.
--
-- Each of these is the reference implementation for a planned API endpoint.
-- Run them against a crawled corpus with:
--   docker exec -i lodestone-postgres-1 psql -U lodestone -d lodestone -f -

-- ---------------------------------------------------------------------------
-- 1. Abandonment detection
--    FFN's status filter is binary (In-Progress / Complete) and says nothing
--    about whether "in progress" means "updated last week" or "left for dead in
--    2013". This is the single most common complaint about its browse.
-- ---------------------------------------------------------------------------

SELECT title, author_name, word_count, chapter_count,
       date_trunc('day', updated_at)::date AS last_touched,
       EXTRACT(YEAR FROM age(now(), updated_at))::int AS years_stale
FROM stories
WHERE status = 'in_progress'
  AND deleted_at IS NULL
  AND updated_at < now() - interval '2 years'
  AND word_count > 20000            -- only the ones worth mourning
ORDER BY favorite_count DESC
LIMIT 20;

-- ---------------------------------------------------------------------------
-- 2. Length-adjusted popularity
--    FFN sorts by raw favourites, which structurally buries every good short
--    work beneath every long mediocre one.
-- ---------------------------------------------------------------------------

SELECT title, author_name, word_count, favorite_count, favorites_per_1k_words
FROM stories
WHERE deleted_at IS NULL
  AND word_count BETWEEN 1000 AND 20000
  AND favorite_count > 20
ORDER BY favorites_per_1k_words DESC
LIMIT 20;

-- ---------------------------------------------------------------------------
-- 3. Ships as first-class objects
--    FFN's [A, B] bracket syntax is the only relationship signal on the entire
--    site, and its own filters cannot query it -- characterid1..4 match
--    "appears in", not "is paired with".
-- ---------------------------------------------------------------------------

SELECT ship, COUNT(*) AS story_count,
       SUM(word_count) AS total_words,
       ROUND(AVG(favorite_count)) AS mean_favorites
FROM (
    SELECT jsonb_array_elements(ships) AS ship_array, word_count, favorite_count
    FROM stories WHERE deleted_at IS NULL AND jsonb_array_length(ships) > 0
) AS exploded,
LATERAL (
    SELECT string_agg(member, ' / ' ORDER BY member) AS ship
    FROM jsonb_array_elements_text(ship_array) AS member
) AS normalized
GROUP BY ship
ORDER BY story_count DESC
LIMIT 20;

-- ---------------------------------------------------------------------------
-- 4. Exclusions
--    FFN has no "not this character" or "not this genre" filter at all.
-- ---------------------------------------------------------------------------

SELECT title, author_name, genres, characters, word_count, favorite_count
FROM stories
WHERE deleted_at IS NULL
  AND status = 'complete'
  AND word_count > 10000
  AND NOT (genres && ARRAY['Romance'])          -- exclude a genre
  AND NOT (characters && ARRAY['OC'])           -- exclude original characters
ORDER BY favorite_count DESC
LIMIT 20;

-- ---------------------------------------------------------------------------
-- 5. Corpus shape
--    Sanity checks on a crawl: coverage, date range, completion mix.
-- ---------------------------------------------------------------------------

SELECT
    COUNT(*)                                            AS stories,
    COUNT(DISTINCT author_id)                           AS authors,
    COUNT(*) FILTER (WHERE status = 'complete')         AS complete,
    COUNT(*) FILTER (WHERE jsonb_array_length(ships) > 0) AS with_ships,
    COUNT(*) FILTER (WHERE is_crossover)                AS crossovers,
    COUNT(DISTINCT language)                            AS languages,
    pg_size_pretty(SUM(pg_column_size(stories.*))::bigint) AS approx_row_bytes,
    MIN(published_at)::date                             AS earliest,
    MAX(published_at)::date                             AS latest
FROM stories
WHERE deleted_at IS NULL;
