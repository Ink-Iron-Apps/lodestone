# Lodestone

A search engine for FanFiction.Net that does the things FFN's own search cannot.

FanFiction.Net hosts something on the order of fourteen million stories and
gives you a search box that matches titles and summaries, sorts three ways, and
offers no exclusions, no boolean operators, no completion filter, no way to
search across fandoms, and no way to tell a story that finished from one
abandoned in 2013. Every piece of metadata needed to do better is already on the
page. Lodestone collects it, normalizes it, and puts a real query engine in
front of it.

**Status: early.** The crawler and parser are built and tested against live
pages. The API and web front end are not written yet.

## What it makes possible

Queries FanFiction.Net cannot express at all:

- **Cross-fandom and crossover search as one namespace.** Crossover stories live
  in neither parent fandom's archive on FFN, which is why they are effectively
  invisible there.
- **Exclusions** — not this character, not this genre, not this author, not
  incomplete.
- **Relationships as first-class objects.** FFN's `[A, B]` bracket syntax is the
  only relationship signal on the entire site. Parsed out, it becomes a real
  facet.
- **Length-adjusted popularity.** Sorting by raw favourites structurally buries
  every good short work beneath every long mediocre one. Favourites per thousand
  words does not.
- **Abandonment detection** — in progress, but untouched for years. This is the
  single most common complaint about FFN's browse, and the data to answer it has
  always been right there in the listing row.
- **Semantic search over summaries**, so that "competent time-travel Harry who
  isn't a Gary Stu" is an expressible query rather than a forum post.

## How it works

FFN exposes no API and no structured data. Every fact about a story is encoded
in one human-readable string:

```
Rated: T - English - Hurt/Comfort/Adventure - Chapters: 50 - Words: 449,697
 - Reviews: 496 - Favs: 727 - Follows: 894 - Updated: ... - Published: ...
 - [Harry P., Artemis] Ron W. - Complete
```

The important discovery is that **listing pages carry the same complete metadata
line as the story page itself**, at 25 to 100 rows per request. That is what
makes indexing the whole corpus affordable: roughly 480,000 requests rather than
the 14.6 million a story-by-story crawl would need.

```
                      ┌──────────────────────────┐
  fanfiction.net ───► │  crawler (residential)   │
                      │  curl_cffi, 5s delay     │
                      └────────────┬─────────────┘
                                   │ StoryRecord
                      ┌────────────▼─────────────┐
                      │  Postgres + pgvector     │  system of record
                      └────────────┬─────────────┘
                                   │ projection
                      ┌────────────▼─────────────┐
                      │  Meilisearch             │  facets + typo tolerance
                      └────────────┬─────────────┘
                                   │
                      ┌────────────▼─────────────┐
                      │  API  →  web front end   │  not yet built
                      └──────────────────────────┘
```

## Crawling responsibly

FanFiction.Net's `robots.txt` declares:

```
User-agent: *
Content-Signal: search=yes, ai-train=no, use=reference
Allow: /
crawl-delay: 5
```

Building a search index is the one use it explicitly grants. Lodestone is built
to stay inside that grant, and the constraints are structural rather than
advisory:

- The 5-second crawl delay is enforced in the fetcher and **cannot be configured
  below the declared value** — it raises instead.
- Only metadata and summaries are stored. Story text is never collected, which
  is why `use=reference` covers what we do.
- Nothing is used to train anything. `ai-train=no` is respected.
- Results link back to fanfiction.net. Lodestone sends readers to FFN; it is not
  a mirror and does not host stories.
- The crawler identifies itself and offers a contact route.

## Running it

```bash
cp .env.example .env      # then edit it
docker compose up -d postgres meilisearch

cd crawler
python -m venv .venv && .venv/bin/pip install -e '.[dev]'

# No database needed: proves the egress works and prints parsed rows.
.venv/bin/python -m lodestone_crawler probe

# Enumerate every fandom, then walk one archive.
.venv/bin/python -m lodestone_crawler discover
.venv/bin/python -m lodestone_crawler backfill --section book --fandom Harry-Potter --max-pages 20
```

### The crawler must run from a residential connection

FFN sits behind Cloudflare, which blocks on the TLS fingerprint rather than the
User-Agent — a stock HTTP client gets a flat 403 no matter what headers it
sends. `curl_cffi` replays a real browser fingerprint, which is enough, and no
browser engine is required. Datacenter IP ranges are blocked separately, so the
crawler will not work from a VPS however polite it is. The query layer has no
such constraint and can be hosted anywhere.

## Tests

```bash
cd crawler && .venv/bin/python -m pytest
```

Two layers: hand-written grammar cases pinning the awkward parts (the slash
inside `Hurt/Comfort`, ship brackets, fandom names containing the field
separator), and shape invariants run against snapshots of real pages. The
snapshots are verbatim FFN HTML, so they are not committed; generate them with
`python scripts/save_fixtures.py` and the invariant tests light up.

## Licence

AGPL-3.0. If you run a modified copy as a service, publish the modifications.
