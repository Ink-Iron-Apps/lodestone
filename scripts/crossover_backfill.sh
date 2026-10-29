#!/usr/bin/env bash
# Discover every A-x-B crossover archive, then walk them all.
#
# Two phases in one script because the second has nothing to do until the first
# finishes, and both are long enough to want a service around them.
#
# Phase 1 (discover-crossovers) sweeps 9 crossover directories and then one
# partner page per fandom that has crossovers -- about 8,800 requests, so
# roughly 12 hours at the robots.txt crawl-delay. It is idempotent but NOT
# resumable: every pair is inserted ON CONFLICT DO NOTHING, so a restart costs
# the requests again rather than duplicating rows. The marker file below is what
# keeps a service restart from paying that twice.
#
# Phase 2 (backfill-crossovers) walks each pair's archive, resuming from
# crawl_state exactly like the fandom backfill, so it is safe to interrupt at
# any point. Measured estimate: ~51,319 pairs averaging 3.6 stories, about 3.5
# days.
#
# This must not run alongside lodestone-backfill. The 5s crawl-delay is enforced
# per fetcher instance, so two crawler processes put FFN under 2.5s between
# requests and break the one grant robots.txt actually gives us.
set -uo pipefail

cd /home/riley/coding/lodestone

MARKER=/var/lib/lodestone/crossovers-discovered

if [[ -f "$MARKER" ]]; then
    echo "$(date -Is) discovery already done ($(cat "$MARKER")), skipping to backfill"
else
    echo "$(date -Is) phase 1: discovering crossover pairs"
    if ./scripts/dev.sh crawl discover-crossovers; then
        mkdir -p "$(dirname "$MARKER")"
        date -Is > "$MARKER"
        echo "$(date -Is) phase 1 complete"
    else
        echo "$(date -Is) phase 1 failed; not marking complete" >&2
        exit 1
    fi
fi

echo "$(date -Is) phase 2: walking crossover archives"
exec ./scripts/dev.sh crawl backfill-crossovers
