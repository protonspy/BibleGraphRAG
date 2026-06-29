"""A Neo4j-backed vector store for GraphRAG, registered via the graphrag_vectors factory.

This is the heart of "absorb GraphRAG into Neo4j": instead of writing embeddings to lancedb,
GraphRAG's generate_text_embeddings workflow writes them straight into Neo4j during indexing — onto
the same nodes core.graph.load_neo4j later enriches (matched by id) — and a native Neo4j vector
index backs core.graph.search.local_search. No lancedb, no separate embedding pass.

We register it under the type id "neo4j" (register_vector_store); rag/settings.yaml sets
vector_store.type: neo4j. GraphRAG instantiates one store per embedding target (index_name); we map
each to a Neo4j label + property + vector index:

    entity_description     -> (:__Entity__    {id}).embedding,  index 'entity_embedding'
    text_unit_text         -> (:__Chunk__     {id}).embedding,  index 'chunk_embedding'
    community_full_content -> (:__Community__ {id}).embedding,  index 'community_embedding'

load_neo4j keys __Entity__/__Chunk__ by id, so embeddings reconcile onto the right nodes regardless
of which step runs first (MERGE by id). The default config enables only entity_description — what
local_search needs — keeping cost and surface small. The Neo4j connection comes from our .env
(core.config.env), not from GraphRAG's config, so secrets stay in one place.
"""
from __future__ import annotations

from typing import Any

from graphrag_vectors.vector_store import (
    VectorStore,
    VectorStoreDocument,
    VectorStoreSearchResult,
)

from core.config import env
from core.graph.neo4j_util import batched_write, quiet_neo4j_logging, session

# GraphRAG embedding target (index_name) -> (Neo4j label, embedding property, vector-index name).
_TARGETS: dict[str, tuple[str, str, str]] = {
    "entity_description": ("__Entity__", "embedding", "entity_embedding"),
    "text_unit_text": ("__Chunk__", "embedding", "chunk_embedding"),
    "community_full_content": ("__Community__", "embedding", "community_embedding"),
}
_DEFAULT_TARGET = _TARGETS["entity_description"]


class Neo4jVectorStore(VectorStore):
    """Stores GraphRAG embeddings as node properties in Neo4j, backed by a native vector index."""

    def __init__(self, **kwargs: Any):
        # The factory passes the merged VectorStoreConfig + IndexSchema as kwargs; the base class
        # picks out index_name/vector_size/etc. and absorbs the rest (type, db_uri, ...) in **kwargs.
        super().__init__(**kwargs)
        self.label, self.prop, self.vector_index = _TARGETS.get(self.index_name, _DEFAULT_TARGET)
        # Our embeddings are env.embedding_dim wide; the schema's 3072 default doesn't apply.
        self.dimensions = env.embedding_dim

    def connect(self, **kwargs: Any) -> None:
        # Connections are opened per operation via neo4j_util.session(); nothing to hold open here.
        quiet_neo4j_logging()

    def create_index(self) -> None:
        with session() as sess:
            sess.run(
                f"CREATE VECTOR INDEX {self.vector_index} IF NOT EXISTS " # type: ignore
                f"FOR (n:{self.label}) ON (n.{self.prop}) "
                "OPTIONS {indexConfig: {`vector.dimensions`: $dim, "
                "`vector.similarity_function`: 'cosine'}}",
                dim=self.dimensions,
            )

    def load_documents(self, documents: list[VectorStoreDocument]) -> None:
        rows = [{"id": str(d.id), "vec": d.vector} for d in documents if d.vector is not None]
        if not rows:
            return
        with session() as sess:
            batched_write(
                sess,
                f"UNWIND $rows AS row MERGE (n:{self.label} {{id: row.id}}) SET n.{self.prop} = row.vec",
                rows,
            )

    def similarity_search_by_vector(
        self,
        query_embedding: list[float],
        k: int = 10,
        select: list[str] | None = None,
        filters: Any | None = None,
        include_vectors: bool = True,
    ) -> list[VectorStoreSearchResult]:
        cypher = (
            f"CALL db.index.vector.queryNodes('{self.vector_index}', $k, $vec) YIELD node, score "
            f"RETURN node.id AS id, score, node.{self.prop} AS vec, "
            "       node.name AS name, node.description AS description"
        )
        results: list[VectorStoreSearchResult] = []
        with session() as sess:
            for r in sess.run(cypher, k=k, vec=query_embedding): # type: ignore
                data = {key: r[key] for key in ("name", "description") if r[key] is not None}
                results.append(
                    VectorStoreSearchResult(
                        document=VectorStoreDocument(
                            id=r["id"], vector=r["vec"] if include_vectors else None, data=data
                        ),
                        score=r["score"],
                    )
                )
        return results

    def search_by_id(
        self, id: str, select: list[str] | None = None, include_vectors: bool = True
    ) -> VectorStoreDocument:
        cypher = (
            f"MATCH (n:{self.label} {{id: $id}}) "
            f"RETURN n.id AS id, n.{self.prop} AS vec, n.name AS name, n.description AS description"
        )
        with session() as sess:
            rec = sess.run(cypher, id=str(id)).single()
        if rec is None:
            return VectorStoreDocument(id=id, vector=None)
        data = {key: rec[key] for key in ("name", "description") if rec[key] is not None}
        return VectorStoreDocument(
            id=rec["id"], vector=rec["vec"] if include_vectors else None, data=data
        )

    def count(self) -> int:
        with session() as sess:
            return sess.run(
                f"MATCH (n:{self.label}) WHERE n.{self.prop} IS NOT NULL RETURN count(n) AS c" # type: ignore
            ).single()["c"]

    def remove(self, ids: list[str]) -> None:
        if not ids:
            return
        with session() as sess:
            sess.run(
                f"MATCH (n:{self.label}) WHERE n.id IN $ids REMOVE n.{self.prop}", # type: ignore
                ids=[str(i) for i in ids],
            )

    def update(self, document: VectorStoreDocument) -> None:
        self.load_documents([document])


_REGISTERED = False


def register_neo4j_vector_store() -> None:
    """Register the Neo4j vector store under the type id 'neo4j' (idempotent)."""
    global _REGISTERED
    if _REGISTERED:
        return
    from graphrag_vectors.vector_store_factory import register_vector_store

    register_vector_store("neo4j", Neo4jVectorStore)
    _REGISTERED = True
