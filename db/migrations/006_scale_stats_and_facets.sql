-- Precomputed stats and facets.
--
-- Both were written against a 2,088-row pilot where scanning the whole table
-- was free. At 3.5M rows /api/stats timed out and /api/facets took 8.6s: they
-- aggregate every row (COUNT FILTER over the table, GROUP BY over unnested
-- arrays) to render a sidebar that changes slowly and does not need to be
-- exact.
--
-- Materialized views make them index lookups. They are refreshed periodically
-- rather than on write, so the counts lag the corpus by minutes -- which is the
-- right trade for numbers whose only job is to label a filter.

-- Parallel workers need more shared memory than Docker's default 64MB /dev/shm
-- allows, and this view is built rarely. Serial is fine.
SET max_parallel_workers_per_gather = 0;

CREATE MATERIALIZED VIEW IF NOT EXISTS corpus_stats AS
SELECT
    count(*)                                              AS stories,
    count(DISTINCT author_id)                             AS authors,
    count(*) FILTER (WHERE status = 'complete')           AS complete,
    count(*) FILTER (WHERE status = 'in_progress'
        AND updated_at < now() - interval '2 years')      AS abandoned,
    count(*) FILTER (WHERE jsonb_array_length(ships) > 0) AS with_ships,
    count(*) FILTER (WHERE is_crossover)                  AS crossovers,
    COALESCE(sum(word_count), 0)                          AS total_words,
    min(published_at)::date                               AS earliest,
    max(published_at)::date                               AS latest,
    now()                                                 AS computed_at
FROM stories WHERE deleted_at IS NULL;

CREATE MATERIALIZED VIEW IF NOT EXISTS facet_counts AS
SELECT 'fandoms' AS facet, f.name AS value, count(*) AS count
FROM story_fandoms sf
JOIN fandoms f ON f.id = sf.fandom_id
JOIN stories s ON s.story_id = sf.story_id AND s.deleted_at IS NULL
GROUP BY f.name
UNION ALL
SELECT 'genres', genre, count(*)
FROM stories s, unnest(s.genres) AS genre WHERE s.deleted_at IS NULL GROUP BY genre
UNION ALL
SELECT 'characters', character_name, count(*)
FROM stories s, unnest(s.characters) AS character_name WHERE s.deleted_at IS NULL
GROUP BY character_name
UNION ALL
SELECT 'languages', language, count(*)
FROM stories WHERE deleted_at IS NULL AND language IS NOT NULL GROUP BY language
UNION ALL
SELECT 'ratings', rating, count(*)
FROM stories WHERE deleted_at IS NULL AND rating IS NOT NULL GROUP BY rating
UNION ALL
SELECT 'ships', ship, count(*)
FROM (
    SELECT jsonb_array_elements(ships) AS ship_group
    FROM stories WHERE deleted_at IS NULL AND jsonb_array_length(ships) > 0
) AS exploded,
LATERAL (
    SELECT string_agg(member, ' / ' ORDER BY member) AS ship
    FROM jsonb_array_elements_text(ship_group) AS member
) AS normalized
GROUP BY ship;

-- CONCURRENTLY refresh needs a unique index, so the sidebar stays readable
-- while the view rebuilds.
CREATE UNIQUE INDEX IF NOT EXISTS facet_counts_key ON facet_counts (facet, value);
CREATE INDEX IF NOT EXISTS facet_counts_lookup ON facet_counts (facet, count DESC);
