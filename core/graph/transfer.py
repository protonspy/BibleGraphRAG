"""Export/import the Neo4j graph as JSON — a lossless, schema-agnostic dump.

`export` reads the whole graph into export/<name>-<N>.json; `import` MERGEs that JSON back into
Neo4j keyed by each node/edge `id`. Both run on the raw Neo4j driver (no GraphRAG/LLM stack) —
this is backup/restore of the loaded mirror (see core.graph.load_neo4j), not ingestion.

The dump is deliberately generic — `MATCH (n)` / `MATCH (a)-[r]->(b)` rather than a fixed label
list — so it survives schema growth. The only assumption is that every node carries a stable `id`
(the GraphRAG entity/community/document/chunk id) used as the join key. Structural edges without an
id (PART_OF / HAS_ENTITY / IN_COMMUNITY) are matched on their endpoints + type instead.

Conventions mirror the parser: files live under export/, numbered <name>-<N>.json and never
overwritten on export; import resolves export/<name>.json or the highest-N file.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase
from neo4j.time import Date, DateTime, Time

from core.config import env
from core.graph.neo4j_util import quiet_neo4j_logging

EXPORT_DIR = Path("export")

# GraphRAG writes dates (documents.creation_date, communities.period) as plain ISO strings, not
# Neo4j temporal types, so nothing needs reviving on import. Kept empty (rather than deleted) so the
# generic _to_jsonable/_restore_value round-trip stays in place if a temporal property is ever added.
TEMPORAL_KEYS: set[str] = set()

# Label / relationship-type names are injected into Cypher (they can't be parameterized), so
# they must be plain identifiers — guards against a tampered export file injecting Cypher.
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# --- serialization helpers -------------------------------------------------------------

def _to_jsonable(value: Any) -> Any:
    """Recursively convert Neo4j-returned values into JSON-serializable ones.

    Only temporal types need translating; Neo4j properties are otherwise primitives or
    arrays of primitives (it forbids nested maps), so deep recursion is just defensive.
    """
    if isinstance(value, (DateTime, Date, Time)):
        return value.to_native().isoformat()
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _clean_props(props: dict[str, Any], embeddings: bool) -> dict[str, Any]:
    """JSON-ify property values, optionally dropping large *_embedding vectors."""
    out: dict[str, Any] = {}
    for key, value in props.items():
        if not embeddings and key.endswith("_embedding"):
            continue
        out[key] = _to_jsonable(value)
    return out


def _restore_value(key: str, value: Any) -> Any:
    """Inverse of _to_jsonable for import: revive temporal strings as Python datetimes.

    The driver maps a Python datetime back to a Neo4j DateTime on write, so the round-trip
    preserves the original type. Non-temporal keys (and unparseable strings) pass through.
    """
    if key in TEMPORAL_KEYS and isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def _validate_ident(name: str) -> str:
    if not isinstance(name, str) or not _SAFE_IDENT.match(name):
        raise ValueError(f"unsafe label/relationship type in export file: {name!r}")
    return name


# --- path resolution (export/ mirror of the parser's output/ scheme) -------------------

def _next_export_path(name: str) -> Path:
    """Return export/<name>-<N>.json with the smallest free N (never overwrites)."""
    n = 1
    while (EXPORT_DIR / f"{name}-{n}.json").exists():
        n += 1
    return EXPORT_DIR / f"{name}-{n}.json"


def _resolve_export_path(target: str) -> Path | None:
    """Resolve a target to a dump file: export/<target>.json, else the highest-N variant."""
    direct = EXPORT_DIR / f"{target}.json"
    if direct.is_file():
        return direct
    numbered: list[tuple[int, Path]] = []
    for path in EXPORT_DIR.glob(f"{target}-*.json"):
        suffix = path.stem[len(target) + 1:]
        if suffix.isdigit():
            numbered.append((int(suffix), path))
    return max(numbered)[1] if numbered else None


# --- export ----------------------------------------------------------------------------

def export_run(
    target: str,
    group_id: str | None = None,
    embeddings: bool = True,
    dry_run: bool = False,
) -> int:
    """Dump the graph (or one group_id namespace) to export/<target>-<N>.json."""
    quiet_neo4j_logging()
    node_where = "WHERE n.group_id = $gid" if group_id else ""
    rel_where = "WHERE a.group_id = $gid AND b.group_id = $gid" if group_id else ""

    driver = GraphDatabase.driver(env.neo4j_uri, auth=(env.neo4j_user, env.neo4j_password))
    nodes: list[dict] = []
    node_ids: set[str] = set()
    relationships: list[dict] = []
    skipped_nodes = 0
    dangling_rels = 0
    try:
        with driver.session() as sess:
            for r in sess.run(
                f"MATCH (n) {node_where} " # type: ignore
                "RETURN n.id AS id, labels(n) AS labels, properties(n) AS props",
                gid=group_id,
            ):
                node_id = r["id"]
                if node_id is None:  # every GraphRAG-loaded node has one; skip anything that doesn't
                    skipped_nodes += 1
                    continue
                nodes.append({
                    "id": node_id,
                    "labels": r["labels"],
                    "properties": _clean_props(r["props"], embeddings),
                })
                node_ids.add(node_id)

            for r in sess.run(
                f"MATCH (a)-[r]->(b) {rel_where} " # type: ignore
                "RETURN type(r) AS type, r.id AS id, a.id AS start, b.id AS end, "
                "properties(r) AS props",
                gid=group_id,
            ):
                start, end = r["start"], r["end"]
                if start not in node_ids or end not in node_ids:
                    dangling_rels += 1  # endpoint outside the exported scope
                    continue
                relationships.append({
                    "id": r["id"],
                    "type": r["type"],
                    "start": start,
                    "end": end,
                    "properties": _clean_props(r["props"], embeddings),
                })
    finally:
        driver.close()

    scope = f"group_id '{group_id}'" if group_id else "whole graph"
    if skipped_nodes:
        print(f"warning: skipped {skipped_nodes} node(s) without an id", file=sys.stderr)
    if dangling_rels:
        print(f"warning: skipped {dangling_rels} relationship(s) crossing the export scope",
              file=sys.stderr)

    if dry_run:
        extra = "" if embeddings else ", embeddings stripped"
        print(f"dry-run: would export {len(nodes)} nodes, {len(relationships)} relationships "
              f"({scope}{extra})")
        return 0

    payload = {
        "meta": {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "neo4j_uri": env.neo4j_uri,
            "group_id": group_id,
            "embeddings": embeddings,
            "node_count": len(nodes),
            "relationship_count": len(relationships),
        },
        "nodes": nodes,
        "relationships": relationships,
    }
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _next_export_path(target)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"exported {len(nodes)} nodes, {len(relationships)} relationships ({scope}) -> {out_path}",
          file=sys.stderr)
    return 0


# --- import ----------------------------------------------------------------------------

def _group_nodes(nodes: list[dict]) -> dict[tuple[str, ...], list[dict]]:
    """Bucket nodes by their (validated) label set — labels can't be parameterized."""
    buckets: dict[tuple[str, ...], list[dict]] = {}
    for node in nodes:
        labels = tuple(_validate_ident(l) for l in node.get("labels", []))
        props = {k: _restore_value(k, v) for k, v in node["properties"].items()}
        buckets.setdefault(labels, []).append({"id": node["id"], "props": props}) # type: ignore
    return buckets


def _group_rels(rels: list[dict]) -> dict[tuple[str, bool], list[dict]]:
    """Bucket relationships by (validated type, has-id) — type can't be parameterized."""
    buckets: dict[tuple[str, bool], list[dict]] = {}
    for rel in rels:
        rtype = _validate_ident(rel["type"])
        props = {k: _restore_value(k, v) for k, v in rel["properties"].items()}
        row = {"id": rel.get("id"), "start": rel["start"], "end": rel["end"], "props": props}
        buckets.setdefault((rtype, rel.get("id") is not None), []).append(row) # type: ignore
    return buckets


def import_run(target: str, wipe: bool = False, dry_run: bool = False) -> int:
    """Load export/<target>[-N].json into Neo4j, upserting nodes/edges by id."""
    path = _resolve_export_path(target)
    if path is None:
        print(f"error: export not found: {EXPORT_DIR / (target + '.json')} "
              f"(run `bgr transfer export --target {target}` first)", file=sys.stderr)
        return 1

    payload = json.loads(path.read_text(encoding="utf-8"))
    nodes = payload.get("nodes", [])
    relationships = payload.get("relationships", [])
    group_id = payload.get("meta", {}).get("group_id")

    try:
        node_buckets = _group_nodes(nodes)
        rel_buckets = _group_rels(relationships)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    wipe_scope = (f"group_id '{group_id}'" if group_id else "whole graph") if wipe else None
    if dry_run:
        wipe_note = f", wipe {wipe_scope} first" if wipe else ""
        print(f"dry-run: would import {len(nodes)} nodes ({len(node_buckets)} label-sets), "
              f"{len(relationships)} relationships ({len(rel_buckets)} type-sets) "
              f"from {path}{wipe_note}")
        return 0

    quiet_neo4j_logging()
    driver = GraphDatabase.driver(env.neo4j_uri, auth=(env.neo4j_user, env.neo4j_password))
    try:
        with driver.session() as sess:
            if wipe:
                if group_id:
                    deleted = sess.run(
                        "MATCH (n {group_id: $gid}) DETACH DELETE n RETURN count(n) AS c",
                        gid=group_id,
                    ).single()["c"]
                else:
                    deleted = sess.run("MATCH (n) DETACH DELETE n RETURN count(n) AS c").single()["c"]
                print(f"wiped {deleted} nodes ({wipe_scope})", file=sys.stderr)

            # Nodes: MERGE on id only (stable across label changes), then overwrite props and
            # re-apply the label set. Labels are static per bucket, so this is injection-safe.
            for labels, rows in node_buckets.items():
                label_clause = f" SET x{''.join(f':{l}' for l in labels)}" if labels else ""
                sess.run(
                    "UNWIND $rows AS row " # type: ignore
                    "MERGE (x {id: row.id}) "
                    "SET x = row.props" + label_clause,
                    rows=rows,
                )

            # Relationships: edges with an id MERGE on it; structural edges (no id) MERGE on
            # endpoints + type. Both then overwrite properties.
            for (rtype, has_id), rows in rel_buckets.items():
                merge = (f"MERGE (a)-[e:{rtype} {{id: row.id}}]->(b)" if has_id
                         else f"MERGE (a)-[e:{rtype}]->(b)")
                sess.run(
                    "UNWIND $rows AS row " # type: ignore
                    "MATCH (a {id: row.start}), (b {id: row.end}) "
                    f"{merge} "
                    "SET e = row.props",
                    rows=rows,
                )
    finally:
        driver.close()

    print(f"imported {len(nodes)} nodes, {len(relationships)} relationships from {path}",
          file=sys.stderr)
    return 0
