#!/usr/bin/env bash
# Run the API against the local Postgres for development.
#
#   ./scripts/dev.sh serve   -- start uvicorn with reload on :8099
#   ./scripts/dev.sh psql    -- open a shell on the corpus
#   ./scripts/dev.sh crawl … -- run the crawler with the right DSN
#
# Reads credentials from .env so they never end up in a shell history.
set -euo pipefail

projectRoot="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$projectRoot"

if [[ ! -f .env ]]; then
    echo "no .env -- copy .env.example and fill it in" >&2
    exit 1
fi
set -a; source .env; set +a

# Host and port are overridable because the corpus is not always reached the
# same way: the compose stack publishes Postgres on 127.0.0.1:5433, while a
# host running it natively uses the stock 5432. Defaults keep the compose
# workflow unchanged.
export LODESTONE_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST:-localhost}:${POSTGRES_PORT:-5433}/${POSTGRES_DB}"
pythonBin="$projectRoot/.venv/bin/python"

case "${1:-serve}" in
    serve)
        # --reload makes WatchFiles walk the source tree, which lives on a
        # Windows drive mounted into WSL. Under load that is slow enough for the
        # reloader to start, log "Uvicorn running", and never spawn a worker --
        # so the server looks healthy while every request times out. Opt in
        # while editing; the default stays reliable.
        cd api
        reloadFlag=()
        [[ "${LODESTONE_RELOAD:-0}" == "1" ]] && reloadFlag=(--reload)
        exec "$projectRoot/.venv/bin/uvicorn" lodestone_api.main:app \
            --host 127.0.0.1 --port "${LODESTONE_PORT:-8099}" "${reloadFlag[@]}"
        ;;
    psql)
        # Native install first: where Postgres is not in a container there is
        # no lodestone-postgres-1 to exec into, and the docker form then fails
        # in a way that reads like the database is down rather than absent.
        if command -v psql >/dev/null 2>&1; then
            exec env PGPASSWORD="$POSTGRES_PASSWORD" psql \
                -h "${POSTGRES_HOST:-localhost}" -p "${POSTGRES_PORT:-5433}" \
                -U "$POSTGRES_USER" -d "$POSTGRES_DB"
        fi
        exec docker exec -it lodestone-postgres-1 \
            psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
        ;;
    crawl)
        shift
        cd crawler
        exec "$pythonBin" -m lodestone_crawler "$@"
        ;;
    *)
        echo "usage: $0 {serve|psql|crawl …}" >&2
        exit 1
        ;;
esac
