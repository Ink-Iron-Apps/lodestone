#!/usr/bin/env bash
# One-line backfill status, safe to call from a monitor or by hand.
set -uo pipefail

status=$(systemctl is-active lodestone-backfill 2>/dev/null || echo unknown)
restarts=$(systemctl show lodestone-backfill -p NRestarts --value 2>/dev/null || echo '?')
# grep -c prints 0 and exits 1 when nothing matches, so a `|| echo 0` fallback
# appends a second line and breaks the arithmetic test below.
problems=$(grep -ciE 'blocked|traceback' /var/log/lodestone-backfill.log 2>/dev/null | head -1)
problems=${problems:-0}

stats=$(docker exec lodestone-postgres-1 psql -U lodestone -d lodestone -Atc "
SELECT (SELECT count(*) FROM stories WHERE deleted_at IS NULL)
    || ' stories, ' || (SELECT count(*) FROM stories WHERE summary_embedding IS NOT NULL)
    || ' embedded, ' || (SELECT count(*) FROM crawl_state WHERE surface_key LIKE 'browse:%' AND is_exhausted)
    || '/' || (SELECT count(*) FROM fandoms WHERE fandom_slug IS NOT NULL)
    || ' fandoms, ' || (SELECT pg_size_pretty(pg_database_size('lodestone')))
" 2>/dev/null | tr -d '\n')

if [[ "$status" != "active" ]]; then
    echo "PROBLEM: service is $status (restarts=$restarts)"
elif [[ "${problems:-0}" -gt 0 ]]; then
    echo "PROBLEM: $problems blocked/error lines | $stats"
elif [[ -z "$stats" ]]; then
    echo "PROBLEM: cannot reach postgres (service still $status)"
else
    echo "ok | $stats | restarts=$restarts"
fi
