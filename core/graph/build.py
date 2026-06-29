"""Build the knowledge graph from a parsed corpus via Microsoft GraphRAG, into Neo4j.

Phases, each skippable:
  1. prepare — segment into pericopes (cached) and write <rag_root>/input/<translation>.csv
  2. index   — graphrag.api.build_index in-process (core.graph.engine); produces output/*.parquet and,
               through our Neo4j vector store (core.graph.neo4j_vectors), writes entity embeddings
               straight into Neo4j instead of lancedb
  3. load    — mirror the parquet graph + community reports into Neo4j (core.graph.load_neo4j)
  4. embed   — ensure the entity vector index and backfill any embeddings the store didn't write
               (core.graph.embed)

GraphRAG's pipeline cache (<rag_root>/cache/) makes re-runs cheap; incremental additions via
`graphrag update`. `bgr query` runs GraphRAG's own search engine over the parquet output, with the
entity embeddings served from the Neo4j vector index this build writes; no lancedb.
"""
from __future__ import annotations

import sys

from core.config import env
from core.graph.episodes import (
    Episode,
    build_episodes,
    build_graphrag_input,
    build_pericope_episodes,
    translation_label,
)
from core.pipe import pericope as pericope_step
from core.pipe.parser import load_records


def _select(
    episodes: list[Episode],
    book: str | None,
    chapter: int | None,
    chapters: set[int] | None,
    limit: int | None,
) -> list[Episode]:
    if book is not None:
        book_lower = book.lower()
        episodes = [e for e in episodes if e.book.lower() == book_lower]
    if chapter is not None:
        episodes = [e for e in episodes if e.chapter == chapter]
    if chapters is not None:
        episodes = [e for e in episodes if e.chapter in chapters]
    if limit is not None:
        episodes = episodes[:limit]
    return episodes


def run(
    target: str,
    book: str | None = None,
    chapter: int | None = None,
    chapters: set[int] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    pericope: bool = True,
    skip_index: bool = False,
    skip_load: bool = False,
    verbose: bool = False,
) -> int:
    """Run prepare → index → load for data parsed under output/<target>[-N].json."""
    try:
        records, path = load_records(target)
    except FileNotFoundError as exc:
        print(f"error: parsed corpus not found: {exc} (run `bgr parser --target {target}` first)", file=sys.stderr)
        return 1

    translation = translation_label(path.stem)

    if pericope:
        # Cache-first: reuse segmented pericopes; segment (and cache) only what's missing.
        # dry-run never calls the LLM — it previews from whatever is already cached.
        if dry_run:
            pericope_map = pericope_step.load_cache(translation).get("chapters", {})
        else:
            try:
                pericope_map = pericope_step.ensure_cache(
                    records, translation, book=book, chapter=chapter, chapters=chapters, limit=limit
                )
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        all_episodes = build_pericope_episodes(records, translation, pericope_map)
    else:
        all_episodes = build_episodes(records, translation)

    episodes = _select(all_episodes, book, chapter, chapters, limit)
    if not episodes:
        print("error: no episodes match the given filters", file=sys.stderr)
        return 1

    total_verses = sum(e.verses for e in episodes)
    grain = "pericope" if pericope else "chapter"
    rag_root = env.rag_root
    print(f"Corpus  : {path} (translation '{translation}', rag_root '{rag_root}')")
    print(f"Episodes: {len(episodes)} ({grain}), {total_verses:,} verses "
          f"[{episodes[0].name} … {episodes[-1].name}]")

    if dry_run:
        for e in episodes[:5]:
            print(f"  {e.name}: {e.verses} verses, {len(e.body)} chars")
        if len(episodes) > 5:
            print(f"  … (+{len(episodes) - 5} more)")
        plan = [f"write {rag_root / 'input' / (translation + '.csv')}"]
        plan.append("skip index" if skip_index else "run `graphrag index`")
        plan.append("skip load" if skip_load else "load Neo4j")
        print("dry-run: would " + ", then ".join(plan))
        return 0

    if not env.api_key or env.api_key.startswith("your_"):
        print("error: OPENAI_API_KEY is not set in .env", file=sys.stderr)
        return 1

    # Phase 1 — prepare the GraphRAG input CSV.
    csv_path = build_graphrag_input(episodes, translation, rag_root)
    print(f"prepare : wrote {len(episodes)} rows -> {csv_path}")

    # Phase 2 — index (batch), in-process via graphrag.api. Imported lazily so prepare-only and
    # --skip-index runs don't pull in graphrag/pandas.
    if not skip_index:
        print("index   : graphrag.api.build_index (in-process)")
        from core.graph import engine

        rc = engine.index(verbose=verbose)
        if rc != 0:
            print(f"error: graphrag indexing failed (exit {rc})", file=sys.stderr)
            return rc
    else:
        print("index   : skipped (--skip-index)")

    # Phase 3 — mirror the parquet output into Neo4j, then embed entities for local search.
    # Imported lazily so prepare/index runs (and --skip-load) don't pull in the Neo4j driver.
    if not skip_load:
        from core.graph import embed, load_neo4j

        rc = load_neo4j.run(rag_root)
        if rc != 0:
            return rc
        return embed.run()
    print("load    : skipped (--skip-load)")
    return 0
