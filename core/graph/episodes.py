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


def build_pericope_episodes(
    records: list[dict], translation: str, pericope_map: dict
) -> list[Episode]:
    """Group verses into one episode per pericope, using a cached pericope map.

    `pericope_map` is the {"Book Chapter": [{start_verse, end_verse, title}, ...]} structure
    produced by core.pipe.pericope. A chapter missing from the map falls back to a single
    whole-chapter episode, so build never silently drops verses. `index` increments per
    episode in canonical order for a monotonic reference_time.
    """
    episodes: list[Episode] = []
    for (book, chapter), group in groupby(records, key=lambda r: (r["book"], r["chapter"])):
        verses = list(group)
        spans = pericope_map.get(f"{book} {chapter}") or [
            {"start_verse": verses[0]["verse"], "end_verse": verses[-1]["verse"], "title": ""}
        ]
        for span in spans:
            lo, hi = span["start_verse"], span["end_verse"]
            in_span = [v for v in verses if lo <= v["verse"] <= hi]
            if not in_span:
                continue
            ref = f"{book} {chapter}:{lo}-{hi}" if lo != hi else f"{book} {chapter}:{lo}"
            title = (span.get("title") or "").strip()
            body = "\n".join(f"{v['verse']} {v['content']}" for v in in_span)
            episodes.append(
                Episode(
                    name=f"{ref} — {title}" if title else ref,
                    body=body,
                    source_description=f"{translation} — {ref}",
                    index=len(episodes),
                    book=book,
                    chapter=chapter,
                    verses=len(in_span),
                )
            )
    return episodes
