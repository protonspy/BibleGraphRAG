"""Turn parsed verse records into GraphRAG input rows — one document per pericope (or chapter).

An "episode" here is a contiguous unit of text (a pericope, or a whole chapter with --no-pericope).
`build_graphrag_input` serializes the selected episodes into the CSV that `graphrag index` reads,
one row per episode. Because GraphRAG chunks each document independently, one-pericope-per-row
keeps extraction focused and prevents bleed across pericope boundaries.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path

# Trailing "-N" that the parser appends to output filenames (akjv-1 -> akjv).
_VERSION_SUFFIX = re.compile(r"-\d+$")


def translation_label(stem: str) -> str:
    """Derive a translation name from an output file stem ('akjv-1' -> 'akjv')."""
    return _VERSION_SUFFIX.sub("", stem)


@dataclass(frozen=True)
class Episode:
    """One contiguous unit of text (a pericope or a whole chapter) → one GraphRAG input row."""

    name: str                 # stable reference / document id, e.g. "Genesis 1" or "Genesis 2:4-25"
    body: str                 # one verse per line, prefixed with its number
    source_description: str   # "akjv — Genesis 1" (becomes the document title)
    index: int                # canonical position (kept for ordering / future use)
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
                    # Name is the stable, unique reference only — no LLM-generated title, which
                    # is volatile across re-segmentation and would break checkpoint matching.
                    # The descriptive title is kept in source_description for human context.
                    name=ref,
                    body=body,
                    source_description=f"{translation} — {ref}" + (f" — {title}" if title else ""),
                    index=len(episodes),
                    book=book,
                    chapter=chapter,
                    verses=len(in_span),
                )
            )
    return episodes


# Column order matches rag/settings.yaml: id_column=id, title_column=title, text_column=text;
# book/chapter feed chunking.prepend_metadata so every chunk carries its locus.
GRAPHRAG_INPUT_COLUMNS = ["id", "title", "text", "book", "chapter"]


def build_graphrag_input(episodes: list[Episode], translation: str, rag_root: Path) -> Path:
    """Write the selected episodes to <rag_root>/input/<translation>.csv (one row per episode).

    Returns the path written. Overwrites any existing file for this translation, so the CSV always
    reflects the current selection; `id` is the stable episode name, which keeps `graphrag update`
    idempotent across re-runs. The csv module handles quoting of commas/quotes/newlines in verse text.
    """
    input_dir = rag_root / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    out_path = input_dir / f"{translation}.csv"
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=GRAPHRAG_INPUT_COLUMNS)
        writer.writeheader()
        for e in episodes:
            writer.writerow({
                "id": e.name,
                "title": e.source_description,
                "text": e.body,
                "book": e.book,
                "chapter": e.chapter,
            })
    return out_path
