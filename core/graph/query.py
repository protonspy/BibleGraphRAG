"""Query the knowledge graph with the GraphRAG query engine (global / local search).

`bgr build` indexes the corpus to <rag_root>/output/*.parquet; querying runs GraphRAG's own search
engine over those tables (core.graph.search) — `global` map-reduces over the community reports,
`local` seeds on entities via the Neo4j vector index and expands their neighbourhood. The answer
streams to stdout; phase progress goes to stderr. Neo4j must be running and the index built.
"""
from __future__ import annotations

import sys

METHODS = ("global", "local")


def run(
    question: str,
    method: str = "global",
    community_level: int | None = None,
    response_type: str | None = None,
    verbose: bool = False,
) -> int:
    """Answer a question with GraphRAG's query engine. Returns a process exit code."""
    if method not in METHODS:
        print(f"error: unknown method '{method}' (choose from {', '.join(METHODS)})", file=sys.stderr)
        return 2

    from core.graph import search

    rtype = response_type or "Multiple Paragraphs"
    level = community_level if community_level is not None else 2
    # The engine streams the answer to stdout itself; we don't re-print here.
    if method == "global":
        search.global_search(question, level=level, response_type=rtype, verbose=verbose)
    else:  # local
        search.local_search(question, level=level, response_type=rtype, verbose=verbose)
    return 0
