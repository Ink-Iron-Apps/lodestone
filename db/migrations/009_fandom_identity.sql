-- Give fandoms a real identity, and undo the rows that the wrong one created.
--
-- A fandom was identified by its name alone (`fandoms_name_key`), and every
-- write went through `ON CONFLICT (name) DO UPDATE`. FFN does not agree: the
-- same title appears in several sections as separate archives with separate
-- story counts and separate numeric category ids. "Teen Titans" is a comic
-- archive of 1,800 stories and a cartoon archive of 41,300; "Warriors" is a
-- 60-story movie archive and a 27,400-story book archive. Name-only identity
-- merged each such pair into whichever row was written last, so one archive's
-- slug and story_count overwrote the other's and the loser was never crawled.
--
-- The crossover walk then made it worse. backfillCrossoverPair had no way to
-- look a fandom up, so it invented one from the archive URL --
-- `Fandom(name=slugA.replace("-", " "))`. "Star-Wars-The-Clone-Wars" becomes
-- "Star Wars The Clone Wars", which is not the directory's "Star Wars: The
-- Clone Wars", so the upsert inserted a second row with no section and no
-- story count. 4,509 of those accumulated, and 166,463 story_fandoms rows
-- point at them instead of at the real fandom.
--
-- Where the invented name happened to match an existing one exactly, the
-- COALESCE clauses wrote the crossover URL's category id onto that real
-- fandom. Thirteen fandoms carry a category id that belongs to a different
-- archive because of it.
--
-- Identity here is now what FFN itself uses:
--   * (section_slug, fandom_slug) -- the archive URL, for anything found in a
--     section directory;
--   * category_id -- the numeric id in crossover URLs, unique where known;
--   * name, only for a sighting that carries no slug at all (a listing row
--     naming a fandom we have not yet walked).

BEGIN;

-- 1. Undo the category ids written by the de-slugging bug ------------------
--
-- CROSSOVER_PAIR_PATTERN split the archive URL on "-and-" greedily, so a
-- fandom whose own name contains "and" was cut in the wrong place:
-- /Batman-and-Tom-and-Jerry-Crossovers/50/5618/ parsed as
-- ("Batman-and-Tom", "Jerry"). The tail of such a split is a real fandom name
-- often enough -- Son, Kids, Monster, Evil, Hero, Ash, Sanctuary, Bone, Glory
-- -- and each of those rows was handed its sibling's category id.
UPDATE fandoms f SET category_id = NULL
WHERE f.section_slug IS NOT NULL
  AND f.category_id IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM fandoms g
      WHERE g.category_id = f.category_id
        AND g.id <> f.id
        AND g.fandom_slug LIKE '%-and-' || f.fandom_slug
  );

-- The remainder are genuine same-title-different-section pairs (Blood, Blame,
-- Sabrina the Teenage Witch, Mirror Mirror) where one of the two owns the id
-- and nothing on hand says which. Clearing both is the honest state: a later
-- sweep of /crossovers/<section>/ assigns them from the directory, which is
-- where a category id is supposed to come from in the first place.
UPDATE fandoms f SET category_id = NULL
WHERE f.section_slug IS NOT NULL
  AND f.category_id IS NOT NULL
  AND (SELECT count(*) FROM fandoms g
       WHERE g.category_id = f.category_id AND g.section_slug IS NOT NULL) > 1;

-- 2. Move the crossover stories onto their real fandoms --------------------
--
-- An invented row and the real archive share a category id, which is what
-- makes this recoverable without re-crawling: the stories were ingested
-- correctly, only their parent attribution points at a row that should not
-- exist.
INSERT INTO story_fandoms (story_id, fandom_id)
SELECT sf.story_id, real.id
FROM story_fandoms sf
JOIN fandoms orphan ON orphan.id = sf.fandom_id AND orphan.section_slug IS NULL
JOIN fandoms real   ON real.category_id = orphan.category_id
                   AND real.section_slug IS NOT NULL
ON CONFLICT DO NOTHING;

DELETE FROM story_fandoms sf
USING fandoms orphan
WHERE orphan.id = sf.fandom_id
  AND orphan.section_slug IS NULL
  AND EXISTS (SELECT 1 FROM fandoms real
              WHERE real.category_id = orphan.category_id
                AND real.section_slug IS NOT NULL);

DELETE FROM fandoms orphan
WHERE orphan.section_slug IS NULL
  AND EXISTS (SELECT 1 FROM fandoms real
              WHERE real.category_id = orphan.category_id
                AND real.section_slug IS NOT NULL);

-- 3. Collapse the invented rows that have no real archive to move to -------
--
-- 161 rows over 65 category ids, and they are the name-collision losers: FFN
-- has an archive at that id, but the only row we ever had for it was eaten by
-- `ON CONFLICT (name)`. Several invented rows can share one id, because the
-- greedy split produced a different mangled prefix per partner ("Cinderella
-- and Snow White", "Cinderella and Romeo", "Cinderella and Princess" are all
-- category 3715). Keep one row per id, move the story links onto it, and stop
-- the name and slug asserting a title we never read.
CREATE TEMP TABLE orphan_survivor AS
SELECT category_id, min(id) AS keep_id
FROM fandoms WHERE section_slug IS NULL
GROUP BY category_id;

INSERT INTO story_fandoms (story_id, fandom_id)
SELECT sf.story_id, s.keep_id
FROM story_fandoms sf
JOIN fandoms orphan ON orphan.id = sf.fandom_id AND orphan.section_slug IS NULL
JOIN orphan_survivor s ON s.category_id = orphan.category_id
ON CONFLICT DO NOTHING;

DELETE FROM story_fandoms sf
USING fandoms orphan, orphan_survivor s
WHERE orphan.id = sf.fandom_id
  AND orphan.section_slug IS NULL
  AND s.category_id = orphan.category_id
  AND orphan.id <> s.keep_id;

DELETE FROM fandoms orphan
USING orphan_survivor s
WHERE orphan.section_slug IS NULL
  AND s.category_id = orphan.category_id
  AND orphan.id <> s.keep_id;

UPDATE fandoms SET
    name = 'FFN category ' || category_id,
    fandom_slug = NULL
WHERE section_slug IS NULL;

-- 4. Repair the mangled slugs stored against each pair ---------------------
--
-- The pair's numeric ids were always right, and FFN routes a crossover
-- archive on those ids and ignores the slug text, which is why 406,457
-- stories came back despite 5,024 pairs holding a wrong slug_a. The slugs are
-- still what the URL is built from, so take them from the fandom the id names.
UPDATE crossover_pairs p SET slug_a = f.fandom_slug
FROM fandoms f
WHERE f.category_id = p.fandom_id_a
  AND f.fandom_slug IS NOT NULL
  AND f.fandom_slug <> p.slug_a;

UPDATE crossover_pairs p SET slug_b = f.fandom_slug
FROM fandoms f
WHERE f.category_id = p.fandom_id_b
  AND f.fandom_slug IS NOT NULL
  AND f.fandom_slug <> p.slug_b;

-- 5. Replace the constraint ------------------------------------------------
ALTER TABLE fandoms DROP CONSTRAINT IF EXISTS fandoms_name_key;

-- The archive URL. Every fandom read from a section directory has both parts.
CREATE UNIQUE INDEX IF NOT EXISTS fandoms_section_slug_key
    ON fandoms (section_slug, fandom_slug)
    WHERE fandom_slug IS NOT NULL;

-- The id crossover URLs are built from. Unique once the de-slugging damage
-- above is undone, and the join key the crossover walk should have been using.
CREATE UNIQUE INDEX IF NOT EXISTS fandoms_category_id_key
    ON fandoms (category_id)
    WHERE category_id IS NOT NULL;

-- A fandom named by a listing row we have not walked yet has no slug, so name
-- is all there is to deduplicate it on. Scoped to those rows only, so it can
-- never again merge two archives that share a title.
CREATE UNIQUE INDEX IF NOT EXISTS fandoms_name_unslugged_key
    ON fandoms (name)
    WHERE fandom_slug IS NULL;

COMMIT;

-- 6. Pair slugs are a cache, not an identity -------------------------------
--
-- They can be NULL for a fandom no directory entry has been read for yet, and
-- iterCrossoverPairs prefers fandoms.fandom_slug over whatever is stored here.
BEGIN;
ALTER TABLE crossover_pairs ALTER COLUMN slug_a DROP NOT NULL;
ALTER TABLE crossover_pairs ALTER COLUMN slug_b DROP NOT NULL;
COMMIT;
