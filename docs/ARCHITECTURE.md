# Lodestone arch

Internal notes. Public prose → README.

## Core insight

Listing rows carry **same** meta line as story page. 25-100 rows/req vs 1.
→ full corpus ~480K reqs, not 14.6M. Everything else follows this.

## Surfaces

| Surface | Path | Rows | Deep page | Use |
|---|---|---|---|---|
| Just In | `/j/0/0/0/` | 100 | **no** | incremental firehose, poll hourly |
| Fandom browse | `/<sec>/<Fandom>/?srt=N&r=10&p=N` | 25 | yes (p=3000 verified) | backfill |
| Crossover | `/<A>-and-<B>-Crossovers/<idA>/<idB>/` | 25 | yes | crossovers live ONLY here |
| Search | `/search/?keywords=..&ppage=N` | 50 | yes | coverage cross-check only |

Sections: anime book cartoon comic game misc movie play tv.
Section dir `/book/` → 2,620 fandoms + abbrev counts (852K). Counts = planning only, not truth.

## Meta line grammar

```
[<Fandom> - ] Rated: <r> - <lang> [- <genres>] - Chapters: N - Words: N
[- Reviews: N] [- Favs: N] [- Follows: N] [- Updated: <ts>] - Published: <ts>
[- <chars>] [- Complete]
```

Story page differs: `Rated: Fiction T`, chars **before** Chapters, `Status: Complete`, trailing `id: N`.
→ classify tokens, never read positionally.

### Traps (all have tests)

- `Hurt/Comfort` = one genre w/ slash → `Hurt/Comfort/Adventure` naive split = garbage. Validate halves vs genre set.
- Fandom names contain ` - ` (`Hetalia - Axis Powers`) → never tokenize before extracting fandom. Split at `Rated:` idx.
- Search rows wrap match terms in `<b>` → strip tags on title + summary.
- Dates: use `data-xutime` attr (exact unix), NOT rendered text (`10h`, `8/15/2012` = lossy).
- No `Updated:` on never-updated fics → fall back to published, else recency sort drops them.
- Genre optional, chars optional, all engagement counts optional.
- `[A, B]` brackets = ship. Unbracketed = appears only. Multiple bracket groups possible.
- FFN default rating filter hides M → always `r=10` (all), else silent truncation.

## Fetch layer

Cloudflare blocks on **TLS fingerprint**, not UA. Plain curl + Chrome UA = 403. `curl_cffi impersonate='chrome'` = 200.
→ no browser engine needed.

**Datacenter ASN blocked separately** → crawler MUST egress residential. Compose uses `network_mode: host`. Query layer unconstrained.

No `charset` header → clients guess latin-1 → mojibake (`Pokémon`). Payload always UTF-8. Always `.content.decode("utf-8")`.

FFN app errors served as **HTTP 200** w/ body `FanFiction.Net Error Type` → 200 ≠ success.

403 → `BlockedError`, abort whole crawl. Retrying deepens block.

### Verified 2026-08-21

12 seq reqs @5s → 12× 200, mean 0.16s, 0 retries. All 4 surfaces OK. Deep p=3000 OK.

## Politeness = structural, not advisory

robots.txt: `Content-Signal: search=yes,ai-train=no,use=reference` + `Allow: /` + `crawl-delay: 5`.
Search index = the granted use. Basis for whole project.

- `crawlDelaySeconds < 5` → raises. Not a config knob.
- metadata + summary only, never story text (`use=reference`)
- no training (`ai-train=no`)
- link back to FFN, not a mirror
- honest self-identification

## Data model

PG = system of record. Meili = disposable projection. Rebuild-from-PG or it doesn't belong.

- `stories` PK = FFN story_id (never reassigned)
- `ships` jsonb not `TEXT[][]` — varying arity per story
- `favorites_per_1k_words` GENERATED STORED — FFN sorts raw favs → buries short good work
- `story_fandoms` M2M — crossover = exactly 2
- `crawl_state` — backfill is days wall-clock, WILL be interrupted, resume mandatory

### Deletion

FFN deletes silently, zero tombstone. Only signal = `last_seen_at` going stale.
→ `deleted_at` soft + reversible (re-seeing resurrects; FFN restores often enough).

### Embeddings

`summary_embedding vector(384)`. Nulled on upsert only when summary text changed → no needless recompute.
HNSW index built **after** backfill (bulk build ≫ incremental). Commented out in schema until then.

## Crawl ordering

**Backfill = sort by PUBLISH date (srt=2).** Publish order immutable → pages don't reshuffle mid-walk across days.
Update-date sort here = rows migrate between pages = silent skips.

**Refresh = sort by UPDATE date (srt=1)**, head N pages. Churn is the point.
Backfill alone → index correct once, wrong forever.

## Pilot: Good Omens (2026-08-21)

`/book/Good-Omens/` backfill → exhaustion. 72 reqs, **0 retries, 0 blocks**, 7min wall (342s of it deliberate crawl-delay).

- 1,764 stories, 845 authors, 1,194 complete, 152 w/ ships, 8 langs, published 2001-04-15 → 2026-04-28
- resume re-run → 0 reqs (`already exhausted`) ✓
- fandom discovery: 13,115 fandoms in 9 reqs / 55s
- **crossovers = 0** → empirically confirms crossovers live ONLY in crossover archives, absent from parent fandom archive. Crossover crawl is NOT optional.
- ratings incl **M** → confirms `r=10` needed; FFN default would've silently dropped them
- 8 langs parsed positionally, all correct → no lang whitelist needed

### QA: 0 anomalies / 1764 rows

null rating/lang/words/published = 0 · updated<published = 0 · bogus genre = 0 · >2 genres = 0 · char-that-is-really-a-count = 0 · empty title = 0 · markup leaked into summary = 0.

### Sizing (measured, not estimated)

704 kB / 1,764 rows = **~409 B/row** → 12M rows ≈ **4.6 GB** table. Matches the affordability premise.

### Bug found + fixed

`crawl_state.rows_ingested` was 2× true count. `recordCrawlProgress` accumulates (`rows_ingested + EXCLUDED`), and terminal calls (exhausted/error) re-passed the whole run total on top of the per-page calls. → terminal calls now pass `NO_NEW_ROWS`.

## Open items

- `/crossovers/<Fandom>/<catId>/` (partner list) → FFN `Error Type 1` from curl_cffi, works in browser. Tried: warm session, referer, full browser hdrs, no-slash. Unresolved. Workaround: `/crossovers/<section>/` dir works, crossover archives work → discover pairs from crossover rows.
- Char IDs are global ints (`177975=Daisuke K.`) in filter form → harvest for canonical char entity resolution. Not wired yet.
- Story page fields listings lack: chapter titles, cover art. Only fetch when needed, never bulk.
- API + web: not written.
- Corpus size: max story_id 14,584,841 @ 2026-08-21. Live count unknown (deletions).
