"""Pericope segmentation step — group a chapter's verses into semantic units via LLM.

A whole chapter is too large a unit for one GraphRAG extraction pass: entities and
relations get dropped, and coreference ("the tree in the midst of the garden") resolves to
the wrong referent across that much text. This step asks an LLM to split each chapter into
*pericopes* — contiguous verse ranges that form a single narrative or teaching unit — so
`build` can feed one small, focused document per pericope instead of one per chapter.

Boundaries are cached in cache/<translation>-pericopes.json; segmentation is quasi-
deterministic and we don't want to re-pay the LLM on every build. Re-run with --force to
re-segment.
"""
from __future__ import annotations

import json
import sys
from itertools import groupby
from pathlib import Path

from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from core.config import env
from core.llm import structured_model
from core.pipe.parser import load_records
from core.graph.episodes import translation_label

# Derived/cacheable artifacts live here, separate from parser outputs (output/) and
# graph exports (export/). Re-run `bgr pericope --force` to rebuild.
CACHE_DIR = Path("cache")

_SYSTEM_PROMPT = (
    "You are a biblical scholar segmenting a chapter into pericopes. A pericope is a "
    "self-contained unit of text — a single scene, speech, dialogue, genealogy, or teaching "
    "— that can be read on its own. Typical pericopes run 3-25 verses; never split in the "
    "middle of a scene or a continuous speech. The pericopes must be in order, contiguous, "
    "and non-overlapping, together covering every verse from the first to the last with no "
    "gaps, using the actual verse numbers shown. Titles are short (3-8 words) and descriptive."
)


class PericopeSpan(BaseModel):
    """One pericope: a contiguous range of verses forming a single unit of text."""

    start_verse: int = Field(description="Verse number where the pericope begins")
    end_verse: int = Field(description="Verse number where the pericope ends")
    title: str = Field(description="Short 3-8 word descriptive title for the pericope")


class PericopeList(BaseModel):
    """All pericopes covering a chapter, in order and tiling it without gaps."""

    pericopes: list[PericopeSpan] = Field(description="Pericopes in canonical order")


def _cache_path(translation: str) -> Path:
    return CACHE_DIR / f"{translation}-pericopes.json"


def load_cache(translation: str) -> dict:
    """Load the cached pericope map for a translation, or an empty skeleton if absent."""
    path = _cache_path(translation)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"translation": translation, "model": None, "chapters": {}}


def chapter_key(book: str, chapter: int) -> str:
    return f"{book} {chapter}"


def _normalize_ranges(raw: list[dict], verse_numbers: list[int]) -> list[dict]:
    """Coerce model-proposed ranges into a contiguous, gap-free tiling of the chapter.

    The model's end boundaries and titles are kept as guidance; starts are reflowed so the
    pericopes tile [min_verse, max_verse] exactly. Returns a single whole-chapter pericope
    if nothing usable comes back.
    """
    lo, hi = verse_numbers[0], verse_numbers[-1]
    candidates = []
    for item in raw:
        try:
            end = int(item["end_verse"])
        except (KeyError, TypeError, ValueError):
            continue
        title = str(item.get("title", "")).strip()
        candidates.append((end, title))
    candidates.sort(key=lambda c: c[0])

    out: list[dict] = []
    cursor = lo
    for end, title in candidates:
        if end < cursor:
            continue  # already covered; skip stray/overlapping range
        end = min(end, hi)
        out.append({"start_verse": cursor, "end_verse": end, "title": title})
        cursor = end + 1
        if cursor > hi:
            break

    if not out:
        return [{"start_verse": lo, "end_verse": hi, "title": ""}]
    if out[-1]["end_verse"] < hi:  # extend the tail to cover the rest
        out[-1]["end_verse"] = hi
    return out


def segment_chapter(book: str, chapter: int, verses: list[dict], segmenter: Runnable) -> list[dict]:
    """Ask the LLM to split one chapter's verses into pericopes; never raises.

    `segmenter` is a structured-output runnable returning a PericopeList (see structured_model).
    """
    verse_numbers = [v["verse"] for v in verses]
    body = "\n".join(f"{v['verse']} {v['content']}" for v in verses)
    user = f"Segment {book} chapter {chapter} into pericopes.\n\n{body}"
    try:
        result: PericopeList = segmenter.invoke([("system", _SYSTEM_PROMPT), ("human", user)])
        raw = [span.model_dump() for span in result.pericopes]
        return _normalize_ranges(raw, verse_numbers)
    except Exception as exc:  # a bad chapter falls back to whole-chapter, never aborts
        print(f"warning: segmentation failed for {book} {chapter} "
              f"({type(exc).__name__}: {exc}); using whole chapter", file=sys.stderr)
        return [{"start_verse": verse_numbers[0], "end_verse": verse_numbers[-1], "title": ""}]


def select_chapters(
    records: list[dict],
    book: str | None,
    chapter: int | None,
    limit: int | None,
    chapters: set[int] | None = None,
):
    """Yield (book, chapter, verses) groups matching the filters, in canonical order."""
    groups = [
        (b, c, list(g))
        for (b, c), g in groupby(records, key=lambda r: (r["book"], r["chapter"]))
    ]
    if book is not None:
        groups = [grp for grp in groups if grp[0].lower() == book.lower()]
    if chapter is not None:
        groups = [grp for grp in groups if grp[1] == chapter]
    if chapters is not None:
        groups = [grp for grp in groups if grp[1] in chapters]
    if limit is not None:
        groups = groups[:limit]
    return groups


def ensure_cache(
    records: list[dict],
    translation: str,
    *,
    book: str | None = None,
    chapter: int | None = None,
    chapters: set[int] | None = None,
    limit: int | None = None,
    model: str | None = None,
    force: bool = False,
) -> dict:
    """Segment any in-scope chapters not yet cached, then return the chapters map.

    Shared by `bgr build` (default pericope mode segments inline) and the standalone `bgr
    pericope` command. Only the LLM is called for chapters missing from the cache (or all,
    when force). `limit` caps the chapters considered, which safely over-covers an
    episode-level limit since each chapter yields at least one pericope. Raises RuntimeError
    if segmentation is needed but no API key is configured.
    """
    model = model or env.small_model or env.llm_model
    groups = select_chapters(records, book, chapter, limit, chapters=chapters)
    cache = load_cache(translation)
    chapters = cache["chapters"]
    pending = [grp for grp in groups if force or chapter_key(grp[0], grp[1]) not in chapters]
    if not pending:
        return chapters

    segmenter = structured_model(PericopeList, model)  # raises RuntimeError if no API key
    for i, (b, c, verses) in enumerate(pending, 1):
        pericopes = segment_chapter(b, c, verses, segmenter)
        chapters[chapter_key(b, c)] = pericopes
        labels = ", ".join(f"{p['start_verse']}-{p['end_verse']}" for p in pericopes)
        print(f"[segment {i}/{len(pending)}] {b} {c}: {len(pericopes)} pericopes [{labels}]")

    cache["model"] = model
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(translation).write_text(
        json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return chapters


def run(
    target: str,
    book: str | None = None,
    chapter: int | None = None,
    chapters: set[int] | None = None,
    limit: int | None = None,
    model: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> int:
    """Segment a parsed corpus into pericopes and cache them to cache/<translation>-pericopes.json."""
    try:
        records, path = load_records(target)
    except FileNotFoundError as exc:
        print(f"error: parsed corpus not found: {exc} (run `bgr parser --target {target}` first)", file=sys.stderr)
        return 1

    translation = translation_label(path.stem)
    model = model or env.small_model or env.llm_model
    groups = select_chapters(records, book, chapter, limit, chapters=chapters)
    if not groups:
        print("error: no chapters match the given filters", file=sys.stderr)
        return 1

    cache = load_cache(translation)
    chapters = cache["chapters"]
    pending = [grp for grp in groups if force or chapter_key(grp[0], grp[1]) not in chapters]

    print(f"Corpus  : {path} (translation '{translation}', model '{model}')")
    print(f"Chapters: {len(groups)} selected, {len(pending)} to segment "
          f"({len(groups) - len(pending)} cached)")

    if dry_run:
        for b, c, verses in groups[:5]:
            print(f"  {b} {c}: {len(verses)} verses")
        if len(groups) > 5:
            print(f"  … (+{len(groups) - 5} more)")
        print("dry-run: nothing segmented")
        return 0

    if not pending:
        print("nothing to do (all selected chapters already cached; use --force to re-segment)")
        return 0

    try:
        ensure_cache(records, translation, book=book, chapter=chapter, chapters=chapters,
                     limit=limit, model=model, force=force)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"done: cached -> {_cache_path(translation)}")
    return 0
