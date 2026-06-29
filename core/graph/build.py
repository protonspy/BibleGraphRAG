"""Ingest a parsed corpus into Graphiti — one episode per pericope by default, per chapter with --no-pericope.

reference_time carries no real-world meaning for this corpus; we derive a monotonic
timestamp from each episode's canonical index so Graphiti keeps narrative order on edges.

Resume/checkpoint: the graph itself is the checkpoint. Each successful add_episode leaves an
Episodic node, so on a re-run we skip episodes whose node already exists in the group and
ingest only the rest — which includes episodes that errored mid-run and never saved. This
makes interrupted runs cheap to continue without re-paying for completed work.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from graphiti_core.nodes import EpisodeType
from neo4j import GraphDatabase

from core.config import env
from core.graph.client import build_graphiti, quiet_neo4j_logging
from core.graph.entities import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES
from core.graph.episodes import (
    Episode,
    build_episodes,
    build_pericope_episodes,
    translation_label,
)
from core.pipe import enrich as enrich_step
from core.pipe import pericope as pericope_step
from core.pipe.parser import load_records

# Arbitrary epoch; only the ordering of reference_time across episodes matters.
BASE_TIME = datetime(1970, 1, 1, tzinfo=timezone.utc)


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


def _ingested_episode_names(group: str) -> set[str]:
    """Names of episodes already ingested into `group` — the resume checkpoint.

    The graph is the source of truth: a successful add_episode leaves an Episodic node, so
    its absence means the episode never completed (new, or errored mid-run) and is pending.
    """
    driver = GraphDatabase.driver(env.neo4j_uri, auth=(env.neo4j_user, env.neo4j_password))
    try:
        with driver.session() as sess:
            rows = sess.run("MATCH (e:Episodic {group_id: $group}) RETURN e.name AS name", group=group)
            return {r["name"] for r in rows}
    finally:
        driver.close()


def _resolve_resume(resume: bool | None, done: int, total: int) -> bool:
    """Whether to skip already-ingested episodes. None => ask interactively (Y default)."""
    if resume is not None:
        return resume
    prompt = f"Checkpoint: {done}/{total} episodes already ingested. Resume (skip them)? [Y/n] "
    try:
        answer = input(prompt).strip().lower()
    except EOFError:  # non-interactive (piped/background) — resume rather than redo work
        print("non-interactive input; defaulting to resume", file=sys.stderr)
        return True
    return answer in ("", "y", "yes")


async def _ingest(
    target: str,
    book: str | None,
    chapter: int | None,
    chapters: set[int] | None,
    limit: int | None,
    group_id: str | None,
    dry_run: bool,
    pericope: bool,
    resume: bool | None,
    enrich: bool,
) -> int:
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
    group = group_id or translation

    if not episodes:
        print("error: no episodes match the given filters", file=sys.stderr)
        return 1

    total_verses = sum(e.verses for e in episodes)
    grain = "pericope" if pericope else "chapter"
    print(f"Corpus  : {path} (translation '{translation}', group_id '{group}')")
    print(f"Episodes: {len(episodes)} ({grain}), {total_verses:,} verses "
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

    # Resume/checkpoint: skip episodes already present in the graph for this group.
    done_names = _ingested_episode_names(group)
    already = [e for e in episodes if e.name in done_names]
    if already:
        if _resolve_resume(resume, len(already), len(episodes)):
            episodes = [e for e in episodes if e.name not in done_names]
            print(f"resume: skipping {len(already)} already-ingested episode(s), {len(episodes)} to go")
        else:
            print(f"re-ingesting all {len(episodes)} episode(s); existing nodes for "
                  f"'{group}' may be duplicated unless the group was wiped")
        if not episodes:
            print("nothing to do: every selected episode is already in the graph")
            return 0

    # Enrichment: per-chapter extraction guidance (cache-first), applied to each pericope below.
    guidance: dict[str, str] = {}
    if enrich:
        try:
            guidance = enrich_step.ensure_guidance(
                records, translation, book=book, chapter=chapter, chapters=chapters, limit=limit
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

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
                    edge_types=EDGE_TYPES,
                    edge_type_map=EDGE_TYPE_MAP,
                    custom_extraction_instructions=guidance.get(pericope_step.chapter_key(e.book, e.chapter)),
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
    chapters: set[int] | None = None,
    limit: int | None = None,
    group_id: str | None = None,
    dry_run: bool = False,
    pericope: bool = True,
    resume: bool | None = None,
    enrich: bool = True,
) -> int:
    """Build the knowledge graph for data parsed under output/<target>[-N].json."""
    return asyncio.run(
        _ingest(target, book, chapter, chapters, limit, group_id, dry_run, pericope, resume, enrich)
    )
