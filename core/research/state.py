"""LangGraph state for the deep researcher.

`learnings` and `sources` accumulate across rounds (operator.add appends what each round returns);
everything else is scalar/replaced per round. The raw GraphRAG answer is deliberately NOT a field
here — it lives only inside research_round and is discarded after distillation, so the context that
flows to synthesis stays small (the compression invariant from open_deep_research's compress step).
"""
from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict


class SubQuery(TypedDict):
    """One planned search: the query text plus the goal/next-directions that seeded it."""

    query: str
    goal: str


class Learning(TypedDict):
    """A distilled finding kept after compression, with its provenance."""

    text: str
    mode: str   # the GraphRAG method that produced it (global/local/drift/basic)
    query: str  # the sub-query it answered


class ResearchState(TypedDict, total=False):
    """Top-level state for the deep_researcher graph."""

    question: str                                       # the user's original question
    output_mode: Literal["report", "answer"]
    brief: str                                          # restated, self-contained research brief
    seed: str                                           # text the next round plans its queries from
    pending: list[str]                                  # follow-up questions surfaced this round
    frontier: list[SubQuery]                            # queries planned for the current round
    depth: int                                          # rounds remaining
    breadth: int                                        # queries per round (halves each round)
    global_calls: int                                   # cost governor: expensive global searches so far
    learnings: Annotated[list[Learning], operator.add]  # accumulated distilled findings
    sources: Annotated[list[str], operator.add]         # provenance (mode·query; real citations = v2)
    report: str                                         # final synthesized output
