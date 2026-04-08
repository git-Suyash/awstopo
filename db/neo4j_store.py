"""
Async Neo4j storage via the official neo4j driver.

Uses MERGE (not CREATE) throughout so re-running a scan updates existing
nodes rather than duplicating them.

Schema constraints are created on first connect to enforce uniqueness.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import ServiceUnavailable

from config.main import Neo4jSettings
from exceptions.main import StorageError
from graph.graph import GraphDocument, GraphEdge, GraphNode

log = structlog.get_logger(__name__)

# How many nodes/edges to write per transaction batch
_BATCH_SIZE = 100


class Neo4jStore:
    def __init__(self, settings: Neo4jSettings) -> None:
        self._settings = settings
        self._driver: AsyncDriver | None = None

    async def connect(self) -> None:
        self._driver = AsyncGraphDatabase.driver(
            self._settings.uri,
            auth=(self._settings.user, self._settings.password.get_secret_value()),
            database=self._settings.database,
        )
        try:
            await self._driver.verify_connectivity()
        except ServiceUnavailable as exc:
            raise StorageError(f"Neo4j unavailable: {exc}") from exc

        await self._ensure_constraints()
        log.info("neo4j.connected", uri=self._settings.uri)

    async def disconnect(self) -> None:
        if self._driver:
            await self._driver.close()
            log.info("neo4j.disconnected")

    async def _ensure_constraints(self) -> None:
        """Create uniqueness constraints so MERGE is fast and data is clean."""
        assert self._driver is not None  # noqa: S101
        constraints = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:AWSAccount) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Region) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:VPC) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Subnet) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:SecurityGroup) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:EC2Instance) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:RDSInstance) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:RDSCluster) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:S3Bucket) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:InternetGateway) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:NatGateway) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:RouteTable) REQUIRE n.id IS UNIQUE",
        ]
        async with self._driver.session() as session:
            for cql in constraints:
                await session.run(cql)

    async def ingest_graph(self, doc: GraphDocument) -> None:
        """
        Write all nodes and edges from a GraphDocument into Neo4j.

        All writes use MERGE for idempotency.
        Nodes are written first, then edges (referential integrity).
        """
        assert self._driver is not None  # noqa: S101
        try:
            await self._write_nodes(doc.nodes)
            await self._write_edges(doc.edges)
            log.info(
                "neo4j.graph_ingested",
                nodes=len(doc.nodes),
                edges=len(doc.edges),
                scan_id=doc.metadata.scan_id,
            )
        except Exception as exc:
            raise StorageError(f"Neo4j ingestion failed: {exc}") from exc

    async def _write_nodes(self, nodes: list[GraphNode]) -> None:
        assert self._driver is not None  # noqa: S101
        # Group by label for efficient batch MERGE
        by_label: dict[str, list[GraphNode]] = {}
        for node in nodes:
            by_label.setdefault(node.label.value, []).append(node)

        for label, label_nodes in by_label.items():
            for batch in _chunks(label_nodes, _BATCH_SIZE):
                query = f"""
                UNWIND $nodes AS n
                MERGE (resource:{label} {{id: n.id}})
                SET resource += n.props
                SET resource.updated_at = timestamp()
                """
                params = {
                    "nodes": [
                        {
                            "id": node.id,
                            "props": _flatten_props(node),
                        }
                        for node in batch
                    ]
                }
                async with self._driver.session() as session:
                    result = await session.run(query, params)
                    await result.consume()

    async def _write_edges(self, edges: list[GraphEdge]) -> None:
        assert self._driver is not None  # noqa: S101
        # Group edges by (source_label, target_label, type) so every batch can
        # use label-qualified MATCH and hit the uniqueness-constraint indexes.
        by_key: dict[tuple[str, str, str], list[GraphEdge]] = {}
        for edge in edges:
            src_lbl = edge.source_label.value if edge.source_label else ""
            tgt_lbl = edge.target_label.value if edge.target_label else ""
            key = (src_lbl, tgt_lbl, edge.type.value)
            by_key.setdefault(key, []).append(edge)

        for (src_lbl, tgt_lbl, rel_type), group_edges in by_key.items():
            # Build MATCH clauses: use labels when known, fall back to unlabeled.
            src_match = f"(src:{src_lbl} {{id: e.source}})" if src_lbl else "(src {id: e.source})"
            tgt_match = f"(tgt:{tgt_lbl} {{id: e.target}})" if tgt_lbl else "(tgt {id: e.target})"
            query = f"""
            UNWIND $edges AS e
            MATCH {src_match}
            MATCH {tgt_match}
            MERGE (src)-[r:{rel_type}]->(tgt)
            SET r += e.props
            SET r.updated_at = timestamp()
            """
            for batch in _chunks(group_edges, _BATCH_SIZE):
                params = {
                    "edges": [
                        {
                            "source": edge.source,
                            "target": edge.target,
                            "props": edge.properties,
                        }
                        for edge in batch
                    ]
                }
                async with self._driver.session() as session:
                    result = await session.run(query, params)
                    await result.consume()

    async def get_resource_graph(self, resource_id: str, depth: int = 2) -> dict[str, Any]:
        """
        Return the neighbourhood of a resource up to `depth` hops.
        Useful for the future FastAPI endpoint.
        """
        assert self._driver is not None  # noqa: S101
        query = """
        MATCH path = (start {id: $resource_id})-[*1..$depth]-()
        RETURN path
        """
        async with self._driver.session() as session:
            result = await session.run(query, resource_id=resource_id, depth=depth)
            records = await result.data()
            return {"resource_id": resource_id, "depth": depth, "paths": records}


def _chunks(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def _flatten_props(node: GraphNode) -> dict[str, Any]:
    """Flatten a GraphNode into a Neo4j-compatible property map."""
    props: dict[str, Any] = {
        "name": node.name,
        "region": node.region,
        "account_id": node.account_id,
        "vpc_id": node.vpc_id,
        "subnet_id": node.subnet_id,
        "app": node.app,
        "environment": node.environment,
        "label": node.label.value,
    }
    # Merge non-nested properties only (Neo4j doesn't support nested props)
    for k, v in node.properties.items():
        if not isinstance(v, (dict, list)):
            props[k] = v
        elif isinstance(v, list) and all(isinstance(i, str) for i in v):
            props[k] = v  # list of strings is fine
    return {k: v for k, v in props.items() if v is not None}
