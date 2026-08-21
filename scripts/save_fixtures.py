"""Snapshot real FFN pages as parser test fixtures.

Re-run when FFN changes markup; tests then tell you exactly what broke.
"""
import pathlib
import time

from curl_cffi import requests

FIXTURE_DIR = pathlib.Path(__file__).resolve().parent.parent / "crawler" / "tests" / "fixtures"

FIXTURE_SOURCES = {
    "just_in.html": "https://www.fanfiction.net/j/0/0/0/",
    "browse_fandom.html": "https://www.fanfiction.net/book/Harry-Potter/?&srt=1&r=10&p=2",
    "browse_deep.html": "https://www.fanfiction.net/book/Harry-Potter/?&srt=1&r=10&p=3000",
    "browse_small_fandom.html": "https://www.fanfiction.net/anime/Millionaire-Detective/?&srt=1&r=10&p=1",
    "story_page.html": "https://www.fanfiction.net/s/13571544/1/",
    "search_results.html": "https://www.fanfiction.net/search/?keywords=time+travel&ready=1&type=story",
    "crossover_directory.html": "https://www.fanfiction.net/crossovers/Harry-Potter/224/",
    "section_directory.html": "https://www.fanfiction.net/book/",
}

if __name__ == "__main__":
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session(impersonate="chrome")
    for fixtureName, url in FIXTURE_SOURCES.items():
        response = session.get(url, timeout=30)
        destination = FIXTURE_DIR / fixtureName
        # FFN sends no charset header; curl_cffi guesses latin-1 and mangles
        # every non-ASCII byte. The payload is always UTF-8 -- decode it as such.
        decodedHtml = response.content.decode("utf-8", errors="replace")
        destination.write_text(decodedHtml, encoding="utf-8")
        print(f"{fixtureName:28} {response.status_code} {len(decodedHtml):>8}b")
        time.sleep(5)
