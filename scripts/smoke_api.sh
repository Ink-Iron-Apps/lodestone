#!/usr/bin/env bash
# End-to-end check that each headline capability actually answers.
set -uo pipefail
base="${1:-http://127.0.0.1:8099}"

probe() {
    local label="$1" query="$2"
    local total
    total=$(curl -s "$base/api/search?$query&pageSize=1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["total"])' 2>/dev/null)
    printf '  %-46s %8s\n' "$label" "${total:-ERR}"
}

echo "== corpus =="
curl -s "$base/api/stats" | python3 -m json.tool

echo
echo "== capability probes (result counts) =="
probe "everything"                              ""
probe "full-text: 'apocalypse'"                 "q=apocalypse"
probe "author name search (FFN cannot)"         "q=Moczo"
probe "complete only"                           "status=complete"
probe "abandoned only"                          "onlyAbandoned=true"
probe "hide abandoned"                          "excludeAbandoned=true"
probe "genre Humor"                             "genre=Humor"
probe "genre Humor, NOT Romance"                "genre=Humor&excludeGenre=Romance"
probe "character Aziraphale"                    "character=Aziraphale"
probe "Aziraphale, NOT OC"                      "character=Aziraphale&excludeCharacter=OC"
probe "ship Crowley/Aziraphale (paired)"        "ship=A.+Crowley&ship=Aziraphale"
probe "10K+ words, complete, 50+ favs"          "minWords=10000&status=complete&minFavorites=50"
probe "non-English"                             "language=French"

echo
echo "== sort: favourites per 1K words (top 3) =="
curl -s "$base/api/search?sort=favorites_per_1k&minFavorites=20&pageSize=3" | python3 "$(dirname "$0")/_format_results.py" ratio

echo
echo "== sort: raw favourites, same filter (what FFN would show) =="
curl -s "$base/api/search?sort=favorites&minFavorites=20&pageSize=3" | python3 "$(dirname "$0")/_format_results.py" favorites
