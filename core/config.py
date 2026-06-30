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

    # Deep researcher — its own model, independent of the GraphRAG indexing/search models. It governs
    # only the LangGraph orchestration LLM calls (plan queries, distil answers, synthesize the report);
    # the GraphRAG searches the loop runs still use settings.yaml. `research_small_model` handles the hot
    # path (distillation, run breadth×depth times) and falls back to `research_model` when unset.
    research_model: str = Field("deepseek/deepseek-v4-flash", validation_alias="RESEARCH_MODEL")
    research_small_model: str | None = Field(None, validation_alias="RESEARCH_SMALL_MODEL")

    # OpenAlex — the deep researcher's scholarly source (free, keyless). `openalex_mailto` opts into
    # the faster "polite pool"; set it to a contact email in .env. per_page caps works per sub-query.
    openalex_base_url: str = Field("https://api.openalex.org", validation_alias="OPENALEX_BASE_URL")
    openalex_mailto: str | None = Field(None, validation_alias="OPENALEX_MAILTO")
    openalex_per_page: int = Field(5, validation_alias="OPENALEX_PER_PAGE")

    # Full-text acquisition for the deep-read tier. The researcher first tries to fetch+parse a work's
    # OA url itself (httpx + pypdf/trafilatura); when that's blocked (403) or yields too little, it falls
    # back to this reader service, which emulates a browser and returns clean text. Jina Reader's keyless
    # endpoint is the default (prefix the target url). Set empty to disable the fallback (degrade to abstract).
    reader_service_url: str | None = Field("https://r.jina.ai/", validation_alias="READER_SERVICE_URL")


env = Settings()