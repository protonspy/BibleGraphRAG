"""Inspect an ingested graph — entity/relationship counts and samples for validation."""
from __future__ import annotations

from neo4j import GraphDatabase

from core.config import env
from core.graph.client import quiet_neo4j_logging


def run(group_id: str | None = None, limit: int = 30) -> int:
    """Print typed-entity counts, entity names, and extracted relationships."""
    quiet_neo4j_logging()
    gid_clause = "WHERE n.group_id = $gid" if group_id else ""
    edge_clause = "WHERE e.group_id = $gid" if group_id else ""
    params = {"gid": group_id, "limit": limit}

    driver = GraphDatabase.driver(env.neo4j_uri, auth=(env.neo4j_user, env.neo4j_password))
    try:
        with driver.session() as sess:
            scope = f"group_id '{group_id}'" if group_id else "all groups"
            print(f"Graph inspection ({scope})")

            total = sess.run(f"MATCH (n:Entity) {gid_clause} RETURN count(n) AS c", params).single()["c"]
            edges = sess.run(f"MATCH ()-[e:RELATES_TO]->() {edge_clause} RETURN count(e) AS c", params).single()["c"]
            episodes = sess.run(
                f"MATCH (n:Episodic) {gid_clause} RETURN count(n) AS c", params
            ).single()["c"]
            print(f"  episodes: {episodes}   entities: {total}   relationships: {edges}")

            print("\nEntities by type:")
            rows = sess.run(
                f"""MATCH (n:Entity) {gid_clause}
                    UNWIND labels(n) AS lbl
                    WITH lbl, count(DISTINCT n) AS c WHERE lbl <> 'Entity'
                    RETURN lbl, c ORDER BY c DESC""",
                params,
            )
            typed = list(rows)
            for r in typed:
                print(f"  {r['lbl']:10} {r['c']}")
            if not typed:
                print("  (none typed)")

            print(f"\nEntities (up to {limit}):")
            rows = sess.run(
                f"""MATCH (n:Entity) {gid_clause}
                    RETURN n.name AS name, [l IN labels(n) WHERE l <> 'Entity'] AS types
                    ORDER BY name LIMIT $limit""",
                params,
            )
            for r in rows:
                tag = f"  [{', '.join(r['types'])}]" if r["types"] else ""
                print(f"  {r['name']}{tag}")

            print(f"\nRelationships (up to {limit}):")
            rows = sess.run(
                f"""MATCH (a:Entity)-[e:RELATES_TO]->(b:Entity) {edge_clause}
                    RETURN a.name AS src, e.name AS rel, b.name AS tgt, e.fact AS fact
                    ORDER BY src LIMIT $limit""",
                params,
            )
            for r in rows:
                print(f"  ({r['src']}) -[{r['rel']}]-> ({r['tgt']})")
                if r["fact"]:
                    print(f"      \"{r['fact']}\"")
    finally:
        driver.close()
    return 0
