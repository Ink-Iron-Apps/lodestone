#!/usr/bin/env bash
# One-line backfill status, safe to call from a monitor or by hand.
set -uo pipefail

status=$(systemctl is-active lodestone-backfill 2>/dev/null || echo unknown)
restarts=$(systemctl show lodestone-backfill -p NRestarts --value 2>/dev/null || echo '?')
# Only look at recent history. Counting over the whole log means one transient
# failure -- a Postgres crash-recovery reconnect, say -- flags PROBLEM forever
# after, which trains you to ignore the alert.
#
# grep -c also prints 0 and exits 1 when nothing matches, so a `|| echo 0`
# fallback appends a second line and breaks the arithmetic test below.
# Match the exact strings the crawler emits when Cloudflare shuts the door.
# A bare '403' is useless here: it matches millisecond timestamps (10:22:25,403)
# and story counts (4032), so it fires constantly on a perfectly healthy log.
problems=$(tail -500 /var/log/lodestone-backfill.log 2>/dev/null     | grep -ciE 'BLOCKED:|BlockedError|stopped trusting this egress' | head -1)
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
    echo "PROBLEM: $problems Cloudflare block lines in recent log | $stats"
elif [[ -z "$stats" ]]; then
    echo "PROBLEM: cannot reach postgres (service still $status)"
else
    echo "ok | $stats | restarts=$restarts"
fi
