"""Turn parsed verse records into Graphiti episodes — one episode per chapter."""
from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import groupby

# Trailing "-N" that the parser appends to output filenames (akjv-1 -> akjv).
_VERSION_SUFFIX = re.compile(r"-\d+$")


def translation_label(stem: str) -> str:
    """Derive a translation name from an output file stem ('akjv-1' -> 'akjv')."""
    return _VERSION_SUFFIX.sub("", stem)


@dataclass(frozen=True)
class Episode:
    """A chapter's worth of verses, ready for Graphiti.add_episode."""

    name: str                 # "Genesis 1"
    body: str                 # one verse per line, prefixed with its number
    source_description: str   # "akjv — Genesis 1"
    index: int                # canonical position, for deterministic reference_time
    book: str
    chapter: int
    verses: int


def build_episodes(records: list[dict], translation: str) -> list[Episode]:
    """Group consecutive verse records by (book, chapter) into chapter episodes.

    Records are assumed to be in canonical order (as parsed). `index` increments per
    episode so callers can derive a monotonic reference_time preserving narrative order.
    """
    episodes: list[Episode] = []
    for (book, chapter), group in groupby(records, key=lambda r: (r["book"], r["chapter"])):
        verses = list(group)
        body = "\n".join(f"{v['verse']} {v['content']}" for v in verses)
        episodes.append(
            Episode(
                name=f"{book} {chapter}",
                body=body,
                source_description=f"{translation} — {book} {chapter}",
                index=len(episodes),
                book=book,
                chapter=chapter,
                verses=len(verses),
            )
        )
    return episodes
