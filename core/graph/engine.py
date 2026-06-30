"""In-process GraphRAG indexing via graphrag.api (no subprocess).

`bgr build` calls graphrag.api.build_index directly, so prepare → index runs in one Python process
with native progress (ConsoleWorkflowCallbacks) and Python-level error handling — a smoother pipeline
than shelling out and parsing stdout.

settings.yaml interpolates the env-var placeholders, which we set in os.environ from
core.config.Settings before loading the config, so the repo .env stays the single source of truth.
Mirrors graphrag.cli.index (see the vendored graphrag/ clone for the reference implementation).

Indexing writes the parquet tables and the lancedb vector tables (vector_store.type=lancedb) under
<rag_root>/output; querying (core.graph.search → graphrag.api) reads both back natively.
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
    runtime. So the chdir must stand for the whole index; index() restores the cwd once build_index
    returns. The lancedb db_uri is likewise relative to rag_root, so the chdir keeps it consistent.
    """
    _inject_env()
    from graphrag.config.load_config import load_config

    return load_config(Path(env.rag_root))


def index(verbose: bool = False) -> int:
    """Run the GraphRAG indexing pipeline in-process. Returns a process exit code."""
    import graphrag.api as api
    from graphrag.callbacks.console_workflow_callbacks import ConsoleWorkflowCallbacks
    from graphrag.logger.standard_logging import init_loggers

    # _load_config (graphrag) chdir's into rag_root, and the pipeline reads prompt paths relative to
    # that cwd. Keep it for the whole index, then restore the cwd once build_index returns.
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
    # Every workflow matters now — embeddings (lancedb) are written here, not backfilled elsewhere.
    failed = [o for o in outputs if o.error is not None]
    for o in failed:
        print(f"error [{o.workflow}]: {o.error}", file=sys.stderr)
    return 1 if failed else 0
