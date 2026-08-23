-- Two-tier vector storage, chosen for RAM rather than disk.
--
-- An HNSW index over vector(768) costs ~4KB per story -- larger than the 3KB
-- vector it indexes -- and building it over ~8.8M stories needs roughly 28GB of
-- maintenance_work_mem. This machine has ~13GB available inside WSL and 5.9GB
-- free on the host, so that build simply cannot happen in memory; pgvector
-- would fall back to an on-disk build lasting days.
--
-- Instead:
--   summary_embedding      halfvec(768)  ~1.5KB/story, never indexed, read only
--                                        for exact reranking of a shortlist
--   summary_embedding_bits bit(768)      96 bytes/story, HNSW + Hamming
--
-- The bit index over 8.8M stories is ~2GB and builds comfortably in RAM. Search
-- is a Hamming prefilter followed by an exact halfvec rerank of the top
-- candidates, which recovers nearly all the recall of a full-precision index at
-- a fraction of the memory.
--
-- halfvec loses precision below float32, but embedding values sit in a narrow
-- range where 10 mantissa bits are ample; cosine ordering is unaffected in
-- practice.

ALTER TABLE stories DROP COLUMN IF EXISTS summary_embedding;

ALTER TABLE stories ADD COLUMN summary_embedding halfvec(768);

-- Derived, so it can never drift from the vector it quantizes.
ALTER TABLE stories ADD COLUMN summary_embedding_bits bit(768)
    GENERATED ALWAYS AS (binary_quantize(summary_embedding)::bit(768)) STORED;

-- Built after the backfill, not now: one bulk build is far cheaper than
-- maintaining the graph across millions of inserts.
--   SET maintenance_work_mem = '4GB';
--   CREATE INDEX stories_embedding_bits_idx ON stories
--       USING hnsw (summary_embedding_bits bit_hamming_ops);
