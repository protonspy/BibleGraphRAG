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


def classify_method(question: str) -> str:
    """Classify `question` into one of METHODS via a small LLM call; fall back to 'local' on error."""
    from core.llm import structured_model

    try:
        model = structured_model(_Route, model=env.small_model or env.llm_model)
        decision: _Route = model.invoke([("system", _SYSTEM), ("human", question)])  # type: ignore[assignment]
        if decision.method not in METHODS:  # belt-and-suspenders; the schema already constrains it
            raise ValueError(f"router returned unknown method {decision.method!r}")
        print(f"[router] {decision.method} — {decision.reason}", file=sys.stderr, flush=True)
        return decision.method
    except Exception as exc:
        print(f"[router] classification failed ({type(exc).__name__}: {exc}); "
              f"falling back to '{_FALLBACK}'", file=sys.stderr, flush=True)
        return _FALLBACK
