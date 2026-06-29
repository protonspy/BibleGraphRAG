"""Inspect a loaded GraphRAG graph in Neo4j — counts and samples for validation.

Reads the schema written by core.graph.load_neo4j (__Entity__ / __Chunk__ / __Document__ /
__Community__, related by RELATED / HAS_ENTITY / PART_OF / IN_COMMUNITY). An empty graph just
prints zeros — run `bgr build` (without --skip-load) to populate it.
"""
from __future__ import annotations

from core.graph.neo4j_util import quiet_neo4j_logging, session


def run(limit: int = 30) -> int:
    """Print node/edge counts, entities by type, and samples of entities, relations, communities."""
    quiet_neo4j_logging()
    with session() as sess:
        print("Graph inspection (GraphRAG → Neo4j)")

        def count(cypher: str) -> int:
            return sess.run(cypher).single()[0]

        docs = count("MATCH (d:__Document__) RETURN count(d)")
        chunks = count("MATCH (c:__Chunk__) RETURN count(c)")
        ents = count("MATCH (e:__Entity__) RETURN count(e)")
        edges = count("MATCH ()-[r:RELATED]->() RETURN count(r)")
        coms = count("MATCH (k:__Community__) RETURN count(k)")
        print(f"  documents: {docs}   chunks: {chunks}   entities: {ents}   "
              f"relationships: {edges}   communities: {coms}")

        print("\nEntities by type:")
        rows = list(sess.run(
            "MATCH (e:__Entity__) WHERE e.type IS NOT NULL "
            "RETURN e.type AS type, count(*) AS c ORDER BY c DESC"))
        for r in rows:
            print(f"  {r['type']:14} {r['c']}")
        if not rows:
            print("  (none)")

        print(f"\nEntities (up to {limit}):")
        for r in sess.run(
            "MATCH (e:__Entity__) RETURN e.name AS name, e.type AS type "
            "ORDER BY e.name LIMIT $limit", limit=limit):
            tag = f"  [{r['type']}]" if r["type"] else ""
            print(f"  {r['name']}{tag}")

        print(f"\nRelationships (up to {limit}, strongest first):")
        for r in sess.run(
            "MATCH (a:__Entity__)-[r:RELATED]->(b:__Entity__) "
            "RETURN a.name AS src, b.name AS tgt, r.description AS desc, r.weight AS w "
            "ORDER BY w DESC LIMIT $limit", limit=limit):
            print(f"  ({r['src']}) -[RELATED]-> ({r['tgt']})")
            if r["desc"]:
                print(f"      \"{r['desc']}\"")

        print(f"\nCommunities (up to {limit}):")
        for r in sess.run(
            "MATCH (k:__Community__) "
            "RETURN k.community AS id, k.level AS level, "
            "       coalesce(k.report_title, k.title) AS title, k.rank AS rank "
            "ORDER BY k.level, k.community LIMIT $limit", limit=limit):
            rank = f" (rank {r['rank']})" if r["rank"] is not None else ""
            print(f"  [L{r['level']}] #{r['id']} {r['title']}{rank}")
    return 0
