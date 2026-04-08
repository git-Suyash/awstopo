"""
Graph data models.

The GraphDocument is the primary output format — it is stored in MongoDB
and drives both Neo4j ingestion and frontend visualization.

Design goals:
  - Every node has a stable `id` (the AWS resource ID)
  - Every edge has a semantic `type` (verb in CAPS)
  - `groups` pre-computes groupings so the frontend doesn't have to
  - `metadata` carries scan provenance
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from collectors.resources import ResourceType


# ── Graph primitives ───────────────────────────────────────────────────────────


class NodeLabel(str, Enum):
    """Maps 1-to-1 to Neo4j node labels."""

    VPC = "VPC"
    SUBNET = "Subnet"
    SECURITY_GROUP = "SecurityGroup"
    INTERNET_GATEWAY = "InternetGateway"
    NAT_GATEWAY = "NatGateway"
    ROUTE_TABLE = "RouteTable"
    EC2_INSTANCE = "EC2Instance"
    RDS_INSTANCE = "RDSInstance"
    RDS_CLUSTER = "RDSCluster"
    S3_BUCKET = "S3Bucket"
    ACCOUNT = "AWSAccount"
    REGION = "Region"


RESOURCE_TYPE_TO_LABEL: dict[ResourceType, NodeLabel] = {
    ResourceType.VPC: NodeLabel.VPC,
    ResourceType.SUBNET: NodeLabel.SUBNET,
    ResourceType.SECURITY_GROUP: NodeLabel.SECURITY_GROUP,
    ResourceType.INTERNET_GATEWAY: NodeLabel.INTERNET_GATEWAY,
    ResourceType.NAT_GATEWAY: NodeLabel.NAT_GATEWAY,
    ResourceType.ROUTE_TABLE: NodeLabel.ROUTE_TABLE,
    ResourceType.EC2_INSTANCE: NodeLabel.EC2_INSTANCE,
    ResourceType.RDS_INSTANCE: NodeLabel.RDS_INSTANCE,
    ResourceType.RDS_CLUSTER: NodeLabel.RDS_CLUSTER,
    ResourceType.S3_BUCKET: NodeLabel.S3_BUCKET,
}


class EdgeType(str, Enum):
    """Relationship verbs used in Neo4j and encoded in MongoDB."""

    # Containment (structural)
    BELONGS_TO_ACCOUNT = "BELONGS_TO_ACCOUNT"
    IN_REGION = "IN_REGION"
    CONTAINS_SUBNET = "CONTAINS_SUBNET"
    IN_VPC = "IN_VPC"
    IN_SUBNET = "IN_SUBNET"

    # EC2
    PROTECTED_BY = "PROTECTED_BY"        # instance → security group
    HAS_ROUTE_TABLE = "HAS_ROUTE_TABLE"  # subnet → route table

    # RDS
    MEMBER_OF_CLUSTER = "MEMBER_OF_CLUSTER"

    # Security group references
    SG_REFERENCES_SG = "SG_REFERENCES_SG"

    # Networking
    ATTACHED_TO_VPC = "ATTACHED_TO_VPC"  # IGW → VPC
    ROUTES_THROUGH = "ROUTES_THROUGH"    # subnet → NAT/IGW

    # Access
    ACCESSIBLE_FROM = "ACCESSIBLE_FROM"  # resource → security group source


class GraphNode(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str                          # AWS resource ID — globally unique within account
    label: NodeLabel
    properties: dict[str, Any] = Field(default_factory=dict)

    # Denormalised fields for fast frontend grouping
    vpc_id: str | None = None
    subnet_id: str | None = None
    region: str | None = None
    account_id: str | None = None
    app: str | None = None           # from App/Application tag
    environment: str | None = None   # from Env/Environment tag
    name: str | None = None


class GraphEdge(BaseModel):
    source: str          # node id
    target: str          # node id
    type: EdgeType
    properties: dict[str, Any] = Field(default_factory=dict)
    source_label: NodeLabel | None = None
    target_label: NodeLabel | None = None


# ── Grouping index ─────────────────────────────────────────────────────────────


class GroupEntry(BaseModel):
    """Lightweight reference to a resource within a group."""
    id: str
    label: NodeLabel
    name: str | None = None


class ResourceGroups(BaseModel):
    """
    Pre-computed grouping indexes.

    These allow the frontend to render "VPC view", "App view", or
    "Environment view" without scanning all nodes.
    """

    by_vpc: dict[str, list[GroupEntry]] = Field(default_factory=dict)
    by_subnet: dict[str, list[GroupEntry]] = Field(default_factory=dict)
    by_app: dict[str, list[GroupEntry]] = Field(default_factory=dict)
    by_environment: dict[str, list[GroupEntry]] = Field(default_factory=dict)
    by_region: dict[str, list[GroupEntry]] = Field(default_factory=dict)
    untagged: list[GroupEntry] = Field(default_factory=list)


# ── Top-level document ─────────────────────────────────────────────────────────


class ScanMetadata(BaseModel):
    account_id: str
    scan_id: str              # UUID — used as idempotency key in DB
    started_at: datetime
    completed_at: datetime | None = None
    regions_scanned: list[str] = Field(default_factory=list)
    collector_errors: dict[str, str] = Field(default_factory=dict)
    node_count: int = 0
    edge_count: int = 0


class GraphDocument(BaseModel):
    """
    Primary output document stored in MongoDB and used to build Neo4j graph.

    Structure is optimised for:
      1. Direct storage in MongoDB as a single document per scan
      2. Streaming into Neo4j via MERGE operations
      3. Frontend graph rendering (nodes + edges + group indexes)
    """

    model_config = ConfigDict(populate_by_name=True)

    metadata: ScanMetadata
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    groups: ResourceGroups = Field(default_factory=ResourceGroups)

    def to_mongo_dict(self) -> dict[str, Any]:
        """Serialize for MongoDB insertion (uses scan_id as _id)."""
        d = self.model_dump(mode="json")
        d["_id"] = self.metadata.scan_id
        return d
