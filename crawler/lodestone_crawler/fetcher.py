"""Polite HTTP client for FanFiction.Net.

Two non-obvious things this exists to encapsulate:

1. **TLS fingerprinting.** FFN sits behind Cloudflare, which blocks on the TLS
   and HTTP/2 fingerprint, not the User-Agent. A stock `requests`/`httpx` client
   gets a flat 403 no matter what headers it sends. `curl_cffi` replays a real
   Chrome fingerprint, which is enough -- no browser engine required.

2. **Encoding.** FFN sends no `charset` in its response headers, so any client
   that trusts the header falls back to latin-1 and silently mangles every
   non-ASCII byte (`Pokémon` -> `PokÃ©mon`). The payload is always UTF-8.

Politeness is not optional here. FFN's robots.txt grants `search=yes` and asks
for `crawl-delay: 5`; honouring both is what makes this project legitimate.
"""
from __future__ import annotations

import dataclasses
import logging
import random
import time
from typing import Optional

from curl_cffi import requests

logger = logging.getLogger(__name__)

FFN_ORIGIN = "https://www.fanfiction.net"

# Declared crawl-delay in https://www.fanfiction.net/robots.txt. Do not lower it.
ROBOTS_CRAWL_DELAY_SECONDS = 5.0

# FFN renders application faults as a styled page with HTTP 200, so a 200 alone
# is not proof of success.
SERVER_ERROR_MARKER = "FanFiction.Net Error Type"

# Identify honestly and give operators a way to reach us. robots.txt permits
# search indexing; hiding would undercut the one basis we have for being here.
USER_AGENT_SUFFIX = "Lodestone/0.1 (+https://github.com/Ink-Iron-Apps/lodestone)"


class FetchError(RuntimeError):
    """Raised when a page could not be retrieved after exhausting retries."""


class BlockedError(FetchError):
    """Raised on a 403, which means Cloudflare stopped trusting this egress."""


@dataclasses.dataclass(slots=True)
class FetchStats:
    requestCount: int = 0
    retryCount: int = 0
    blockedCount: int = 0
    totalWaitSeconds: float = 0.0


class FanFictionFetcher:
    """Serial, rate-limited fetcher. One instance == one crawl worker.

    Deliberately synchronous and single-flight: at a 5s crawl-delay there is no
    throughput to win from concurrency within a worker, and a serial loop makes
    the politeness guarantee trivially auditable.
    """

    def __init__(
        self,
        impersonate: str = "chrome",
        crawlDelaySeconds: float = ROBOTS_CRAWL_DELAY_SECONDS,
        maxRetries: int = 4,
        jitterSeconds: float = 1.0,
    ) -> None:
        if crawlDelaySeconds < ROBOTS_CRAWL_DELAY_SECONDS:
            raise ValueError(
                f"crawlDelaySeconds={crawlDelaySeconds} undercuts the robots.txt "
                f"crawl-delay of {ROBOTS_CRAWL_DELAY_SECONDS}"
            )
        self.crawlDelaySeconds = crawlDelaySeconds
        self.maxRetries = maxRetries
        self.jitterSeconds = jitterSeconds
        self.stats = FetchStats()
        self._lastRequestAt: Optional[float] = None
        self._session = requests.Session(impersonate=impersonate)
        self._session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Crawler": USER_AGENT_SUFFIX,
        })

    def _waitForTurn(self) -> None:
        if self._lastRequestAt is None:
            return
        elapsed = time.monotonic() - self._lastRequestAt
        # Jitter keeps a fleet of workers from marching in lockstep.
        target = self.crawlDelaySeconds + random.uniform(0, self.jitterSeconds)
        if elapsed < target:
            waitSeconds = target - elapsed
            self.stats.totalWaitSeconds += waitSeconds
            time.sleep(waitSeconds)

    def fetch(self, url: str) -> str:
        """Fetch one page, returning correctly decoded HTML.

        Raises BlockedError on a 403 so the caller can stop the whole crawl
        rather than hammering a door that has been shut.
        """
        lastError: Optional[Exception] = None

        for attempt in range(self.maxRetries):
            self._waitForTurn()
            self._lastRequestAt = time.monotonic()
            self.stats.requestCount += 1

            try:
                response = self._session.get(url, timeout=30)
            except Exception as error:
                lastError = error
                logger.warning("fetch %s attempt %d transport error: %s", url, attempt + 1, error)
                self.stats.retryCount += 1
                time.sleep(2 ** attempt)
                continue

            if response.status_code == 403:
                self.stats.blockedCount += 1
                raise BlockedError(f"403 from {url} -- Cloudflare has stopped trusting this egress")

            if response.status_code == 429:
                retryAfter = float(response.headers.get("Retry-After", 60))
                logger.warning("429 from %s, honouring Retry-After=%ss", url, retryAfter)
                self.stats.retryCount += 1
                time.sleep(retryAfter)
                continue

            if response.status_code >= 500:
                lastError = FetchError(f"{response.status_code} from {url}")
                self.stats.retryCount += 1
                time.sleep(2 ** attempt)
                continue

            html = response.content.decode("utf-8", errors="replace")

            if SERVER_ERROR_MARKER in html:
                # FFN's own application error, served as 200. Transient often
                # enough to be worth a retry, permanent for some URL shapes.
                lastError = FetchError(f"FFN application error for {url}")
                self.stats.retryCount += 1
                time.sleep(2 ** attempt)
                continue

            return html

        raise FetchError(f"gave up on {url} after {self.maxRetries} attempts") from lastError
