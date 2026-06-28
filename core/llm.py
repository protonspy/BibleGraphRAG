"""Shared LangChain access for the pipeline's own (non-Graphiti) LLM calls.

Graphiti manages its own OpenAI-compatible client for entity/edge extraction and dedup. This
module is for the steps we drive directly — pericope segmentation and extraction-instruction
enrichment — using LangChain's ChatOpenAI pointed at OpenRouter (same base_url/key as the
rest of the pipeline).
"""
from __future__ import annotations

from typing import TypeVar

from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from core.config import env

_Schema = TypeVar("_Schema", bound=BaseModel)


def chat_model(model: str | None = None, *, temperature: float = 0, json_mode: bool = False) -> ChatOpenAI:
    """Build a ChatOpenAI bound to OpenRouter. Raises RuntimeError if no API key is set.

    json_mode sets response_format=json_object so the model returns parseable JSON (the
    prompt must still describe the shape). Defaults to deterministic output (temperature 0).
    """
    if not env.api_key or env.api_key.startswith("your_"):
        raise RuntimeError("OPENAI_API_KEY is not set in .env")
    model_kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
    return ChatOpenAI(
        model=model or env.llm_model,
        api_key=env.api_key,
        base_url=env.base_url,
        temperature=temperature,
        model_kwargs=model_kwargs,
    )


def structured_model(
    schema: type[_Schema], model: str | None = None, *, temperature: float = 0
) -> Runnable:
    """ChatOpenAI that returns a validated `schema` instance (LangChain structured output).

    Uses OpenAI/OpenRouter native structured outputs (json_schema), the same mechanism
    Graphiti relies on, so invoking the returned runnable yields a parsed Pydantic object —
    no manual JSON parsing. Raises RuntimeError if no API key is set.
    """
    return chat_model(model, temperature=temperature).with_structured_output(schema, method="json_schema")
