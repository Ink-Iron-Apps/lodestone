"""Command line entry point.

    python -m lodestone_crawler discover
    python -m lodestone_crawler backfill --section book --fandom Harry-Potter --max-pages 10
    python -m lodestone_crawler justin
    python -m lodestone_crawler probe
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .fetcher import BlockedError, FanFictionFetcher
from .models import Fandom
from .pipeline import backfillFandom, discoverFandoms, pollJustIn, refreshFandom
from .storage import StoryStore
from .surfaces import justInUrl
from .parser import parseListingPage

DEFAULT_DSN = "postgresql://lodestone:lodestone@localhost:5432/lodestone"


def buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lodestone-crawler")
    parser.add_argument("--dsn", default=os.environ.get("LODESTONE_DSN", DEFAULT_DSN))
    parser.add_argument("--crawl-delay", type=float, default=5.0,
                        help="seconds between requests; may not go below the robots.txt value of 5")
    parser.add_argument("-v", "--verbose", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("discover", help="enumerate every fandom in every section directory")
    subparsers.add_parser("justin", help="ingest the newest 100 stories site-wide")
    subparsers.add_parser("probe", help="fetch one page and print parsed rows; touches no database")

    backfillParser = subparsers.add_parser("backfill", help="walk a fandom archive to exhaustion")
    backfillParser.add_argument("--section", required=True)
    backfillParser.add_argument("--fandom", required=True, help="URL slug, e.g. Harry-Potter")
    backfillParser.add_argument("--name", help="display name; defaults to the slug with dashes replaced")
    backfillParser.add_argument("--max-pages", type=int, default=None)

    refreshParser = subparsers.add_parser("refresh", help="re-crawl a fandom's recently-updated head")
    refreshParser.add_argument("--section", required=True)
    refreshParser.add_argument("--fandom", required=True)
    refreshParser.add_argument("--name")
    refreshParser.add_argument("--head-pages", type=int, default=20)

    return parser


def runProbe(fetcher: FanFictionFetcher) -> int:
    """Smoke test with no database: prove the egress works and the parser agrees."""
    records = parseListingPage(fetcher.fetch(justInUrl()))
    print(f"parsed {len(records)} rows from the Just In firehose\n")
    for record in records[:5]:
        fandomNames = " + ".join(fandom.name for fandom in record.fandoms)
        shipText = "; ".join("/".join(ship) for ship in record.ships) or "-"
        print(f"  [{record.storyId}] {record.title}  by {record.authorName}")
        print(f"      {fandomNames} | {record.rating} | {record.language} | "
              f"{'/'.join(record.genres) or 'no genre'}")
        print(f"      {record.wordCount:,}w in {record.chapterCount}ch | "
              f"{record.status} | ships: {shipText}")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = buildParser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    fetcher = FanFictionFetcher(crawlDelaySeconds=arguments.crawl_delay)

    if arguments.command == "probe":
        return runProbe(fetcher)

    try:
        with StoryStore(arguments.dsn) as store:
            if arguments.command == "discover":
                print(f"discovered {discoverFandoms(fetcher, store)} fandoms")

            elif arguments.command == "justin":
                outcome = pollJustIn(fetcher, store)
                print(f"ingested {outcome.rowsIngested} rows")

            elif arguments.command in ("backfill", "refresh"):
                fandom = Fandom(
                    name=arguments.name or arguments.fandom.replace("-", " "),
                    sectionSlug=arguments.section,
                    fandomSlug=arguments.fandom,
                )
                if arguments.command == "backfill":
                    outcome = backfillFandom(fetcher, store, fandom, maxPages=arguments.max_pages)
                else:
                    outcome = refreshFandom(fetcher, store, fandom, headPages=arguments.head_pages)
                print(f"{outcome.surfaceKey}: {outcome.pagesFetched} pages, "
                      f"{outcome.rowsIngested} rows, exhausted={outcome.isExhausted} "
                      f"({outcome.stoppedReason})")

    except BlockedError as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        print("Stop crawling from this egress and investigate before retrying.", file=sys.stderr)
        return 2

    stats = fetcher.stats
    logging.info("requests=%d retries=%d waited=%.0fs",
                 stats.requestCount, stats.retryCount, stats.totalWaitSeconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
