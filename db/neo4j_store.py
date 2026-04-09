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
        """
        Create composite (id, user_id) uniqueness constraints for multi-tenant isolation.

        Drops any legacy single-property id constraints first — those would block
        a second user from scanning the same AWS account (same resource IDs).
        """
        assert self._driver is not None  # noqa: S101
        labels = [
            "AWSAccount", "Region", "VPC", "Subnet", "SecurityGroup",
            "EC2Instance", "RDSInstance", "RDSCluster", "S3Bucket",
            "InternetGateway", "NatGateway", "RouteTable",
        ]
        async with self._driver.session() as session:
            # Find and drop old single-property id constraints
            result = await session.run(
                "SHOW CONSTRAINTS YIELD name, properties "
                "WHERE size(properties) = 1 AND properties[0] = 'id'"
            )
            old = await result.data()
            for row in old:
                await session.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")
            if old:
                log.info("neo4j.dropped_legacy_constraints", count=len(old))

            # Create composite (id, user_id) constraints
            for label in labels:
                await session.run(
                    f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) "
                    f"REQUIRE (n.id, n.user_id) IS UNIQUE"
                )

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
                # MERGE on (id, user_id) so each user gets isolated nodes
                # even when multiple users scan the same AWS account
                query = f"""
                UNWIND $nodes AS n
                MERGE (resource:{label} {{id: n.id, user_id: n.user_id}})
                SET resource += n.props
                SET resource.updated_at = timestamp()
                """
                params = {
                    "nodes": [
                        {
                            "id": node.id,
                            "user_id": node.user_id or "",
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
            # Build MATCH clauses with user_id in key to stay within tenant's graph
            if src_lbl:
                src_match = f"(src:{src_lbl} {{id: e.source, user_id: e.user_id}})"
            else:
                src_match = "(src {id: e.source, user_id: e.user_id})"
            if tgt_lbl:
                tgt_match = f"(tgt:{tgt_lbl} {{id: e.target, user_id: e.user_id}})"
            else:
                tgt_match = "(tgt {id: e.target, user_id: e.user_id})"
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
                            "user_id": edge.properties.get("user_id", ""),
                            "props": edge.properties,
                        }
                        for edge in batch
                    ]
                }
                async with self._driver.session() as session:
                    result = await session.run(query, params)
                    await result.consume()

    async def get_user_graph(self, user_id: str) -> dict[str, Any]:
        """
        Return all nodes and relationships belonging to a user.
        Used by the API to deliver graph data to the frontend.
        """
        assert self._driver is not None  # noqa: S101
        query = """
        MATCH (n {user_id: $user_id})
        OPTIONAL MATCH (n)-[r]->(m {user_id: $user_id})
        RETURN
            n.id          AS source_id,
            labels(n)[0]  AS source_label,
            n             AS source_props,
            type(r)       AS rel_type,
            m.id          AS target_id,
            labels(m)[0]  AS target_label
        """
        async with self._driver.session() as session:
            result = await session.run(query, user_id=user_id)
            records = await result.data()

        nodes: dict[str, dict] = {}
        edges: list[dict] = []
        for row in records:
            sid = row["source_id"]
            if sid and sid not in nodes:
                props = dict(row["source_props"])
                props.pop("user_id", None)
                nodes[sid] = {"id": sid, "label": row["source_label"], "properties": props}
            if row["rel_type"] and row["target_id"]:
                edges.append({
                    "source": sid,
                    "target": row["target_id"],
                    "type": row["rel_type"],
                })

        return {"nodes": list(nodes.values()), "edges": edges}

    async def get_resource_neighbourhood(self, user_id: str, resource_id: str, depth: int = 2) -> dict[str, Any]:
        """Return the neighbourhood of a single resource up to `depth` hops, scoped to the user."""
        assert self._driver is not None  # noqa: S101
        query = """
        MATCH path = (start {id: $resource_id, user_id: $user_id})-[*1..$depth]-({user_id: $user_id})
        RETURN path
        """
        async with self._driver.session() as session:
            result = await session.run(query, resource_id=resource_id, user_id=user_id, depth=depth)
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
        "user_id": node.user_id or "",
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
