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
    scholar_query: str  # the same need as academic search keywords (drives the OpenAlex source)


class Learning(TypedDict):
    """A distilled finding kept after compression, with its provenance."""

    text: str
    source: str  # which source produced it: "graph" (Bible knowledge graph) or "openalex" (scholarship)
    mode: str    # the GraphRAG method (global/local/drift/basic), or "openalex"
    query: str   # the sub-query it answered


class ResearchState(TypedDict, total=False):
    """Top-level state for the deep_researcher graph."""

    question: str                                       # the user's original question
    output_mode: Literal["report", "answer"]
    scholar: bool                                       # augment each query with OpenAlex scholarship
    brief: str                                          # restated, self-contained research brief
    seed: str                                           # text the next round plans its queries from
    pending: list[str]                                  # follow-up questions surfaced this round
    frontier: list[SubQuery]                            # queries planned for the current round
    depth: int                                          # rounds remaining
    breadth: int                                        # queries per round (halves each round)
    global_calls: int                                   # cost governor: expensive global searches so far
    learnings: Annotated[list[Learning], operator.add]  # accumulated distilled findings
    sources: Annotated[list[str], operator.add]         # the search trail (mode·query)
    citations: Annotated[list[str], operator.add]       # real scholarly references (OpenAlex, OA-only)
    lit_references: Annotated[list[str], operator.add]  # works the read articles themselves cite (validation/snowball)
    read_works: Annotated[list[str], operator.add]      # ids of works already deep-read (cross-round dedup)
    report: str                                         # final synthesized output
