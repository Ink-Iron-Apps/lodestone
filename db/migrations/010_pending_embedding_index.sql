-- Make the embedding queue cheap to poll.
--
-- The embed worker claims work with
--
--     SELECT story_id, title, summary FROM stories
--     WHERE summary_embedding IS NULL AND deleted_at IS NULL
--     ORDER BY story_id LIMIT n FOR UPDATE SKIP LOCKED
--
-- and no index covered that predicate, so the planner used stories_pkey and
-- filtered after fetching each heap tuple. With the backfill finished and
-- nothing matching, every execution walked all 7,943,122 primary-key entries
-- and visited every heap page before returning zero rows: about 7 minutes of
-- IO/DataFileRead per poll. The service polls by exiting and letting
-- `Restart=always` start it again, which had done this 3,165 times;
-- pg_statio_user_tables showed 59,989,898,403 heap blocks read from `stories`
-- in 8 days of uptime, 447 TB, the dominant read load on the host.
--
-- The same shape made the original backfill quadratic. The loop re-issues the
-- query once per batch, always from the start of the story_id order, so each
-- batch re-walked every row already embedded -- roughly 248,000 batches over a
-- growing prefix.
--
-- A partial index holds only the rows that still need a vector, so it is
-- effectively empty once the queue drains and the poll becomes an index scan
-- over nothing. It stays useful afterwards: the upsert nulls
-- `summary_embedding` when a summary is edited, and those rows reappear here.
CREATE INDEX CONCURRENTLY IF NOT EXISTS stories_pending_embedding_idx
    ON stories (story_id)
    WHERE summary_embedding IS NULL AND deleted_at IS NULL;
