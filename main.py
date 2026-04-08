"""
Main scan orchestrator.

Flow per region:
  1. Authenticate (STS AssumeRole)
  2. Collect  → VPC, EC2, RDS, S3 collectors run concurrently
  3. Process  → Merge inventories, build graph
  4. Store    → Write to MongoDB and Neo4j in parallel

S3 is collected once (global) alongside the first region to avoid duplication.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import structlog

from auth.main import CredentialProvider,discover_enabled_regions
from collectors.resources import BaseResource, RegionInventory
from collectors.sub_collectors.ec2 import EC2Collector
from collectors.sub_collectors.rds import RDSCollector
from collectors.sub_collectors.s3 import S3Collector
from collectors.sub_collectors.vpc import VPCCollector
from config.main import Settings, get_settings
from db.mongo import MongoStore
from db.neo4j_store import Neo4jStore
from graph.graph import GraphDocument, ScanMetadata
from graph.graph_builder import GraphBuilder, merge_inventories
from logger.logging import configure_logging



log = structlog.get_logger(__name__)


async def collect_region(
    region: str,
    account_id: str,
    credential_provider: CredentialProvider,
    settings: Settings,
    include_s3: bool = False,
) -> RegionInventory:
    """
    Run all collectors for a single region concurrently.

    `include_s3` should be True for exactly one region per scan
    since S3 is a global service.
    """
    session = await credential_provider.get_session(region)
    pipeline_cfg = settings.pipeline

    collector_classes = [
        ("vpc", VPCCollector),
        ("ec2", EC2Collector),
        ("rds", RDSCollector),
    ]
    if include_s3:
        collector_classes.append(("s3", S3Collector))

    # Run all collectors concurrently, each with independent error handling
    sem = asyncio.Semaphore(pipeline_cfg.concurrency)

    async def run_collector(name: str, cls: type) -> tuple[str, list[BaseResource], str | None]:
        async with sem:
            collector = cls(session, region, account_id, pipeline_cfg)
            resources, error = await collector.safe_collect()
            return name, resources, error

    tasks = [run_collector(name, cls) for name, cls in collector_classes]
    raw_results = await asyncio.gather(*tasks)

    inventory = RegionInventory(
        account_id=account_id,
        region=region,
        scan_timestamp=datetime.now(UTC),
    )
    for name, resources, error in raw_results:
        if error:
            inventory.collector_errors[name] = error
        for resource in resources:
            _add_to_inventory(inventory, resource)

    log.info(
        "region.collected",
        region=region,
        vpcs=len(inventory.vpcs),
        subnets=len(inventory.subnets),
        sgs=len(inventory.security_groups),
        ec2=len(inventory.ec2_instances),
        rds=len(inventory.rds_instances),
        rds_clusters=len(inventory.rds_clusters),
        s3=len(inventory.s3_buckets),
        errors=list(inventory.collector_errors.keys()),
    )
    return inventory


def _add_to_inventory(inventory: RegionInventory, resource: BaseResource) -> None:
    """Route a typed resource to the correct list in the inventory."""
    from collectors.resources import (
        EC2InstanceResource,
        InternetGatewayResource,
        NatGatewayResource,
        RDSClusterResource,
        RDSInstanceResource,
        RouteTableResource,
        S3BucketResource,
        SecurityGroupResource,
        SubnetResource,
        VPCResource,
    )

    match resource:
        case VPCResource():
            inventory.vpcs.append(resource)
        case SubnetResource():
            inventory.subnets.append(resource)
        case SecurityGroupResource():
            inventory.security_groups.append(resource)
        case InternetGatewayResource():
            inventory.internet_gateways.append(resource)
        case NatGatewayResource():
            inventory.nat_gateways.append(resource)
        case RouteTableResource():
            inventory.route_tables.append(resource)
        case EC2InstanceResource():
            inventory.ec2_instances.append(resource)
        case RDSInstanceResource():
            inventory.rds_instances.append(resource)
        case RDSClusterResource():
            inventory.rds_clusters.append(resource)
        case S3BucketResource():
            inventory.s3_buckets.append(resource)
        case _:
            log.warning("unknown_resource_type", resource_type=type(resource).__name__)


async def resolve_account_id(credential_provider: CredentialProvider) -> str:
    """Use STS GetCallerIdentity to discover the target account ID."""
    import aioboto3

    creds = await credential_provider.get_credentials()
    session = aioboto3.Session(
        aws_access_key_id=creds["aws_access_key_id"],
        aws_secret_access_key=creds["aws_secret_access_key"],
        aws_session_token=creds["aws_session_token"],
    )
    async with session.client("sts") as sts:
        identity = await sts.get_caller_identity()
        return identity["Account"]


async def run_scan(
    *,
    user_id: str,
    role_arn: str,
    external_id: str,
    regions: list[str] | None = None,
    settings: Settings | None = None,
) -> GraphDocument:
    """
    Full scan pipeline entry point.

    Accepts per-user AWS credentials so multiple users can each scan
    their own account in isolation.

    Steps:
      1. Auth  — assume role via STS using the user's role_arn + external_id
      2. Discover account ID and regions
      3. Collect — all regions concurrently (S3 collected once with first region)
      4. Process — merge inventories, build graph tagged with user_id
      5. Store  — MongoDB + Neo4j concurrently (tenant-isolated by user_id)
    """
    if settings is None:
        settings = get_settings()

    configure_logging(settings.log)
    scan_id = str(uuid.uuid4())
    started_at = datetime.now(UTC)

    log.info("scan.start", scan_id=scan_id, role_arn=role_arn, user_id=user_id)

    # ── 1. Auth ─────────────────────────────────────────────────────────────────
    credential_provider = CredentialProvider(
        role_arn=role_arn,
        external_id=external_id,
        session_duration_seconds=settings.aws.session_duration_seconds,
    )

    # ── 2. Discover ─────────────────────────────────────────────────────────────
    account_id = await resolve_account_id(credential_provider)
    structlog.contextvars.bind_contextvars(account_id=account_id, scan_id=scan_id)

    if not regions:
        regions = settings.aws.regions
    if not regions:
        regions = await discover_enabled_regions(credential_provider)
        log.info("regions.auto_discovered", count=len(regions), regions=regions)
    else:
        log.info("regions.configured", regions=regions)

    # ── 3. Collect ──────────────────────────────────────────────────────────────
    region_tasks = [
        collect_region(
            region=region,
            account_id=account_id,
            credential_provider=credential_provider,
            settings=settings,
            include_s3=(i == 0),  # collect S3 only with first region
        )
        for i, region in enumerate(regions)
    ]
    inventories = await asyncio.gather(*region_tasks, return_exceptions=False)

    # ── 4. Process ──────────────────────────────────────────────────────────────
    merged = merge_inventories(list(inventories))
    all_errors = {k: v for inv in inventories for k, v in inv.collector_errors.items()}

    metadata = ScanMetadata(
        user_id=user_id,
        account_id=account_id,
        scan_id=scan_id,
        started_at=started_at,
        regions_scanned=regions,
        collector_errors=all_errors,
    )

    builder = GraphBuilder()
    graph_doc = builder.build(merged, metadata)
    # Stamp user_id onto every edge so Neo4j MATCH can scope to this tenant
    for edge in graph_doc.edges:
        edge.properties["user_id"] = user_id
    graph_doc.metadata.completed_at = datetime.now(UTC)

    log.info(
        "scan.graph_built",
        nodes=graph_doc.metadata.node_count,
        edges=graph_doc.metadata.edge_count,
    )

    # ── 5. Store ─────────────────────────────────────────────────────────────────
    mongo_store = MongoStore(settings.mongo)
    neo4j_store = Neo4jStore(settings.neo4j)

    await asyncio.gather(
        mongo_store.connect(),
        neo4j_store.connect(),
    )
    try:
        await asyncio.gather(
            mongo_store.save_scan(graph_doc),
            neo4j_store.ingest_graph(graph_doc),
        )
    finally:
        await asyncio.gather(
            mongo_store.disconnect(),
            neo4j_store.disconnect(),
        )

    elapsed = (datetime.now(UTC) - started_at).total_seconds()
    log.info(
        "scan.complete",
        scan_id=scan_id,
        elapsed_seconds=round(elapsed, 2),
        nodes=graph_doc.metadata.node_count,
        edges=graph_doc.metadata.edge_count,
        errors=list(all_errors.keys()) or None,
    )
    return graph_doc



if __name__ == "__main__":
    _s = get_settings()
    asyncio.run(run_scan(
        user_id="cli",
        role_arn=_s.aws.role_arn,
        external_id=_s.aws.external_id.get_secret_value(),
        regions=_s.aws.regions,
        settings=_s,
    ))