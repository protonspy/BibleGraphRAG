"""Shared configuration — typed settings loaded from .env for the pipeline."""
from __future__ import annotations
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Resolved configuration for the Graphiti pipeline (Neo4j + OpenRouter)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    api_key: str = Field("", validation_alias="OPENAI_API_KEY")
    base_url: str = Field("https://openrouter.ai/api/v1", validation_alias="OPENAI_BASE_URL")
    llm_model: str = Field("openai/gpt-oss-120b", validation_alias="LLM_MODEL")
    small_model: str | None = Field(None, validation_alias="LLM_SMALL_MODEL")
    embedding_model: str = Field("openai/text-embedding-3-small", validation_alias="EMBEDDING_MODEL")
    embedding_dim: int = Field(1536, validation_alias="EMBEDDING_DIM")
    # Embeddings work through OpenRouter, but allow routing them elsewhere if needed.
    embed_api_key: str | None = Field(None, validation_alias="EMBED_API_KEY")
    embed_base_url: str | None = Field(None, validation_alias="EMBED_BASE_URL")


env = Settings()