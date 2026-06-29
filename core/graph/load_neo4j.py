"""Project a GraphRAG parquet index (<rag_root>/output) into Neo4j — the query store.

GraphRAG indexes to parquet (its working format); this loader projects the final tables into a
property graph, and `bgr query` (core.graph.search) then reads Neo4j directly. Entity embeddings
land on these same nodes — written during indexing by core.graph.neo4j_vectors and backfilled by
core.graph.embed. The conventional GraphRAG→Neo4j schema keeps the existing tooling (inspect /
transfer) working:

    (:__Document__)<-[:PART_OF]-(:__Chunk__)-[:HAS_ENTITY]->(:__Entity__)
    (:__Entity__)-[:RELATED {weight, description}]->(:__Entity__)
    (:__Entity__)-[:IN_COMMUNITY]->(:__Community__)-[:IN_COMMUNITY]->(:__Community__ parent)

Every write MERGEs by a stable id, so re-loading the same index is idempotent. Column names were
confirmed against GraphRAG 3.1.0 output (entities/relationships/communities/community_reports/
text_units/documents.parquet).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.graph.neo4j_util import batched_write, quiet_neo4j_logging, session

# Final tables emitted by `graphrag index`. covariates.parquet is omitted (claims disabled).
_TABLES = ("documents", "text_units", "entities", "relationships", "communities", "community_reports")


def _clean(value: Any) -> Any:
    """Convert a pandas/numpy cell into a Neo4j-storable value (primitive or list of primitives)."""
    if isinstance(value, np.ndarray):
        return [_clean(v) for v in value.tolist()]
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):  # safe: arrays handled above
        return None
    return value


def _records(df: pd.DataFrame, columns: list[str]) -> list[dict]:
    """Project the given columns of `df` into a list of cleaned, driver-ready row dicts."""
    present = [c for c in columns if c in df.columns]
    return [{c: _clean(row[c]) for c in present} for _, row in df.iterrows()]


def _read_tables(output_dir: Path) -> dict[str, pd.DataFrame] | None:
    """Load the required parquet tables, or None (with an error) if the index is missing."""
    missing = [t for t in _TABLES if not (output_dir / f"{t}.parquet").is_file()]
    if missing:
        print(f"error: GraphRAG output not found in {output_dir} (missing {', '.join(missing)}); "
              f"run `bgr build` without --skip-index first", file=sys.stderr)
        return None
    return {t: pd.read_parquet(output_dir / f"{t}.parquet") for t in _TABLES}


def run(rag_root: Path) -> int:
    """Load <rag_root>/output/*.parquet into Neo4j. Returns a process exit code."""
    output_dir = Path(rag_root) / "output"
    tables = _read_tables(output_dir)
    if tables is None:
        return 1

    quiet_neo4j_logging()
    docs = tables["documents"]
    chunks = tables["text_units"]
    ents = tables["entities"]
    rels = tables["relationships"]
    coms = tables["communities"]
    reps = tables["community_reports"]

    with session() as sess:
        # Constraints make the MERGEs fast and enforce id uniqueness; the name index backs the
        # entity-by-title lookup that relationships rely on.
        for label in ("__Document__", "__Chunk__", "__Entity__", "__Community__"):
            sess.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE") # type: ignore
        sess.run("CREATE INDEX IF NOT EXISTS FOR (n:__Entity__) ON (n.name)")                   # noqa

        # Documents (one per pericope; id is the canonical reference, e.g. "Genesis 1:1-2").
        n_docs = batched_write(sess,
            "UNWIND $rows AS row MERGE (d:__Document__ {id: row.id}) SET d.title = row.title",
            _records(docs, ["id", "title"]))

        # Chunks (text units) and their PART_OF link to the source document.
        n_chunks = batched_write(sess,
            "UNWIND $rows AS row MERGE (c:__Chunk__ {id: row.id}) "
            "SET c.text = row.text, c.n_tokens = row.n_tokens "
            "WITH c, row WHERE row.document_id IS NOT NULL "
            "MATCH (d:__Document__ {id: row.document_id}) MERGE (c)-[:PART_OF]->(d)",
            _records(chunks, ["id", "text", "n_tokens", "document_id"]))

        # Entities (name = title) and HAS_ENTITY from each chunk the entity appears in.
        n_ents = batched_write(sess,
            "UNWIND $rows AS row MERGE (e:__Entity__ {id: row.id}) "
            "SET e.name = row.title, e.type = row.type, e.description = row.description, "
            "    e.human_readable_id = row.human_readable_id, e.frequency = row.frequency, e.degree = row.degree "
            "WITH e, row UNWIND row.text_unit_ids AS tuid "
            "MATCH (c:__Chunk__ {id: tuid}) MERGE (c)-[:HAS_ENTITY]->(e)",
            _records(ents, ["id", "title", "type", "description", "human_readable_id", "frequency", "degree", "text_unit_ids"]))

        # Relationships: source/target are entity titles (names).
        n_rels = batched_write(sess,
            "UNWIND $rows AS row "
            "MATCH (a:__Entity__ {name: row.source}), (b:__Entity__ {name: row.target}) "
            "MERGE (a)-[r:RELATED {id: row.id}]->(b) "
            "SET r.description = row.description, r.weight = row.weight, r.combined_degree = row.combined_degree",
            _records(rels, ["id", "source", "target", "description", "weight", "combined_degree"]))

        # Communities, entity membership, and the parent hierarchy (parent = -1 means root).
        n_coms = batched_write(sess,
            "UNWIND $rows AS row MERGE (k:__Community__ {id: row.id}) "
            "SET k.community = row.community, k.level = row.level, k.title = row.title, "
            "    k.size = row.size, k.period = row.period, k.parent = row.parent "
            "WITH k, row UNWIND row.entity_ids AS eid "
            "MATCH (e:__Entity__ {id: eid}) MERGE (e)-[:IN_COMMUNITY]->(k)",
            _records(coms, ["id", "community", "level", "title", "size", "period", "parent", "entity_ids"]))
        # Community hierarchy: child community -> parent community (matched by the integer community id).
        batched_write(sess,
            "UNWIND $rows AS row WITH row WHERE row.parent <> -1 "
            "MATCH (child:__Community__ {community: row.community}), (parent:__Community__ {community: row.parent}) "
            "MERGE (child)-[:IN_COMMUNITY]->(parent)",
            _records(coms, ["community", "parent"]))

        # Community reports: attach the report fields onto the matching community node.
        n_reps = batched_write(sess,
            "UNWIND $rows AS row MATCH (k:__Community__ {community: row.community}) "
            "SET k.summary = row.summary, k.full_content = row.full_content, k.rank = row.rank, "
            "    k.rating_explanation = row.rating_explanation, k.report_title = row.title",
            _records(reps, ["community", "summary", "full_content", "rank", "rating_explanation", "title"]))

    print(f"load    : Neo4j <- {output_dir}")
    print(f"          {n_docs} documents, {n_chunks} chunks, {n_ents} entities, "
          f"{n_rels} relationships, {n_coms} communities ({n_reps} reports)")
    return 0
