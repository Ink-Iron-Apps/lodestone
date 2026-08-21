-- Lodestone schema.
--
-- Postgres is the system of record; Meilisearch is a disposable projection of
-- it. Anything that cannot be rebuilt from these tables by replaying a reindex
-- does not belong in the search engine.
--
-- Sizing: ~12M stories at ~500B/row is ~6GB of table plus indexes, and 12M
-- 384-dim float32 summary embeddings is ~18GB raw / ~2GB at int8. Comfortably a
-- single-box workload.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Fandoms
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fandoms (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    section_slug  TEXT,
    fandom_slug   TEXT,
    -- FFN's own numeric category id. Only knowable from a filter form or a
    -- crossover directory, so it stays nullable for fandoms first seen in a row.
    category_id   INTEGER,
    story_count   INTEGER,
    last_crawled_at TIMESTAMPTZ,
    UNIQUE (name)
);

CREATE INDEX IF NOT EXISTS fandoms_slug_idx ON fandoms (section_slug, fandom_slug);
CREATE INDEX IF NOT EXISTS fandoms_name_trgm_idx ON fandoms USING gin (name gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- Stories
-- ---------------------------------------------------------------------------

CREATE TYPE story_status AS ENUM ('in_progress', 'complete');

CREATE TABLE IF NOT EXISTS stories (
    story_id       BIGINT PRIMARY KEY,          -- FFN's own id, never reassigned
    title          TEXT NOT NULL,
    author_id      BIGINT NOT NULL,
    author_name    TEXT NOT NULL,
    summary        TEXT NOT NULL DEFAULT '',

    is_crossover   BOOLEAN NOT NULL DEFAULT FALSE,
    rating         TEXT,
    language       TEXT,
    genres         TEXT[] NOT NULL DEFAULT '{}',

    -- Every character named on the story, ship members included.
    characters     TEXT[] NOT NULL DEFAULT '{}',
    -- FFN's [A, B] bracket groups: the site's only relationship signal.
    -- jsonb rather than TEXT[][] because a story may declare several ships of
    -- differing arity.
    ships          JSONB NOT NULL DEFAULT '[]',

    chapter_count  INTEGER,
    word_count     INTEGER,
    review_count   INTEGER NOT NULL DEFAULT 0,
    favorite_count INTEGER NOT NULL DEFAULT 0,
    follow_count   INTEGER NOT NULL DEFAULT 0,

    status         story_status NOT NULL DEFAULT 'in_progress',
    published_at   TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ,

    cover_image_id BIGINT,

    -- Crawl bookkeeping. FFN deletes stories silently and offers no tombstone,
    -- so absence has to be inferred: last_seen_at going stale is the only
    -- signal there is.
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at     TIMESTAMPTZ,

    summary_embedding vector(384),

    -- Popularity per unit length. FFN sorts by raw favourites, which structurally
    -- buries every good short work under every long mediocre one.
    favorites_per_1k_words NUMERIC GENERATED ALWAYS AS (
        CASE WHEN word_count > 0
             THEN ROUND((favorite_count::NUMERIC * 1000) / word_count, 4)
        END
    ) STORED
);

CREATE INDEX IF NOT EXISTS stories_updated_idx     ON stories (updated_at DESC);
CREATE INDEX IF NOT EXISTS stories_published_idx   ON stories (published_at DESC);
CREATE INDEX IF NOT EXISTS stories_author_idx      ON stories (author_id);
CREATE INDEX IF NOT EXISTS stories_favorites_idx   ON stories (favorite_count DESC);
CREATE INDEX IF NOT EXISTS stories_ratio_idx       ON stories (favorites_per_1k_words DESC);
CREATE INDEX IF NOT EXISTS stories_genres_idx      ON stories USING gin (genres);
CREATE INDEX IF NOT EXISTS stories_characters_idx  ON stories USING gin (characters);
CREATE INDEX IF NOT EXISTS stories_ships_idx       ON stories USING gin (ships jsonb_path_ops);
CREATE INDEX IF NOT EXISTS stories_last_seen_idx   ON stories (last_seen_at) WHERE deleted_at IS NULL;

-- The query FFN cannot express at all: "in progress, but abandoned years ago".
CREATE INDEX IF NOT EXISTS stories_abandoned_idx ON stories (updated_at)
    WHERE status = 'in_progress' AND deleted_at IS NULL;

-- Built after the initial backfill, not before -- HNSW construction on an empty
-- table then incrementally filled is far slower than one bulk build.
-- CREATE INDEX stories_embedding_idx ON stories
--     USING hnsw (summary_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ---------------------------------------------------------------------------
-- Story <-> fandom (many-to-many: crossovers have exactly two)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS story_fandoms (
    story_id  BIGINT NOT NULL REFERENCES stories (story_id) ON DELETE CASCADE,
    fandom_id BIGINT NOT NULL REFERENCES fandoms (id) ON DELETE CASCADE,
    PRIMARY KEY (story_id, fandom_id)
);

CREATE INDEX IF NOT EXISTS story_fandoms_fandom_idx ON story_fandoms (fandom_id);

-- ---------------------------------------------------------------------------
-- Crawl state
-- ---------------------------------------------------------------------------

-- Resumability is a hard requirement, not a nicety: a full backfill is on the
-- order of a week of wall-clock, and it will be interrupted.
CREATE TABLE IF NOT EXISTS crawl_state (
    surface_key      TEXT PRIMARY KEY,   -- e.g. 'browse:book/Harry-Potter'
    last_page        INTEGER NOT NULL DEFAULT 0,
    is_exhausted     BOOLEAN NOT NULL DEFAULT FALSE,
    rows_ingested    BIGINT NOT NULL DEFAULT 0,
    last_error       TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
