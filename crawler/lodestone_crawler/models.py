"""Canonical record shapes for Lodestone.

One StoryRecord is everything FanFiction.Net exposes about a story, normalized.
Listing pages and story pages both parse into this same shape -- listing pages
just leave a few fields None (they carry no cover image or chapter titles).
"""
from __future__ import annotations

import dataclasses
import datetime
import enum
from typing import Optional


class StoryStatus(enum.StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"


@dataclasses.dataclass(slots=True)
class Fandom:
    """A fandom as FFN models it: a display name plus its archive path.

    categoryId is only known when we reached the fandom through a directory or
    filter form; parsing a bare listing row gives us the name alone.
    """

    name: str
    sectionSlug: Optional[str] = None
    fandomSlug: Optional[str] = None
    categoryId: Optional[int] = None
    # Abbreviated on FFN's directory pages ("852K"), so good enough to order a
    # crawl by size but not to trust as a count.
    storyCount: Optional[int] = None


@dataclasses.dataclass(slots=True)
class StoryRecord:
    storyId: int
    title: str
    authorName: str
    authorId: int
    summary: str

    fandoms: list[Fandom] = dataclasses.field(default_factory=list)
    isCrossover: bool = False

    rating: Optional[str] = None
    language: Optional[str] = None
    genres: list[str] = dataclasses.field(default_factory=list)

    # Unbracketed names are "characters appear"; bracketed groups are the ships.
    # FFN's [A, B] syntax is the only relationship signal the whole site has.
    characters: list[str] = dataclasses.field(default_factory=list)
    ships: list[list[str]] = dataclasses.field(default_factory=list)

    chapterCount: Optional[int] = None
    wordCount: Optional[int] = None
    reviewCount: int = 0
    favoriteCount: int = 0
    followCount: int = 0

    status: StoryStatus = StoryStatus.IN_PROGRESS
    publishedAt: Optional[datetime.datetime] = None
    updatedAt: Optional[datetime.datetime] = None

    coverImageId: Optional[int] = None
    chapterTitles: list[str] = dataclasses.field(default_factory=list)

    @property
    def storyUrl(self) -> str:
        return f"https://www.fanfiction.net/s/{self.storyId}/1/"

    @property
    def favoritesPerThousandWords(self) -> Optional[float]:
        """A crude quality signal FFN itself never surfaces: popularity adjusted
        for length, so a 2K-word oneshot with 300 favs outranks a 400K-word epic
        with 400."""
        if not self.wordCount:
            return None
        return round(self.favoriteCount / (self.wordCount / 1000), 4)

    @property
    def isProbablyAbandoned(self) -> bool:
        """In-progress and untouched for two years. FFN offers no way to filter
        these out, and they are the single biggest complaint about its browse."""
        if self.status is StoryStatus.COMPLETE or self.updatedAt is None:
            return False
        age = datetime.datetime.now(datetime.UTC) - self.updatedAt
        return age.days > 730
