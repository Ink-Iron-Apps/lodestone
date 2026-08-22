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

export LODESTONE_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@localhost:5433/${POSTGRES_DB}"
pythonBin="$projectRoot/.venv/bin/python"

case "${1:-serve}" in
    serve)
        cd api
        exec "$projectRoot/.venv/bin/uvicorn" lodestone_api.main:app \
            --host 127.0.0.1 --port "${LODESTONE_PORT:-8099}" --reload
        ;;
    psql)
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
