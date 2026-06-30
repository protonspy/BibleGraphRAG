"""Shared configuration — typed settings loaded from .env for the pipeline."""
from __future__ import annotations
from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Resolved configuration for the GraphRAG pipeline (parquet + lancedb + OpenRouter)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # Root of the Microsoft GraphRAG workspace (settings.yaml + prompts/ + input/ + output/).
    # `bgr build` writes input here and indexes via graphrag.api, producing output/*.parquet and the
    # output/lancedb vector tables. Not named "graphrag" so it can't shadow the installed package.
    # Resolved to an ABSOLUTE path (see below) because graphrag's load_config does os.chdir(root) — a
    # relative rag_root would otherwise resolve against the changed cwd in any step run after indexing.
    rag_root: Path = Field(Path("rag"), validation_alias="RAG_ROOT")

    @field_validator("rag_root", mode="after")
    @classmethod
    def _absolute_rag_root(cls, value: Path) -> Path:
        return value.resolve()

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