"""
Type-safe Pydantic v2 models for AWS resources.

All models use strict validation and explicit field aliases where AWS returns
differently-cased keys. Tags are normalized to a flat dict.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

# ── Shared primitives ──────────────────────────────────────────────────────────

Tags = dict[str, str]


def parse_tags(raw_tags: list[dict[str, str]] | None) -> Tags:
    """Convert AWS [{Key: k, Value: v}] format to a plain dict."""
    if not raw_tags:
        return {}
    return {t["Key"]: t["Value"] for t in raw_tags if "Key" in t and "Value" in t}


class ResourceType(str, Enum):
    VPC = "vpc"
    SUBNET = "subnet"
    SECURITY_GROUP = "security_group"
    INTERNET_GATEWAY = "internet_gateway"
    NAT_GATEWAY = "nat_gateway"
    ROUTE_TABLE = "route_table"
    EC2_INSTANCE = "ec2_instance"
    RDS_INSTANCE = "rds_instance"
    RDS_CLUSTER = "rds_cluster"
    S3_BUCKET = "s3_bucket"
    ELASTIC_IP = "elastic_ip"
    NETWORK_INTERFACE = "network_interface"


class BaseResource(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    resource_id: str
    resource_type: ResourceType
    region: str
    account_id: str
    name: str | None = None
    tags: Tags = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)  # not persisted to graph

    @property
    def app_tag(self) -> str | None:
        return self.tags.get("App") or self.tags.get("Application") or self.tags.get("app")

    @property
    def env_tag(self) -> str | None:
        return self.tags.get("Env") or self.tags.get("Environment") or self.tags.get("environment")


# ── VPC Resources ──────────────────────────────────────────────────────────────


class CidrBlock(BaseModel):
    cidr: str
    state: str = "associated"


class VPCResource(BaseResource):
    resource_type: ResourceType = ResourceType.VPC
    cidr_block: str
    additional_cidrs: list[CidrBlock] = Field(default_factory=list)
    is_default: bool = False
    dhcp_options_id: str | None = None
    state: str = "available"


class SubnetResource(BaseResource):
    resource_type: ResourceType = ResourceType.SUBNET
    vpc_id: str
    cidr_block: str
    availability_zone: str
    available_ip_count: int = 0
    is_public: bool = False  # derived: has route to IGW
    map_public_ip_on_launch: bool = False
    state: str = "available"


class SecurityGroupRule(BaseModel):
    protocol: str
    from_port: int | None = None
    to_port: int | None = None
    cidr_ranges: list[str] = Field(default_factory=list)
    source_sg_ids: list[str] = Field(default_factory=list)
    description: str | None = None


class SecurityGroupResource(BaseResource):
    resource_type: ResourceType = ResourceType.SECURITY_GROUP
    vpc_id: str
    description: str = ""
    inbound_rules: list[SecurityGroupRule] = Field(default_factory=list)
    outbound_rules: list[SecurityGroupRule] = Field(default_factory=list)


class InternetGatewayResource(BaseResource):
    resource_type: ResourceType = ResourceType.INTERNET_GATEWAY
    attached_vpc_ids: list[str] = Field(default_factory=list)


class NatGatewayResource(BaseResource):
    resource_type: ResourceType = ResourceType.NAT_GATEWAY
    vpc_id: str
    subnet_id: str
    state: str
    public_ip: str | None = None
    private_ip: str | None = None


class RouteTableResource(BaseResource):
    resource_type: ResourceType = ResourceType.ROUTE_TABLE
    vpc_id: str
    associated_subnet_ids: list[str] = Field(default_factory=list)
    is_main: bool = False
    routes: list[dict[str, Any]] = Field(default_factory=list)
    has_igw_route: bool = False  # derived


# ── EC2 Resources ──────────────────────────────────────────────────────────────


class EC2State(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    TERMINATED = "terminated"


class NetworkInterfaceRef(BaseModel):
    eni_id: str
    subnet_id: str | None = None
    private_ip: str | None = None
    public_ip: str | None = None
    security_group_ids: list[str] = Field(default_factory=list)


class EC2InstanceResource(BaseResource):
    resource_type: ResourceType = ResourceType.EC2_INSTANCE
    vpc_id: str | None = None
    subnet_id: str | None = None
    instance_type: str
    state: EC2State
    private_ip: str | None = None
    public_ip: str | None = None
    security_group_ids: list[str] = Field(default_factory=list)
    iam_instance_profile_arn: str | None = None
    image_id: str
    key_name: str | None = None
    launch_time: datetime | None = None
    availability_zone: str | None = None
    network_interfaces: list[NetworkInterfaceRef] = Field(default_factory=list)
    platform: str = "linux"
    monitoring_enabled: bool = False


# ── RDS Resources ──────────────────────────────────────────────────────────────


class RDSInstanceResource(BaseResource):
    resource_type: ResourceType = ResourceType.RDS_INSTANCE
    vpc_id: str | None = None
    subnet_ids: list[str] = Field(default_factory=list)
    security_group_ids: list[str] = Field(default_factory=list)
    engine: str
    engine_version: str
    instance_class: str
    status: str
    endpoint_address: str | None = None
    endpoint_port: int | None = None
    multi_az: bool = False
    publicly_accessible: bool = False
    storage_encrypted: bool = False
    db_cluster_identifier: str | None = None
    db_subnet_group_name: str | None = None
    availability_zone: str | None = None
    backup_retention_days: int = 0
    deletion_protection: bool = False


class RDSClusterResource(BaseResource):
    resource_type: ResourceType = ResourceType.RDS_CLUSTER
    vpc_id: str | None = None
    subnet_ids: list[str] = Field(default_factory=list)
    security_group_ids: list[str] = Field(default_factory=list)
    engine: str
    engine_version: str
    status: str
    endpoint: str | None = None
    reader_endpoint: str | None = None
    port: int | None = None
    multi_az: bool = False
    storage_encrypted: bool = False
    backup_retention_days: int = 0
    deletion_protection: bool = False
    member_instance_ids: list[str] = Field(default_factory=list)


# ── S3 Resources ───────────────────────────────────────────────────────────────


class S3BlockPublicAccess(BaseModel):
    block_public_acls: bool = True
    ignore_public_acls: bool = True
    block_public_policy: bool = True
    restrict_public_buckets: bool = True

    @property
    def fully_blocked(self) -> bool:
        return all(
            [
                self.block_public_acls,
                self.ignore_public_acls,
                self.block_public_policy,
                self.restrict_public_buckets,
            ]
        )


class S3BucketResource(BaseResource):
    resource_type: ResourceType = ResourceType.S3_BUCKET
    bucket_region: str = ""
    creation_date: datetime | None = None
    versioning_enabled: bool = False
    server_side_encryption: bool = False
    encryption_algorithm: str | None = None
    public_access_block: S3BlockPublicAccess = Field(default_factory=S3BlockPublicAccess)
    has_bucket_policy: bool = False
    is_public: bool = False  # derived
    replication_enabled: bool = False
    logging_enabled: bool = False
    lifecycle_rules_count: int = 0


# ── Collector output container ─────────────────────────────────────────────────

AnyResource = Annotated[
    VPCResource
    | SubnetResource
    | SecurityGroupResource
    | InternetGatewayResource
    | NatGatewayResource
    | RouteTableResource
    | EC2InstanceResource
    | RDSInstanceResource
    | RDSClusterResource
    | S3BucketResource,
    Field(discriminator="resource_type"),
]


class RegionInventory(BaseModel):
    """All resources discovered in a single region."""

    account_id: str
    region: str
    scan_timestamp: datetime

    vpcs: list[VPCResource] = Field(default_factory=list)
    subnets: list[SubnetResource] = Field(default_factory=list)
    security_groups: list[SecurityGroupResource] = Field(default_factory=list)
    internet_gateways: list[InternetGatewayResource] = Field(default_factory=list)
    nat_gateways: list[NatGatewayResource] = Field(default_factory=list)
    route_tables: list[RouteTableResource] = Field(default_factory=list)
    ec2_instances: list[EC2InstanceResource] = Field(default_factory=list)
    rds_instances: list[RDSInstanceResource] = Field(default_factory=list)
    rds_clusters: list[RDSClusterResource] = Field(default_factory=list)
    s3_buckets: list[S3BucketResource] = Field(default_factory=list)

    # Collector-level errors (service → error message) — allow partial data
    collector_errors: dict[str, str] = Field(default_factory=dict)

    def all_resources(self) -> list[BaseResource]:
        return (
            self.vpcs  # type: ignore[return-value]
            + self.subnets
            + self.security_groups
            + self.internet_gateways
            + self.nat_gateways
            + self.route_tables
            + self.ec2_instances
            + self.rds_instances
            + self.rds_clusters
            + self.s3_buckets
        )
