"""RDS collector — instances and Aurora clusters."""

from __future__ import annotations

from collectors.base_collector import BaseCollector
from collectors.resources import (
    BaseResource,
    RDSClusterResource,
    RDSInstanceResource,
    parse_tags,
)


class RDSCollector(BaseCollector):
    service = "rds"

    async def collect(self) -> list[BaseResource]:
        resources: list[BaseResource] = []
        resources.extend(await self._collect_instances())
        resources.extend(await self._collect_clusters())
        return resources

    async def _collect_instances(self) -> list[RDSInstanceResource]:
        raw = await self._paginate("describe_db_instances", "DBInstances")
        result = []
        for db in raw:
            tags = parse_tags(db.get("TagList"))
            # Extract subnet IDs from the subnet group
            subnet_ids = [
                s["SubnetIdentifier"]
                for s in db.get("DBSubnetGroup", {}).get("Subnets", [])
            ]
            result.append(
                RDSInstanceResource(
                    resource_id=db["DBInstanceIdentifier"],
                    region=self._region,
                    account_id=self._account_id,
                    name=tags.get("Name") or db["DBInstanceIdentifier"],
                    tags=tags,
                    vpc_id=db.get("DBSubnetGroup", {}).get("VpcId"),
                    subnet_ids=subnet_ids,
                    security_group_ids=[g["VpcSecurityGroupId"] for g in db.get("VpcSecurityGroups", [])],
                    engine=db.get("Engine", ""),
                    engine_version=db.get("EngineVersion", ""),
                    instance_class=db.get("DBInstanceClass", ""),
                    status=db.get("DBInstanceStatus", ""),
                    endpoint_address=db.get("Endpoint", {}).get("Address"),
                    endpoint_port=db.get("Endpoint", {}).get("Port"),
                    multi_az=db.get("MultiAZ", False),
                    publicly_accessible=db.get("PubliclyAccessible", False),
                    storage_encrypted=db.get("StorageEncrypted", False),
                    db_cluster_identifier=db.get("DBClusterIdentifier"),
                    db_subnet_group_name=db.get("DBSubnetGroup", {}).get("DBSubnetGroupName"),
                    availability_zone=db.get("AvailabilityZone"),
                    backup_retention_days=db.get("BackupRetentionPeriod", 0),
                    deletion_protection=db.get("DeletionProtection", False),
                    raw=db,
                )
            )
        self._log.info("rds_instances.collected", count=len(result))
        return result

    async def _collect_clusters(self) -> list[RDSClusterResource]:
        raw = await self._paginate("describe_db_clusters", "DBClusters")
        result = []
        for cluster in raw:
            tags = parse_tags(cluster.get("TagList"))
            # Collect subnet IDs from the cluster's subnet group if present
            subnet_ids: list[str] = []
            result.append(
                RDSClusterResource(
                    resource_id=cluster["DBClusterIdentifier"],
                    region=self._region,
                    account_id=self._account_id,
                    name=tags.get("Name") or cluster["DBClusterIdentifier"],
                    tags=tags,
                    vpc_id=cluster.get("VpcId") or cluster.get("DbClusterResourceId"),
                    subnet_ids=subnet_ids,
                    security_group_ids=[
                        g["VpcSecurityGroupId"] for g in cluster.get("VpcSecurityGroups", [])
                    ],
                    engine=cluster.get("Engine", ""),
                    engine_version=cluster.get("EngineVersion", ""),
                    status=cluster.get("Status", ""),
                    endpoint=cluster.get("Endpoint"),
                    reader_endpoint=cluster.get("ReaderEndpoint"),
                    port=cluster.get("Port"),
                    multi_az=cluster.get("MultiAZ", False),
                    storage_encrypted=cluster.get("StorageEncrypted", False),
                    backup_retention_days=cluster.get("BackupRetentionPeriod", 0),
                    deletion_protection=cluster.get("DeletionProtection", False),
                    member_instance_ids=[
                        m["DBInstanceIdentifier"]
                        for m in cluster.get("DBClusterMembers", [])
                    ],
                    raw=cluster,
                )
            )
        self._log.info("rds_clusters.collected", count=len(result))
        return result
