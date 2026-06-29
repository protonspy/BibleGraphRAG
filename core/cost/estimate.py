"""Cost estimation — approximate USD to run the full corpus through the GraphRAG pipeline.

Counts corpus tokens with tiktoken, prices them against live OpenRouter rates
(falling back to a builtin table), and applies a configurable overhead multiplier
to account for GraphRAG making several LLM calls over the corpus: graph extraction
(one call per chunk plus gleanings), description summarization, and community-report
generation (one call per community, re-sending entity/relationship descriptions), each
with large system prompts. Embeddings cover text units, entity descriptions, and reports.

Estimate only — the ground-truth cost is OpenRouter's returned usage.cost.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import tiktoken

from core.config import env
from core.pipe.parser import load_records

MODELS_ENDPOINT = "/models"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# Embedding models are absent from the bulk /models list but ARE priced via the
# per-model /models/{id}/endpoints route (see fetch_endpoint_pricing). This table is
# only a last resort for offline / --no-fetch runs. Per-token USD; embeddings bill input only.
FALLBACK_PRICING: dict[str, dict[str, float]] = {
    "openai/gpt-4o": {"prompt": 2.5e-6, "completion": 1.0e-5},
    "openai/gpt-4o-mini": {"prompt": 1.5e-7, "completion": 6.0e-7},
    "openai/text-embedding-3-small": {"prompt": 2.0e-8, "completion": 0.0},
    "openai/text-embedding-3-large": {"prompt": 1.3e-7, "completion": 0.0},
    "openai/text-embedding-ada-002": {"prompt": 1.0e-7, "completion": 0.0},
}

# GraphRAG re-reads the corpus across extraction (chunk + gleanings), summarization, and
# community reports, each with a large system prompt — so input dwarfs the raw corpus. These are
# deliberately rough; override with --input-factor / --output-ratio / --embed-factor and trust
# OpenRouter's usage.cost for the real number.
DEFAULT_INPUT_FACTOR = 8.0   # LLM input tokens per corpus token (prompts + context, repeated per call)
DEFAULT_OUTPUT_RATIO = 0.5   # LLM output tokens as a fraction of corpus tokens (descriptions + reports)
DEFAULT_EMBED_FACTOR = 1.5   # embedding tokens as a multiple of corpus tokens (text units + entities + reports)


def encoding_for(model: str) -> tiktoken.Encoding:
    """tiktoken encoding for a model id, stripping any 'provider/' prefix.

    Falls back to o200k_base (the GPT-4o family encoding) for unknown models.
    """
    name = model.split("/")[-1]
    try:
        return tiktoken.encoding_for_model(name)
    except KeyError:
        return tiktoken.get_encoding("o200k_base")


def count_corpus_tokens(target: str, model: str) -> tuple[Path, int, int]:
    """Return (path, verse_count, token_count) for the verse content of the parsed corpus.

    Reads the records written by `bgr parser` (output/<target>[-N].json).
    """
    records, path = load_records(target)
    enc = encoding_for(model)
    tokens = sum(len(enc.encode(rec["content"])) for rec in records)
    return path, len(records), tokens


def fetch_pricing(base_url: str, timeout: float = 15.0) -> dict[str, dict[str, float]]:
    """Fetch per-token pricing from {base_url}/models. Returns {} on failure."""
    url = base_url.rstrip("/") + MODELS_ENDPOINT
    req = urllib.request.Request(url, headers={"User-Agent": "biblegraphrag-estimate"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"warning: could not fetch live pricing ({exc}); using fallback table", file=sys.stderr)
        return {}
    pricing: dict[str, dict[str, float]] = {}
    for entry in payload.get("data", []):
        raw = entry.get("pricing") or {}
        try:
            pricing[entry["id"]] = {
                "prompt": float(raw.get("prompt") or 0.0),
                "completion": float(raw.get("completion") or 0.0),
            }
        except (TypeError, ValueError):
            continue
    return pricing


def fetch_endpoint_pricing(base_url: str, model_id: str, timeout: float = 15.0) -> dict[str, float] | None:
    """Fetch pricing for a single model via {base_url}/models/{id}/endpoints.

    Covers models missing from the bulk /models list (e.g. embeddings). Returns the
    cheapest endpoint's per-token price, or None on failure.
    """
    url = f"{base_url.rstrip('/')}{MODELS_ENDPOINT}/{model_id}/endpoints"
    req = urllib.request.Request(url, headers={"User-Agent": "biblegraphrag-estimate"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    endpoints = (payload.get("data") or {}).get("endpoints") or []
    prices: list[dict[str, float]] = []
    for ep in endpoints:
        raw = ep.get("pricing") or {}
        try:
            prices.append({"prompt": float(raw.get("prompt") or 0.0),
                           "completion": float(raw.get("completion") or 0.0)})
        except (TypeError, ValueError):
            continue
    return min(prices, key=lambda p: p["prompt"]) if prices else None


def resolve_price(
    model: str, fetched: dict[str, dict[str, float]], base_url: str, fetch: bool
) -> tuple[dict[str, float] | None, str | None]:
    """Resolve per-token pricing, returning (price, source).

    Order: bulk /models list -> per-model /endpoints route -> builtin fallback table.
    """
    if model in fetched:
        return fetched[model], "live"
    if fetch:
        live = fetch_endpoint_pricing(base_url, model)
        if live is not None:
            return live, "live /endpoints"
    price = FALLBACK_PRICING.get(model)
    return price, ("fallback" if price is not None else None)


def _fmt_usd(value: float) -> str:
    return f"${value:,.2f}" if value >= 0.01 else f"${value:.4f}"


def _per_million(price_per_token: float) -> str:
    return f"${price_per_token * 1e6:,.2f}/M"


def run(
    target: str,
    input_factor: float = DEFAULT_INPUT_FACTOR,
    output_ratio: float = DEFAULT_OUTPUT_RATIO,
    embed_factor: float = DEFAULT_EMBED_FACTOR,
    model_override: str | None = None,
    fetch: bool = True,
) -> int:
    """Estimate and print the USD cost of processing data/<target>.txt end to end."""
    base_url = env.base_url
    llm_model = model_override or env.llm_model
    small_model = env.small_model
    embed_model = env.embedding_model

    try:
        corpus_path, verses, corpus_tok = count_corpus_tokens(target, llm_model)
    except FileNotFoundError as exc:
        print(f"error: parsed corpus not found: {exc} (run `bgr parser --target {target}` first)",
              file=sys.stderr)
        return 1

    fetched = fetch_pricing(base_url) if fetch else {}
    llm_price, llm_src = resolve_price(llm_model, fetched, base_url, fetch)
    small_price, small_src = resolve_price(small_model, fetched, base_url, fetch)
    embed_price, embed_src = resolve_price(embed_model, fetched, base_url, fetch)

    missing = [m for m, p in ((llm_model, llm_price), (embed_model, embed_price)) if p is None]
    if missing:
        print(f"error: no pricing found for: {', '.join(missing)} (try --no-fetch or extend FALLBACK_PRICING)",
              file=sys.stderr)
        return 1

    in_tok = corpus_tok * input_factor
    out_tok = corpus_tok * output_ratio
    emb_tok = corpus_tok * embed_factor

    def llm_cost(price: dict[str, float]) -> float:
        return in_tok * price["prompt"] + out_tok * price["completion"]

    embed_cost = emb_tok * embed_price["prompt"]
    total = llm_cost(llm_price) + embed_cost

    enc_name = encoding_for(llm_model).name

    print("BibleGraphRAG — cost estimate (full corpus processing)")
    print(f"Corpus : {corpus_path} — {verses:,} verses, "
          f"{corpus_tok:,} tokens (tiktoken {enc_name})")
    print()
    print(f"Assumptions: input_factor={input_factor}  output_ratio={output_ratio}  embed_factor={embed_factor}")
    print("Prices (per token; source on the right):")
    print(f"  LLM    {llm_model:<32} in {_per_million(llm_price['prompt'])}  out {_per_million(llm_price['completion'])}  [{llm_src}]")
    if small_price:
        print(f"  small  {small_model:<32} in {_per_million(small_price['prompt'])}  out {_per_million(small_price['completion'])}  [{small_src}]")
    print(f"  embed  {embed_model:<32} in {_per_million(embed_price['prompt'])}  [{embed_src}]")
    print()
    print(f"Estimate (main model = {llm_model}):")
    print(f"  LLM input   {in_tok:>14,.0f} tok × {_per_million(llm_price['prompt'])}  = {_fmt_usd(in_tok * llm_price['prompt'])}")
    print(f"  LLM output  {out_tok:>14,.0f} tok × {_per_million(llm_price['completion'])}  = {_fmt_usd(out_tok * llm_price['completion'])}")
    print(f"  Embeddings  {emb_tok:>14,.0f} tok × {_per_million(embed_price['prompt'])}  = {_fmt_usd(embed_cost)}")
    print(f"  {'-' * 50}")
    print(f"  TOTAL ≈ {_fmt_usd(total)}")
    if small_price:
        print()
        print(f"  (with main model {small_model}: ≈ {_fmt_usd(llm_cost(small_price) + embed_cost)})")
    print()
    print("⚠ Estimate. GraphRAG overhead approximated via multiplier;")
    print("  the real cost is the usage.cost returned by OpenRouter.")
    return 0
