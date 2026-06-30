"""Query the knowledge graph with the GraphRAG query engine (global / local search).

`bgr build` indexes the corpus to <rag_root>/output/*.parquet; querying runs GraphRAG's own search
engine over those tables (core.graph.search). The four methods:
  global — map-reduce over the community reports (sensemaking; no vectors)
  local  — seed on entities via the lancedb vector store, expand their neighbourhood
  drift  — prime on community reports, then drill into entities/relationships and refine
  basic  — plain vector RAG over text units (chunk retrieval, no graph)
The answer streams to stdout; phase progress goes to stderr. Requires a built index — parquet plus
the entity/chunk/community lancedb tables (see rag/settings.yaml embed_text.names).
"""
from __future__ import annotations

import sys

METHODS = ("global", "local", "drift", "basic")


def run(
    question: str,
    method: str | None = None,
    community_level: int | None = None,
    response_type: str | None = None,
    verbose: bool = False,
) -> int:
    """Answer a question with GraphRAG's query engine. Returns a process exit code.

    `method=None` (the default — no explicit -m) routes the question to the best method via a quick
    LLM classifier (core.graph.route); an explicit method skips the classifier.
    """
    if method is None:
        from core.graph.route import classify_method
        method = classify_method(question)
    if method not in METHODS:
        print(f"error: unknown method '{method}' (choose from {', '.join(METHODS)})", file=sys.stderr)
        return 2

    from core.graph import search

    rtype = response_type or "Multiple Paragraphs"
    level = community_level if community_level is not None else 2
    # The engine streams the answer to stdout itself; we don't re-print here.
    if method == "global":
        search.global_search(question, level=level, response_type=rtype, verbose=verbose)
    elif method == "local":
        search.local_search(question, level=level, response_type=rtype, verbose=verbose)
    elif method == "drift":
        search.drift_search(question, level=level, response_type=rtype, verbose=verbose)
    else:  # basic — plain vector RAG over text units; no community level
        search.basic_search(question, response_type=rtype, verbose=verbose)
    return 0
