-- Full-text search over the fields FFN itself indexes (title + summary) plus
-- author name, which FFN's search cannot match at all.
--
-- Applied automatically to a fresh volume via schema.sql; run this file by hand
-- against an existing database:
--   docker exec -i lodestone-postgres-1 psql -U lodestone -d lodestone -f - < db/migrations/002_fulltext.sql

ALTER TABLE stories
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', title), 'A') ||
        setweight(to_tsvector('english', author_name), 'B') ||
        setweight(to_tsvector('english', summary), 'C')
    ) STORED;

CREATE INDEX IF NOT EXISTS stories_search_idx ON stories USING gin (search_vector);

-- Trigram index for substring matching on titles, so a partial or misspelled
-- title still finds the story. Postgres FTS alone cannot do this.
CREATE INDEX IF NOT EXISTS stories_title_trgm_idx ON stories USING gin (title gin_trgm_ops);
