-- Widen the embedding column to match nomic-embed-text.
--
-- The schema originally assumed a 384-wide model (all-minilm). nomic-embed-text
-- is 768-wide and materially stronger on short descriptive text, which is
-- exactly what a fic summary is. Any existing vectors are discarded rather than
-- padded: mixing widths, or mixing models at the same width, makes cosine
-- distance meaningless.
--
-- At 12M stories this column is ~37GB as float32. If that becomes the binding
-- constraint, halfvec(768) halves it for negligible recall loss on this kind of
-- lookup -- but do that as a deliberate migration, not by truncating here.

ALTER TABLE stories DROP COLUMN IF EXISTS summary_embedding;
ALTER TABLE stories ADD COLUMN summary_embedding vector(768);
