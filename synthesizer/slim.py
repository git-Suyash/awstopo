"""
Graph synthesizer — strips the raw GraphDocument down to a compact,
HTTP-friendly representation.

What gets dropped:
  - Node: user_id, vpc_id, subnet_id, app, environment (redundant denorms)
  - Node properties: raw tags dict, resource_type echo, any None values
  - Edge: properties dict entirely (carries only internal user_id)
  - Edge: source_label / target_label (derivable from node map)
  - groups: the full pre-computed grouping index (frontend can derive it)

What is kept:
  - Node: id, label, region, account_id, name + a small type-specific
    subset of properties (see _KEEP_PROPS)
  - Edge: source, target, type
  - Metadata: scan provenance (no user_id)
"""

from __future__ import annotations

from typing import Any

from graph.graph import GraphDocument, GraphNode


# Per-label allowlist of properties worth keeping.
# Everything else in node.properties is dropped.
_KEEP_PROPS: dict[str, set[str]] = {
    "VPC": {"cidr_block", "is_default"},
    "Subnet": {"cidr_block", "availability_zone", "map_public_ip"},
    "SecurityGroup": {"group_name", "description"},
    "InternetGateway": set(),
    "NatGateway": {"state", "subnet_id"},
    "RouteTable": {"main"},
    "EC2Instance": {"instance_type", "state", "availability_zone", "public_ip", "private_ip"},
    "RDSInstance": {"engine", "engine_version", "instance_class", "multi_az", "storage_encrypted"},
    "RDSCluster": {"engine", "engine_version", "multi_az", "storage_encrypted"},
    "S3Bucket": {"region", "versioning", "public_access_blocked", "encryption"},
    "AWSAccount": {"account_id"},
    "Region": {"region"},
}


def _slim_node(node: GraphNode) -> dict[str, Any]:
    allowed = _KEEP_PROPS.get(node.label, set())
    props = {k: v for k, v in node.properties.items() if k in allowed and v is not None}

    out: dict[str, Any] = {"id": node.id, "label": node.label}
    if node.name:
        out["name"] = node.name
    if node.region:
        out["region"] = node.region
    if node.account_id:
        out["account_id"] = node.account_id
    if props:
        out["properties"] = props
    return out


def synthesize(doc: GraphDocument) -> dict[str, Any]:
    """
    Return a slimmed-down dict ready for JSON serialisation.

    Output shape:
    {
      "metadata": { scan provenance, no user_id },
      "nodes":    [ { id, label, name?, region?, account_id?, properties? }, ... ],
      "edges":    [ { source, target, type }, ... ]
    }
    """
    meta = doc.metadata
    slim_meta = {
        "scan_id":        meta.scan_id,
        "account_id":     meta.account_id,
        "regions_scanned": meta.regions_scanned,
        "started_at":     meta.started_at.isoformat() if meta.started_at else None,
        "completed_at":   meta.completed_at.isoformat() if meta.completed_at else None,
        "node_count":     meta.node_count,
        "edge_count":     meta.edge_count,
        "collector_errors": meta.collector_errors or None,
    }

    slim_nodes = [_slim_node(n) for n in doc.nodes]

    slim_edges = [
        {"source": e.source, "target": e.target, "type": e.type}
        for e in doc.edges
    ]

    return {
        "metadata": slim_meta,
        "nodes": slim_nodes,
        "edges": slim_edges,
    }
