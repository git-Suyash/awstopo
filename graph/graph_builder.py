"""
Graph builder.

Converts a RegionInventory into a GraphDocument with:
  - Typed nodes for every resource
  - Semantic edges representing relationships
  - Pre-computed group indexes by VPC, subnet, app tag, environment, region
"""

from __future__ import annotations

from collections import defaultdict

import structlog

from collectors.resources import (
    EC2InstanceResource,
    InternetGatewayResource,
    NatGatewayResource,
    RDSClusterResource,
    RDSInstanceResource,
    RegionInventory,
    ResourceType,
    RouteTableResource,
    S3BucketResource,
    SecurityGroupResource,
    SubnetResource,
    VPCResource,
    BaseResource,
)
from graph.graph import (
    EdgeType,
    GraphDocument,
    GraphEdge,
    GraphNode,
    GroupEntry,
    NodeLabel,
    RESOURCE_TYPE_TO_LABEL,
    ResourceGroups,
    ScanMetadata,
)

log = structlog.get_logger(__name__)


class GraphBuilder:
    """Stateless builder — call build() with an inventory to get a GraphDocument."""

    def build(
        self,
        inventory: RegionInventory,
        metadata: ScanMetadata,
    ) -> GraphDocument:
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        groups = ResourceGroups()

        # ── Account & Region virtual nodes ─────────────────────────────────────
        account_node = GraphNode(
            id=inventory.account_id,
            label=NodeLabel.ACCOUNT,
            properties={"account_id": inventory.account_id},
            account_id=inventory.account_id,
        )
        region_node = GraphNode(
            id=f"{inventory.account_id}:{inventory.region}",
            label=NodeLabel.REGION,
            properties={"region": inventory.region},
            account_id=inventory.account_id,
            region=inventory.region,
        )
        nodes.extend([account_node, region_node])
        edges.append(
            GraphEdge(
                source=region_node.id,
                target=account_node.id,
                type=EdgeType.BELONGS_TO_ACCOUNT,
            )
        )

        # ── Process each resource type ──────────────────────────────────────────
        for vpc in inventory.vpcs:
            node = self._make_node(vpc)
            nodes.append(node)
            edges.append(GraphEdge(source=node.id, target=region_node.id, type=EdgeType.IN_REGION))
            self._add_to_groups(groups, node)

        for subnet in inventory.subnets:
            node = self._make_node(subnet, vpc_id=subnet.vpc_id)
            nodes.append(node)
            edges.append(GraphEdge(source=node.id, target=subnet.vpc_id, type=EdgeType.IN_VPC))
            self._add_to_groups(groups, node)

        for sg in inventory.security_groups:
            node = self._make_node(sg, vpc_id=sg.vpc_id)
            nodes.append(node)
            edges.append(GraphEdge(source=node.id, target=sg.vpc_id, type=EdgeType.IN_VPC))
            # SG → SG references (ingress rules that ref other SGs)
            for rule in sg.inbound_rules:
                for source_sg_id in rule.source_sg_ids:
                    edges.append(
                        GraphEdge(
                            source=sg.resource_id,
                            target=source_sg_id,
                            type=EdgeType.SG_REFERENCES_SG,
                            properties={"protocol": rule.protocol, "from_port": rule.from_port},
                        )
                    )
            self._add_to_groups(groups, node)

        for igw in inventory.internet_gateways:
            node = self._make_node(igw)
            nodes.append(node)
            for vpc_id in igw.attached_vpc_ids:
                edges.append(
                    GraphEdge(source=node.id, target=vpc_id, type=EdgeType.ATTACHED_TO_VPC)
                )
            self._add_to_groups(groups, node)

        for nat in inventory.nat_gateways:
            node = self._make_node(nat, vpc_id=nat.vpc_id, subnet_id=nat.subnet_id)
            nodes.append(node)
            edges.append(GraphEdge(source=node.id, target=nat.vpc_id, type=EdgeType.IN_VPC))
            edges.append(GraphEdge(source=node.id, target=nat.subnet_id, type=EdgeType.IN_SUBNET))
            self._add_to_groups(groups, node)

        for rt in inventory.route_tables:
            node = self._make_node(rt, vpc_id=rt.vpc_id)
            nodes.append(node)
            edges.append(GraphEdge(source=node.id, target=rt.vpc_id, type=EdgeType.IN_VPC))
            for subnet_id in rt.associated_subnet_ids:
                edges.append(
                    GraphEdge(source=subnet_id, target=node.id, type=EdgeType.HAS_ROUTE_TABLE)
                )
            self._add_to_groups(groups, node)

        for ec2 in inventory.ec2_instances:
            node = self._make_node(ec2, vpc_id=ec2.vpc_id, subnet_id=ec2.subnet_id)
            nodes.append(node)
            if ec2.vpc_id:
                edges.append(GraphEdge(source=node.id, target=ec2.vpc_id, type=EdgeType.IN_VPC))
            if ec2.subnet_id:
                edges.append(
                    GraphEdge(source=node.id, target=ec2.subnet_id, type=EdgeType.IN_SUBNET)
                )
            for sg_id in ec2.security_group_ids:
                edges.append(
                    GraphEdge(source=node.id, target=sg_id, type=EdgeType.PROTECTED_BY)
                )
            self._add_to_groups(groups, node)

        for rds in inventory.rds_instances:
            node = self._make_node(rds, vpc_id=rds.vpc_id)
            nodes.append(node)
            if rds.vpc_id:
                edges.append(GraphEdge(source=node.id, target=rds.vpc_id, type=EdgeType.IN_VPC))
            for subnet_id in rds.subnet_ids:
                edges.append(
                    GraphEdge(source=node.id, target=subnet_id, type=EdgeType.IN_SUBNET)
                )
            for sg_id in rds.security_group_ids:
                edges.append(
                    GraphEdge(source=node.id, target=sg_id, type=EdgeType.PROTECTED_BY)
                )
            if rds.db_cluster_identifier:
                edges.append(
                    GraphEdge(
                        source=node.id,
                        target=rds.db_cluster_identifier,
                        type=EdgeType.MEMBER_OF_CLUSTER,
                    )
                )
            self._add_to_groups(groups, node)

        for cluster in inventory.rds_clusters:
            node = self._make_node(cluster, vpc_id=cluster.vpc_id)
            nodes.append(node)
            if cluster.vpc_id:
                edges.append(
                    GraphEdge(source=node.id, target=cluster.vpc_id, type=EdgeType.IN_VPC)
                )
            for sg_id in cluster.security_group_ids:
                edges.append(
                    GraphEdge(source=node.id, target=sg_id, type=EdgeType.PROTECTED_BY)
                )
            self._add_to_groups(groups, node)

        for s3 in inventory.s3_buckets:
            node = self._make_node(s3)
            # S3 properties enriched inline
            node.properties.update(
                {
                    "is_public": s3.is_public,
                    "versioning_enabled": s3.versioning_enabled,
                    "server_side_encryption": s3.server_side_encryption,
                    "encryption_algorithm": s3.encryption_algorithm,
                    "public_access_block": s3.public_access_block.model_dump(),
                    "has_bucket_policy": s3.has_bucket_policy,
                }
            )
            nodes.append(node)
            # S3 belongs to account, not region (global service)
            edges.append(
                GraphEdge(source=node.id, target=account_node.id, type=EdgeType.BELONGS_TO_ACCOUNT)
            )
            self._add_to_groups(groups, node)

        # ── Deduplicate edges ───────────────────────────────────────────────────
        seen: set[tuple[str, str, str]] = set()
        unique_edges: list[GraphEdge] = []
        for edge in edges:
            key = (edge.source, edge.target, edge.type.value)
            if key not in seen:
                seen.add(key)
                unique_edges.append(edge)

        # ── Finalise metadata ───────────────────────────────────────────────────
        metadata.node_count = len(nodes)
        metadata.edge_count = len(unique_edges)

        return GraphDocument(
            metadata=metadata,
            nodes=nodes,
            edges=unique_edges,
            groups=groups,
        )

    def _make_node(
        self,
        resource: BaseResource,
        vpc_id: str | None = None,
        subnet_id: str | None = None,
    ) -> GraphNode:
        label = RESOURCE_TYPE_TO_LABEL[resource.resource_type]
        props: dict = {
            "resource_type": resource.resource_type.value,
            "name": resource.name,
            "tags": resource.tags,
        }
        # Add type-specific properties
        props.update(self._type_specific_props(resource))

        return GraphNode(
            id=resource.resource_id,
            label=label,
            properties=props,
            vpc_id=vpc_id,
            subnet_id=subnet_id,
            region=resource.region,
            account_id=resource.account_id,
            app=resource.app_tag,
            environment=resource.env_tag,
            name=resource.name,
        )

    def _type_specific_props(self, resource: BaseResource) -> dict:
        """Extract the most useful properties for graph rendering."""
        if isinstance(resource, VPCResource):
            return {"cidr_block": resource.cidr_block, "is_default": resource.is_default}
        if isinstance(resource, SubnetResource):
            return {
                "cidr_block": resource.cidr_block,
                "availability_zone": resource.availability_zone,
                "is_public": resource.is_public,
            }
        if isinstance(resource, EC2InstanceResource):
            return {
                "instance_type": resource.instance_type,
                "state": resource.state.value,
                "private_ip": resource.private_ip,
                "public_ip": resource.public_ip,
                "platform": resource.platform,
            }
        if isinstance(resource, RDSInstanceResource):
            return {
                "engine": resource.engine,
                "instance_class": resource.instance_class,
                "status": resource.status,
                "multi_az": resource.multi_az,
                "publicly_accessible": resource.publicly_accessible,
                "storage_encrypted": resource.storage_encrypted,
            }
        if isinstance(resource, RDSClusterResource):
            return {
                "engine": resource.engine,
                "status": resource.status,
                "multi_az": resource.multi_az,
                "storage_encrypted": resource.storage_encrypted,
            }
        if isinstance(resource, S3BucketResource):
            return {"bucket_region": resource.bucket_region}
        return {}

    def _add_to_groups(self, groups: ResourceGroups, node: GraphNode) -> None:
        entry = GroupEntry(id=node.id, label=node.label, name=node.name)

        if node.vpc_id:
            groups.by_vpc.setdefault(node.vpc_id, []).append(entry)

        if node.subnet_id:
            groups.by_subnet.setdefault(node.subnet_id, []).append(entry)

        if node.app:
            groups.by_app.setdefault(node.app, []).append(entry)
        else:
            groups.untagged.append(entry)

        if node.environment:
            groups.by_environment.setdefault(node.environment, []).append(entry)

        if node.region:
            groups.by_region.setdefault(node.region, []).append(entry)


def merge_inventories(inventories: list[RegionInventory]) -> RegionInventory:
    """Combine multiple per-region inventories into one (for multi-region scans)."""
    if not inventories:
        raise ValueError("No inventories to merge")
    base = inventories[0]
    for inv in inventories[1:]:
        base.vpcs.extend(inv.vpcs)
        base.subnets.extend(inv.subnets)
        base.security_groups.extend(inv.security_groups)
        base.internet_gateways.extend(inv.internet_gateways)
        base.nat_gateways.extend(inv.nat_gateways)
        base.route_tables.extend(inv.route_tables)
        base.ec2_instances.extend(inv.ec2_instances)
        base.rds_instances.extend(inv.rds_instances)
        base.rds_clusters.extend(inv.rds_clusters)
        # S3 is global — deduplicate by bucket name
        existing_names = {b.resource_id for b in base.s3_buckets}
        base.s3_buckets.extend(b for b in inv.s3_buckets if b.resource_id not in existing_names)
        base.collector_errors.update(inv.collector_errors)
    return base
