-- Crossover archives.
--
-- Crossover stories appear in NO parent fandom's own archive -- the Good Omens
-- pilot returned zero of them -- so they are only reachable through the
-- A-x-B archive at /<A>-and-<B>-Crossovers/<idA>/<idB>/. Enumerating those
-- pairs is therefore required for coverage, not an enhancement.
--
-- FFN orders the pair URL by ascending category id, which gives a canonical
-- key: the same pair is never reachable under two different URLs.

CREATE TABLE IF NOT EXISTS crossover_pairs (
    fandom_id_a   INTEGER NOT NULL,   -- always the lower category id
    fandom_id_b   INTEGER NOT NULL,
    slug_a        TEXT NOT NULL,
    slug_b        TEXT NOT NULL,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fandom_id_a, fandom_id_b),
    CONSTRAINT crossover_pair_is_canonically_ordered CHECK (fandom_id_a < fandom_id_b)
);

CREATE INDEX IF NOT EXISTS crossover_pairs_b_idx ON crossover_pairs (fandom_id_b);
