"""Backfill entity embeddings in Neo4j and ensure the vector index — a safety net behind the store.

The primary path writes entity embeddings into Neo4j during indexing, via our GraphRAG vector store
(core.graph.neo4j_vectors). This step runs after the load as a backstop: it ensures the vector index
exists and embeds (via OpenRouter) only the entities still missing a vector — a no-op when the store
already populated them, and a full backfill if the embedding workflow was skipped or failed.
"""
from __future__ import annotations

import sys

from core.config import env
from core.graph.neo4j_util import batched_write, quiet_neo4j_logging, session
from core.llm import embeddings_model

# Must match core.graph.neo4j_vectors (entity_description target) and core.graph.search.
INDEX_NAME = "entity_embedding"


def _ensure_index(sess) -> None:
    sess.run(
        f"CREATE VECTOR INDEX {INDEX_NAME} IF NOT EXISTS FOR (e:__Entity__) ON (e.embedding) "
        "OPTIONS {indexConfig: {`vector.dimensions`: $dim, `vector.similarity_function`: 'cosine'}}",
        dim=env.embedding_dim,
    )


def run() -> int:
    """Ensure the index and backfill missing entity embeddings. Returns an exit code."""
    quiet_neo4j_logging()
    with session() as sess:
        _ensure_index(sess)
        total = sess.run("MATCH (e:__Entity__) RETURN count(e) AS c").single()["c"]
        missing = [dict(r) for r in sess.run(
            "MATCH (e:__Entity__) WHERE e.embedding IS NULL " # type: ignore
            "RETURN e.id AS id, e.name AS name, e.description AS description")]

    if total == 0:
        print("embed   : no entities in Neo4j (run `bgr build` without --skip-load first)", file=sys.stderr)
        return 1
    if not missing:
        print(f"embed   : all {total} entity embeddings present (written during indexing) — index '{INDEX_NAME}' ready")
        return 0

    texts = [f"{r['name']}: {r['description'] or ''}".strip() for r in missing]
    vectors = embeddings_model().embed_documents(texts)
    payload = [{"id": r["id"], "vec": v} for r, v in zip(missing, vectors)]
    with session() as sess:
        written = batched_write(
            sess,
            "UNWIND $rows AS row MATCH (e:__Entity__ {id: row.id}) SET e.embedding = row.vec",
            payload,
        )
    print(f"embed   : backfilled {written}/{total} missing entity embeddings -> Neo4j (index '{INDEX_NAME}')")
    return 0
