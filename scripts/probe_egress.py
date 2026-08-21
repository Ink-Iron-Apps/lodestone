"""Crawl-viability probe: can we reach fanfiction.net from this egress at rate?

Gating experiment for Lodestone. Cloudflare blocks on TLS fingerprint, not just
User-Agent, so plain curl/requests get a flat 403. curl_cffi impersonates a real
browser's TLS + HTTP2 fingerprint without paying for a browser engine.
"""
import sys
import time

from curl_cffi import requests

PROBE_URLS = {
    "just_in": "https://www.fanfiction.net/j/0/0/0/",
    "fandom_browse": "https://www.fanfiction.net/book/Harry-Potter/?&srt=1&r=10&p=2",
    "deep_page": "https://www.fanfiction.net/book/Harry-Potter/?&srt=1&r=10&p=3000",
    "story_page": "https://www.fanfiction.net/s/13571544/1/",
}

IMPERSONATION_TARGETS = ["chrome", "chrome131", "chrome124", "firefox135", "safari184"]


def probeImpersonationTargets():
    print("=== phase 1: which TLS impersonation profile gets through? ===")
    workingTargets = []
    for target in IMPERSONATION_TARGETS:
        try:
            response = requests.get(PROBE_URLS["just_in"], impersonate=target, timeout=30)
            hasRows = "z-list" in response.text
            print(f"  {target:12} -> {response.status_code}  {len(response.text):>7}b  rows={hasRows}")
            if response.status_code == 200 and hasRows:
                workingTargets.append(target)
        except Exception as error:
            print(f"  {target:12} -> ERR {type(error).__name__}: {str(error)[:70]}")
    return workingTargets


def probeSurfaces(impersonationTarget):
    print(f"\n=== phase 2: all crawl surfaces via {impersonationTarget} ===")
    session = requests.Session(impersonate=impersonationTarget)
    for surfaceName, url in PROBE_URLS.items():
        try:
            response = session.get(url, timeout=30)
            rowCount = response.text.count('class="z-list')
            print(f"  {surfaceName:15} -> {response.status_code}  rows={rowCount}")
        except Exception as error:
            print(f"  {surfaceName:15} -> ERR {type(error).__name__}: {str(error)[:70]}")
        time.sleep(5)


def probeSustainedRate(impersonationTarget, requestCount=12, delaySeconds=5):
    """The question that actually matters: does it survive N sequential requests
    at crawl-delay, or does Cloudflare escalate after the first few?"""
    print(f"\n=== phase 3: {requestCount} sequential requests @ {delaySeconds}s ===")
    session = requests.Session(impersonate=impersonationTarget)
    statusCounts = {}
    latencies = []
    for pageNumber in range(2, 2 + requestCount):
        url = f"https://www.fanfiction.net/book/Harry-Potter/?&srt=1&r=10&p={pageNumber}"
        startedAt = time.time()
        try:
            response = session.get(url, timeout=30)
            elapsedSeconds = time.time() - startedAt
            latencies.append(elapsedSeconds)
            rowCount = response.text.count('class="z-list')
            statusCounts[response.status_code] = statusCounts.get(response.status_code, 0) + 1
            print(f"  p={pageNumber:<4} {response.status_code}  rows={rowCount:<3} {elapsedSeconds:.2f}s")
        except Exception as error:
            statusCounts["ERR"] = statusCounts.get("ERR", 0) + 1
            print(f"  p={pageNumber:<4} ERR {type(error).__name__}: {str(error)[:60]}")
        time.sleep(delaySeconds)

    print(f"\n  status distribution: {statusCounts}")
    if latencies:
        print(f"  mean latency: {sum(latencies) / len(latencies):.2f}s")
    return statusCounts


if __name__ == "__main__":
    workingTargets = probeImpersonationTargets()
    if not workingTargets:
        print("\nVERDICT: no impersonation profile got through. Needs a browser engine.")
        sys.exit(1)

    chosenTarget = workingTargets[0]
    probeSurfaces(chosenTarget)
    statusCounts = probeSustainedRate(chosenTarget)

    isSustainable = statusCounts.get(200, 0) >= 11 and not statusCounts.get(403)
    print(f"\nVERDICT: {'sustainable' if isSustainable else 'NOT clean -- review above'} (profile={chosenTarget})")
