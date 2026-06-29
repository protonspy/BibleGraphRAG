"""Extraction-instruction enrichment — per-chapter guidance that steers Graphiti extraction.

Before extracting a chapter, a focused (small-model) LLM call reads the whole chapter — with
its book/chapter context and the project's entity/edge ontology — and writes concise,
context-specific instructions: which entity TYPES to expect, domain disambiguations (e.g.
Eden the region vs the Garden of Eden planted within it; "the man" = Adam), and which
relationships matter. That string is passed to graphiti.add_episode(custom_extraction_
instructions=...) for every pericope episode in the chapter, raising extraction precision.
It deliberately does NOT enumerate a list of named entities to find: priming the extractor
with a closed set biases its recall toward that set and suppresses everything else.

Per-chapter (not per-pericope) is deliberate: one call per chapter is cheaper and the chapter
gives the model enough context to see the whole arc and resolve disambiguations that span
pericopes. This is a single-shot structured chain, not a tool-using agent — it can graduate to
an agent later if it needs tools (cross-references, a glossary, querying the existing graph).
Guidance is cached in cache/<translation>-guidance.json keyed by "Book Chapter" (cache-first).
"""
from __future__ import annotations

import json
import sys

from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from core.config import env
from core.graph.entities import EDGE_TYPES, ENTITY_TYPES
from core.llm import structured_model
from core.pipe.pericope import CACHE_DIR, chapter_key, select_chapters


class ExtractionGuidance(BaseModel):
    """Context-specific guidance for extracting one chapter."""

    instructions: str = Field(
        description="Concise instructions (2-5 sentences) for extracting entities and "
        "relationships from THIS chapter: which entity TYPES to expect, disambiguations "
        "(aliases that are the SAME entity; similar names that are DIFFERENT entities), and "
        "which relationships matter. Refer only to the provided entity and edge types. Do "
        "NOT enumerate a checklist of specific named entities to find — the extractor reads "
        "the full text itself, and a fixed list would bias it to that closed set and suppress "
        "recall of everything else."
    )


def _collapse(text: str | None) -> str:
    return " ".join((text or "").split())


def _ontology_context() -> str:
    """Compact description of the entity/edge ontology, fed to the enrichment LLM."""
    ents = "\n".join(f"- {name}: {_collapse(model.__doc__)}" for name, model in ENTITY_TYPES.items())
    edges = ", ".join(EDGE_TYPES)
    return (
        "ENTITY TYPES (entities are classified into exactly one):\n" + ents +
        "\n\nEDGE TYPES (preferred relationship names):\n" + edges
    )


_SYSTEM_PROMPT = (
    "You are a biblical-studies assistant preparing a knowledge-graph extractor. Given one "
    "chapter and the available ontology, write guidance that helps the extractor capture the "
    "right entities and relationships precisely in THIS biblical context. Focus on: expected "
    "entity types; aliases that denote the SAME entity (e.g. 'the LORD God' and 'God'); "
    "similar names that are DIFFERENT entities (e.g. a region vs a site within it); and the "
    "relationships worth recording. Be concise and specific to the chapter; do not invent "
    "entity or edge types beyond those listed. Do NOT produce an exhaustive list of the named "
    "entities present — the extractor reads the full text itself, and naming a closed set "
    "would bias it toward those and suppress recall of the rest.\n\n" + _ontology_context()
)


def _cache_path(translation: str):
    return CACHE_DIR / f"{translation}-guidance.json"


def load_cache(translation: str) -> dict:
    """Load cached per-chapter guidance for a translation, or an empty skeleton if absent."""
    path = _cache_path(translation)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"translation": translation, "model": None, "guidance": {}}


def _compose(entry: dict) -> str:
    """Build the final instruction string passed to Graphiti from a cached guidance entry.

    Only the disambiguation/relationship instructions are passed through. We deliberately do
    not feed a list of expected named entities to the extractor: priming it with a closed set
    biases recall toward that set and suppresses everything else.
    """
    return (entry.get("instructions") or "").strip()


def guide_chapter(book: str, chapter: int, verses: list[dict], guide: Runnable) -> dict:
    """Generate guidance for one chapter; never raises (falls back to empty guidance)."""
    body = "\n".join(f"{v['verse']} {v['content']}" for v in verses)
    user = f"Chapter: {book} {chapter}\n---\n{body}\n---\nWrite extraction guidance for this chapter."
    try:
        result: ExtractionGuidance = guide.invoke([("system", _SYSTEM_PROMPT), ("human", user)])
        return result.model_dump()
    except Exception as exc:  # enrichment is best-effort; a failure just means no guidance
        print(f"warning: enrichment failed for {book} {chapter} "
              f"({type(exc).__name__}: {exc}); extracting without guidance", file=sys.stderr)
        return {"instructions": ""}


def ensure_guidance(
    records: list[dict],
    translation: str,
    *,
    book: str | None = None,
    chapter: int | None = None,
    chapters: set[int] | None = None,
    limit: int | None = None,
    model: str | None = None,
    force: bool = False,
) -> dict[str, str]:
    """Ensure in-scope chapters have cached guidance, generating missing ones, cache-first.

    Returns {chapter_key: instruction_string} for the selected chapters. Uses the small model
    by default (simple classification task). Raises RuntimeError if generation is needed but
    no API key is configured.
    """
    model = model or env.small_model or env.llm_model
    groups = select_chapters(records, book, chapter, limit, chapters=chapters)
    cache = load_cache(translation)
    guidance = cache["guidance"]
    pending = [grp for grp in groups if force or chapter_key(grp[0], grp[1]) not in guidance]

    if pending:
        guide = structured_model(ExtractionGuidance, model)  # raises RuntimeError if no API key
        for i, (b, c, verses) in enumerate(pending, 1):
            guidance[chapter_key(b, c)] = guide_chapter(b, c, verses, guide)
            print(f"[enrich {i}/{len(pending)}] {b} {c}")
        cache["model"] = model
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(translation).write_text(
            json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    return {
        chapter_key(b, c): _compose(guidance[chapter_key(b, c)])
        for b, c, _ in groups
        if chapter_key(b, c) in guidance
    }
