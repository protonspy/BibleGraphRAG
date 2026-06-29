"""Small Neo4j helpers shared by the loader, inspector, and transfer steps.

These used to live in core.graph.client alongside the Graphiti wiring; with the move to GraphRAG
(whose native store is parquet + lancedb) the only Neo4j code we keep is this thin driver layer.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from neo4j import GraphDatabase, Session

from core.config import env


def quiet_neo4j_logging() -> None:
    """Quiet benign driver noise so the CLI output stays readable.

    - neo4j.notifications: per-query 'property/label does not exist' WARNINGs that the driver
      raises when MATCHing against a sparse or freshly-created graph.
    - neo4j.pool: connection-pool chatter.
    Real failures still surface through each step's own error handling.
    """
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    logging.getLogger("neo4j.pool").setLevel(logging.ERROR)


@contextmanager
def session() -> Iterator[Session]:
    """Yield a Neo4j session from the configured connection, closing the driver afterward."""
    driver = GraphDatabase.driver(env.neo4j_uri, auth=(env.neo4j_user, env.neo4j_password))
    try:
        with driver.session() as sess:
            yield sess
    finally:
        driver.close()


def batched_write(sess: Session, cypher: str, rows: list[dict], batch: int = 1000) -> int:
    """Run `cypher` (which must UNWIND $rows) over `rows` in batches; return how many were sent.

    A no-op (returns 0) when there are no rows, so callers can pass empty tables freely.
    """
    total = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        sess.run(cypher, rows=chunk) # noqa
        total += len(chunk)
    return total
