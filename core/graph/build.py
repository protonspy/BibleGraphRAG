"""Ingest a parsed corpus into Graphiti — one episode per chapter.

reference_time carries no real-world meaning for this corpus; we derive a monotonic
timestamp from each episode's canonical index so Graphiti keeps narrative order on edges.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from graphiti_core.nodes import EpisodeType

from core.config import env
from core.graph.client import build_graphiti, quiet_neo4j_logging
from core.graph.entities import ENTITY_TYPES
from core.graph.episodes import Episode, build_episodes, translation_label
from core.pipe.parser import load_records

# Arbitrary epoch; only the ordering of reference_time across episodes matters.
BASE_TIME = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _select(episodes: list[Episode], book: str | None, chapter: int | None, limit: int | None) -> list[Episode]:
    if book is not None:
        book_lower = book.lower()
        episodes = [e for e in episodes if e.book.lower() == book_lower]
    if chapter is not None:
        episodes = [e for e in episodes if e.chapter == chapter]
    if limit is not None:
        episodes = episodes[:limit]
    return episodes


async def _ingest(
    target: str,
    book: str | None,
    chapter: int | None,
    limit: int | None,
    group_id: str | None,
    dry_run: bool,
) -> int:
    try:
        records, path = load_records(target)
    except FileNotFoundError as exc:
        print(f"error: parsed corpus not found: {exc} (run `bgr parser --target {target}` first)", file=sys.stderr)
        return 1

    translation = translation_label(path.stem)
    episodes = _select(build_episodes(records, translation), book, chapter, limit)
    group = group_id or translation

    if not episodes:
        print("error: no episodes match the given filters", file=sys.stderr)
        return 1

    total_verses = sum(e.verses for e in episodes)
    print(f"Corpus  : {path} (translation '{translation}', group_id '{group}')")
    print(f"Episodes: {len(episodes)} chapters, {total_verses:,} verses "
          f"[{episodes[0].name} … {episodes[-1].name}]")

    if dry_run:
        for e in episodes[:5]:
            print(f"  {e.name}: {e.verses} verses, {len(e.body)} chars")
        if len(episodes) > 5:
            print(f"  … (+{len(episodes) - 5} more)")
        print("dry-run: nothing ingested")
        return 0

    if not env.api_key or env.api_key.startswith("your_"):
        print("error: OPENAI_API_KEY is not set in .env", file=sys.stderr)
        return 1

    quiet_neo4j_logging()
    graphiti = build_graphiti(env)
    failures = 0
    try:
        await graphiti.build_indices_and_constraints()
        for i, e in enumerate(episodes, 1):
            try:
                await graphiti.add_episode(
                    name=e.name,
                    episode_body=e.body,
                    source_description=e.source_description,
                    reference_time=BASE_TIME + timedelta(seconds=e.index),
                    source=EpisodeType.text,
                    group_id=group,
                    entity_types=ENTITY_TYPES,
                )
                print(f"[{i}/{len(episodes)}] {e.name}: ok")
            except Exception as exc:  # one bad chapter shouldn't abort the run
                failures += 1
                print(f"[{i}/{len(episodes)}] {e.name}: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        await graphiti.close()

    print(f"done: {len(episodes) - failures}/{len(episodes)} episodes ingested"
          + (f", {failures} failed" if failures else ""))
    return 1 if failures else 0


def run(
    target: str,
    book: str | None = None,
    chapter: int | None = None,
    limit: int | None = None,
    group_id: str | None = None,
    dry_run: bool = False,
) -> int:
    """Build the knowledge graph for data parsed under output/<target>[-N].json."""
    return asyncio.run(_ingest(target, book, chapter, limit, group_id, dry_run))
