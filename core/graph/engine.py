"""In-process GraphRAG indexing via graphrag.api (no subprocess).

`bgr build` calls graphrag.api.build_index directly, so prepare → index → load → embed runs in one
Python process with native progress (ConsoleWorkflowCallbacks) and Python-level error handling — a
smoother pipeline than shelling out and parsing stdout.

settings.yaml interpolates the env-var placeholders, which we set in os.environ from
core.config.Settings before loading the config, so the repo .env stays the single source of truth.
Mirrors graphrag.cli.index (see the vendored graphrag/ clone for the reference implementation).

Querying goes through GraphRAG's own search engine (core.graph.search → graphrag.api), over the same
parquet output; the entity embeddings it needs live in Neo4j via our vector store (neo4j_vectors).
load_neo4j additionally mirrors the parquet into Neo4j for `bgr inspect` and graph browsing.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from core.config import env


def _inject_env() -> None:
    """Populate os.environ so settings.yaml's ${...} placeholders resolve (repo .env stays canonical)."""
    os.environ["OPENAI_API_KEY"] = env.api_key
    os.environ["OPENAI_BASE_URL"] = env.base_url
    os.environ["GRAPHRAG_LLM_MODEL"] = env.llm_model
    os.environ["GRAPHRAG_EMBEDDING_MODEL"] = env.embedding_model
    os.environ["GRAPHRAG_EMBED_API_KEY"] = env.embed_api_key or env.api_key
    os.environ["GRAPHRAG_EMBED_BASE_URL"] = env.embed_base_url or env.base_url


def _load_config():
    """Load the GraphRagConfig from rag/settings.yaml with creds/models injected from Settings.

    NB: graphrag's load_config does os.chdir(rag_root), and the indexing pipeline depends on it —
    prompt paths in settings.yaml (e.g. prompts/extract_graph.txt) are read relative to the cwd at
    runtime. So the chdir must stand for the whole index; index() restores the cwd only once
    build_index returns. load_neo4j uses the absolute rag_root, so it's unaffected either way.
    """
    _inject_env()
    from graphrag.config.load_config import load_config

    return load_config(Path(env.rag_root))


# Workflows whose failure must not abort the build: embeddings are written to Neo4j by our vector
# store (core.graph.neo4j_vectors), and core.graph.embed backfills any that are missing after the
# load. A failed embedding write therefore still leaves the graph + community-report parquet — which
# is everything load_neo4j needs — intact, so the build can proceed to the Neo4j load.
_NONFATAL_WORKFLOWS = {"generate_text_embeddings"}


def index(verbose: bool = False) -> int:
    """Run the GraphRAG indexing pipeline in-process. Returns a process exit code."""
    import graphrag.api as api
    from graphrag.callbacks.console_workflow_callbacks import ConsoleWorkflowCallbacks
    from graphrag.logger.standard_logging import init_loggers

    from core.graph.neo4j_vectors import register_neo4j_vector_store

    # Register our Neo4j vector store so generate_text_embeddings writes embeddings into Neo4j
    # (settings.yaml: vector_store.type = neo4j) instead of lancedb.
    register_neo4j_vector_store()

    # _load_config (graphrag) chdir's into rag_root, and the pipeline reads prompt paths relative to
    # that cwd. Keep it for the whole index, then restore so later steps (load_neo4j) aren't thrown off.
    cwd = os.getcwd()
    try:
        config = _load_config()
        init_loggers(config=config, verbose=verbose)
        outputs = asyncio.run(
            api.build_index(
                config=config,
                callbacks=[ConsoleWorkflowCallbacks(verbose=verbose)],
                verbose=verbose,
            )
        )
    finally:
        os.chdir(cwd)
    failed = [o for o in outputs if o.error is not None]
    fatal = [o for o in failed if o.workflow not in _NONFATAL_WORKFLOWS]
    for o in failed:
        kind = "warning" if o.workflow in _NONFATAL_WORKFLOWS else "error"
        print(f"{kind} [{o.workflow}]: {o.error}", file=sys.stderr)
    if failed and not fatal:
        print("note: embedding write failed but is not needed — the Neo4j query path embeds its own "
              "vectors (core.graph.embed).", file=sys.stderr)
    return 1 if fatal else 0
