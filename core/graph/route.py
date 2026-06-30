"""Route a question to the best GraphRAG search method with one cheap LLM call.

`bgr query <q>` without an explicit -m/--method asks a small model to classify the question into
global / local / drift / basic before running it, so the caller doesn't need to know GraphRAG's
search taxonomy. An explicit -m skips the router entirely. The decision is logged to stderr; the
chosen method's streamed answer still owns stdout. On any failure the router falls back to 'local'
(cheap, entity-centric, a safe general default) rather than aborting the query.
"""
from __future__ import annotations

import sys
from typing import Literal

from pydantic import BaseModel, Field

from core.config import env
from core.graph.query import METHODS

_FALLBACK = "local"

_SYSTEM = """You route a question about a knowledge graph (built from a text corpus) to the single \
best search strategy. Choose exactly one:

- global: broad, thematic "sensemaking" across the whole corpus — overarching themes or summaries \
spanning many sections ("what are the major themes", "how is X portrayed across the book").
- local: one specific entity (person, place, object) and its immediate connections \
("who is X", "what did X do", "how are X and Y related").
- drift: needs both the big picture and specific detail, or multi-hop reasoning tracing something \
across the corpus ("how does the covenant theme develop from X to Y").
- basic: a simple factual lookup answerable from the literal wording of a passage, no graph needed \
("what does <reference> say", direct quotes).

Pick the single best fit for the question."""


class _Route(BaseModel):
    """The router's decision."""

    method: Literal["global", "local", "drift", "basic"] = Field(description="The chosen search method.")
    reason: str = Field(description="One short clause justifying the choice.")


def classify_method(question: str, allowed: tuple[str, ...] = METHODS) -> str:
    """Classify `question` into one of `allowed` methods via a small LLM call; fall back on error.

    `allowed` constrains the choice to a subset of METHODS (the deep researcher passes
    ("local", "global") to keep drift's per-query cost out of its loop). It defaults to all of
    METHODS, so `bgr query` routing is unchanged. The fallback is 'local' when allowed, else the
    first allowed method.
    """
    from core.llm import structured_model

    fallback = _FALLBACK if _FALLBACK in allowed else allowed[0]
    try:
        model = structured_model(_Route, model=env.small_model or env.llm_model)
        system = _SYSTEM
        if set(allowed) != set(METHODS):  # restrict the choice; the schema still permits all four
            system += f"\n\nFor this task choose ONLY from: {', '.join(allowed)}."
        decision: _Route = model.invoke([("system", system), ("human", question)])  # type: ignore[assignment]
        method = decision.method if decision.method in allowed else fallback
        print(f"[router] {method} — {decision.reason}", file=sys.stderr, flush=True)
        return method
    except Exception as exc:
        print(f"[router] classification failed ({type(exc).__name__}: {exc}); "
              f"falling back to '{fallback}'", file=sys.stderr, flush=True)
        return fallback
