"""Build a configured Graphiti client from Settings.

Uses OpenAIGenericClient (chat.completions) rather than OpenAIClient, because
OpenAIClient calls the OpenAI Responses API (responses.parse), which OpenRouter does
not expose — OpenRouter is Chat Completions only.

Structured-output mode is configurable via Settings (``STRUCTURED_OUTPUT_MODE``) and
defaults to ``json_object``. Native ``json_schema`` sends the raw Pydantic schema as
response_format; providers that enforce OpenAI's strict structured-output subset
(Azure/OpenAI) reject it with "'additionalProperties' is required to be supplied and to
be false", since model_json_schema() never emits that. In ``json_object`` mode Graphiti
injects the schema into the prompt and parses the JSON itself (with code-fence stripping
+ retry), which works across every OpenRouter-routed provider — trading provider-side
constrained decoding for prompt-guided, best-effort parsing. Use ``json_schema`` only for
constrained-decoding providers (vLLM-style, e.g. gpt-oss-120b) that enforce it.
"""
from __future__ import annotations

import logging

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

from core.config import Settings


def quiet_neo4j_logging() -> None:
    """Quiet benign driver noise so the CLI output stays readable.

    - neo4j.notifications: per-query 'property does not exist' WARNINGs from dedup
      queries running against a sparse graph.
    - graphiti_core.driver.neo4j_driver: execute_query logs every failure at ERROR
      *before* re-raising. build_indices_and_constraints fires CREATE INDEX
      IF NOT EXISTS statements concurrently; they race and raise
      EquivalentSchemaRuleAlreadyExists, which Graphiti safely ignores — but the
      premature ERROR logs flood the console even on a fresh, empty database. Our own
      per-episode handler still reports real ingestion failures.
    """
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    logging.getLogger("neo4j.pool").setLevel(logging.ERROR)
    logging.getLogger("graphiti_core.driver.neo4j_driver").setLevel(logging.CRITICAL)


def build_graphiti(settings: Settings) -> Graphiti:
    """Wire an LLM client, embedder, reranker, and Neo4j driver into a Graphiti instance."""
    llm_config = LLMConfig(
        api_key=settings.api_key,
        base_url=settings.base_url,
        model=settings.llm_model,
        small_model=settings.small_model,
    )
    llm_client = OpenAIGenericClient(
        config=llm_config, structured_output_mode=settings.structured_output_mode
    )

    embedder = OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            api_key=settings.embed_api_key,
            base_url=settings.embed_base_url,
            embedding_model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
        )
    )

    # Reranker is used at search time, not during ingestion; share the LLM routing.
    cross_encoder = OpenAIRerankerClient(config=llm_config)

    return Graphiti(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
    )
