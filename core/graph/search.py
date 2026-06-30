"""Answer questions with the GraphRAG query engine (global / local search).

`bgr build` indexes the corpus to <rag_root>/output/*.parquet; these functions load those tables and
run GraphRAG's own search engine over them (graphrag.api) — the same engine, prompts, and context
builders GraphRAG ships, not a reimplementation. Embeddings live in on-disk lancedb tables under
<rag_root>/output/lancedb (rag/settings.yaml sets vector_store.type=lancedb), resolved natively by
GraphRAG — no database server at query time.

The answer streams to stdout token-by-token; map/reduce and context progress go to stderr via a
QueryCallbacks hook (kept off stdout so the answer stays pipe-clean). GraphRAG's load_config chdir's
into rag_root and the search prompts are read relative to that cwd, so each call wraps the run in a
chdir/restore (mirrors core.graph.engine.index). The index must be built first.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pandas as pd

from core.config import env
from core.graph.engine import _load_config


# --- progress feedback (to stderr; the streamed answer owns stdout) ----------------------

def _note(msg: str) -> None:
    """Emit one progress line to stderr so the answer streamed on stdout stays clean."""
    print(msg, file=sys.stderr, flush=True)


def _make_callbacks(verbose: bool):
    """A QueryCallbacks that narrates the map/reduce phases on stderr (global search)."""
    from graphrag.callbacks.query_callbacks import QueryCallbacks

    class _Progress(QueryCallbacks):
        def on_context(self, context) -> None:
            if verbose:
                _note("[graphrag] context assembled")

        def on_map_response_start(self, map_response_contexts) -> None:
            _note(f"[graphrag] map: {len(map_response_contexts)} report batch(es)…")

        def on_map_response_end(self, map_response_outputs) -> None:
            _note(f"[graphrag] map: done ({len(map_response_outputs)} batch(es))")

        def on_reduce_response_start(self, reduce_response_context) -> None:
            _note("[graphrag] reduce: synthesizing the answer…")

    return [_Progress()]


# --- parquet loading ---------------------------------------------------------------------

def _load_outputs(required: list[str], optional: tuple[str, ...] = ()) -> dict | None:
    """Read the GraphRAG output tables a query needs from <rag_root>/output (None if missing)."""
    output_dir = Path(env.rag_root) / "output"
    missing = [t for t in required if not (output_dir / f"{t}.parquet").is_file()]
    if missing:
        _note(f"error: GraphRAG index not found in {output_dir} (missing "
              f"{', '.join(missing)}); run `bgr build` first.")
        return None
    tables: dict[str, pd.DataFrame | None] = {
        t: pd.read_parquet(output_dir / f"{t}.parquet") for t in required
    }
    for t in optional:  # e.g. covariates — absent when claims are disabled
        p = output_dir / f"{t}.parquet"
        tables[t] = pd.read_parquet(p) if p.is_file() else None
    return tables


async def _stream(agen, stream: bool = True) -> str:
    """Drain a GraphRAG *_search_streaming async generator, returning the full text.

    stream=True echoes each chunk to stdout (the `bgr query` behaviour); stream=False just
    accumulates and returns it — used by the deep researcher, which runs many searches and must
    not flood stdout with every intermediate answer.
    """
    full = ""
    async for chunk in agen:
        full += chunk
        if stream:
            print(chunk, end="", flush=True)
    if stream:
        print()  # close the streamed line
    return full


def _run(coro_factory) -> str:
    """Load the config (chdir's into rag_root for prompt paths), run the async query, restore cwd."""
    cwd = os.getcwd()
    try:
        config = _load_config()
        return asyncio.run(coro_factory(config))
    finally:
        os.chdir(cwd)


# --- global search: map-reduce over community reports (GraphRAG engine) ------------------

def global_search(query: str, level: int = 2, response_type: str = "Multiple Paragraphs",
                  verbose: bool = False, stream: bool = True) -> str:
    """Map-reduce over the community reports with GraphRAG's global search engine."""
    tables = _load_outputs(["entities", "communities", "community_reports"])
    if tables is None:
        return ""
    started = time.perf_counter()
    _note(f"[graphrag/global] community level ≤ {level} · {len(tables['community_reports'])} reports")

    async def go(config):
        import graphrag.api as api
        return await _stream(api.global_search_streaming(
            config=config,
            entities=tables["entities"],
            communities=tables["communities"],
            community_reports=tables["community_reports"],
            community_level=level,
            dynamic_community_selection=False,
            response_type=response_type,
            query=query,
            callbacks=_make_callbacks(verbose),
            verbose=verbose,
        ), stream=stream)

    answer = _run(go)
    _note(f"[graphrag/global] done ({time.perf_counter() - started:.1f}s)")
    return answer


# --- local search: entity vector search + neighbourhood (GraphRAG engine) ----------------

def local_search(query: str, level: int = 2, response_type: str = "Multiple Paragraphs",
                 verbose: bool = False, stream: bool = True) -> str:
    """Seed on entities most similar to the query (lancedb vector store), expand, then synthesize."""
    tables = _load_outputs(
        ["communities", "community_reports", "text_units", "relationships", "entities"],
        optional=("covariates",),
    )
    if tables is None:
        return ""
    started = time.perf_counter()
    _note(f"[graphrag/local] vector search over {len(tables['entities'])} entities + neighbourhood…")

    async def go(config):
        import graphrag.api as api
        return await _stream(api.local_search_streaming(
            config=config,
            entities=tables["entities"],
            communities=tables["communities"],
            community_reports=tables["community_reports"],
            text_units=tables["text_units"],
            relationships=tables["relationships"],
            covariates=tables["covariates"],
            community_level=level,
            response_type=response_type,
            query=query,
            callbacks=_make_callbacks(verbose),
            verbose=verbose,
        ), stream=stream)

    answer = _run(go)
    _note(f"[graphrag/local] done ({time.perf_counter() - started:.1f}s)")
    return answer


# --- DRIFT search: community priming + iterative entity drill-down (GraphRAG engine) ------

def drift_search(query: str, level: int = 2, response_type: str = "Multiple Paragraphs",
                 verbose: bool = False, stream: bool = True) -> str:
    """Prime on community reports, then drill into entities/relationships and refine iteratively.

    A hybrid of global (community sensemaking) and local (entity neighbourhood): it seeds from both
    entity and community-report embeddings, so it needs the entity_description *and*
    community_full_content lancedb tables.
    """
    tables = _load_outputs(["entities", "communities", "community_reports", "text_units", "relationships"])
    if tables is None:
        return ""
    started = time.perf_counter()
    _note(f"[graphrag/drift] priming on {len(tables['community_reports'])} reports, "
          f"then drilling into {len(tables['entities'])} entities…")

    async def go(config):
        import graphrag.api as api
        return await _stream(api.drift_search_streaming(
            config=config,
            entities=tables["entities"],
            communities=tables["communities"],
            community_reports=tables["community_reports"],
            text_units=tables["text_units"],
            relationships=tables["relationships"],
            community_level=level,
            response_type=response_type,
            query=query,
            callbacks=_make_callbacks(verbose),
            verbose=verbose,
        ), stream=stream)

    answer = _run(go)
    _note(f"[graphrag/drift] done ({time.perf_counter() - started:.1f}s)")
    return answer


# --- basic search: plain vector RAG over text units (GraphRAG engine) ---------------------

def basic_search(query: str, response_type: str = "Multiple Paragraphs",
                 verbose: bool = False, stream: bool = True) -> str:
    """Plain vector RAG: retrieve the text units most similar to the query, then synthesize.

    No graph, no communities — just chunk retrieval. Needs the text_unit_text lancedb table.
    (No community level: basic search doesn't use the community hierarchy.)
    """
    tables = _load_outputs(["text_units"])
    if tables is None:
        return ""
    started = time.perf_counter()
    _note(f"[graphrag/basic] vector search over {len(tables['text_units'])} text units…")

    async def go(config):
        import graphrag.api as api
        return await _stream(api.basic_search_streaming(
            config=config,
            text_units=tables["text_units"],
            response_type=response_type,
            query=query,
            callbacks=_make_callbacks(verbose),
            verbose=verbose,
        ), stream=stream)

    answer = _run(go)
    _note(f"[graphrag/basic] done ({time.perf_counter() - started:.1f}s)")
    return answer
