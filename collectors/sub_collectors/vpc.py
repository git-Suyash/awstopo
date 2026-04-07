"""VPC collector — gathers the full network topology for a region."""

from __future__ import annotations

from typing import Any

from collectors.base_collector import BaseCollector
from collectors.resources import (
    BaseResource,
    CidrBlock,
    InternetGatewayResource,
    NatGatewayResource,
    RouteTableResource,
    SecurityGroupResource,
    SecurityGroupRule,
    SubnetResource,
    VPCResource,
    parse_tags,
)


class VPCCollector(BaseCollector):
    service = "ec2"

    async def collect(self) -> list[BaseResource]:
        resources: list[BaseResource] = []

        vpcs = await self._collect_vpcs()
        subnets = await self._collect_subnets()
        sgs = await self._collect_security_groups()
        igws = await self._collect_internet_gateways()
        nat_gws = await self._collect_nat_gateways()
        route_tables = await self._collect_route_tables()

        # Mark subnets as public if their route table has a default → IGW route
        igw_ids = {igw.resource_id for igw in igws}
        has_igw_route: set[str] = set()
        for rt in route_tables:
            if rt.has_igw_route:
                has_igw_route.update(rt.associated_subnet_ids)

        for subnet in subnets:
            if subnet.resource_id in has_igw_route:
                subnet.is_public = True

        resources.extend(vpcs)
        resources.extend(subnets)
        resources.extend(sgs)
        resources.extend(igws)
        resources.extend(nat_gws)
        resources.extend(route_tables)
        return resources

    async def _collect_vpcs(self) -> list[VPCResource]:
        raw = await self._paginate("describe_vpcs", "Vpcs")
        result = []
        for v in raw:
            tags = parse_tags(v.get("Tags"))
            additional = [
                CidrBlock(cidr=a["CidrBlock"], state=a.get("State", "associated"))
                for a in v.get("Ipv6CidrBlockAssociationSet", [])
                + v.get("CidrBlockAssociationSet", [])[1:]  # skip first (primary)
            ]
            result.append(
                VPCResource(
                    resource_id=v["VpcId"],
                    region=self._region,
                    account_id=self._account_id,
                    name=tags.get("Name"),
                    tags=tags,
                    cidr_block=v["CidrBlock"],
                    additional_cidrs=additional,
                    is_default=v.get("IsDefault", False),
                    dhcp_options_id=v.get("DhcpOptionsId"),
                    state=v.get("State", "available"),
                    raw=v,
                )
            )
        self._log.info("vpcs.collected", count=len(result))
        return result

    async def _collect_subnets(self) -> list[SubnetResource]:
        raw = await self._paginate("describe_subnets", "Subnets")
        result = []
        for s in raw:
            tags = parse_tags(s.get("Tags"))
            result.append(
                SubnetResource(
                    resource_id=s["SubnetId"],
                    region=self._region,
                    account_id=self._account_id,
                    name=tags.get("Name"),
                    tags=tags,
                    vpc_id=s["VpcId"],
                    cidr_block=s["CidrBlock"],
                    availability_zone=s["AvailabilityZone"],
                    available_ip_count=s.get("AvailableIpAddressCount", 0),
                    map_public_ip_on_launch=s.get("MapPublicIpOnLaunch", False),
                    state=s.get("State", "available"),
                    raw=s,
                )
            )
        self._log.info("subnets.collected", count=len(result))
        return result

    async def _collect_security_groups(self) -> list[SecurityGroupResource]:
        raw = await self._paginate("describe_security_groups", "SecurityGroups")
        result = []
        for sg in raw:
            tags = parse_tags(sg.get("Tags"))
            result.append(
                SecurityGroupResource(
                    resource_id=sg["GroupId"],
                    region=self._region,
                    account_id=self._account_id,
                    name=tags.get("Name") or sg.get("GroupName"),
                    tags=tags,
                    vpc_id=sg.get("VpcId", ""),
                    description=sg.get("Description", ""),
                    inbound_rules=self._parse_rules(sg.get("IpPermissions", [])),
                    outbound_rules=self._parse_rules(sg.get("IpPermissionsEgress", [])),
                    raw=sg,
                )
            )
        self._log.info("security_groups.collected", count=len(result))
        return result

    def _parse_rules(self, permissions: list[dict[str, Any]]) -> list[SecurityGroupRule]:
        rules = []
        for perm in permissions:
            rules.append(
                SecurityGroupRule(
                    protocol=perm.get("IpProtocol", "-1"),
                    from_port=perm.get("FromPort"),
                    to_port=perm.get("ToPort"),
                    cidr_ranges=[r["CidrIp"] for r in perm.get("IpRanges", [])],
                    source_sg_ids=[p["GroupId"] for p in perm.get("UserIdGroupPairs", [])],
                    description=perm.get("Description"),
                )
            )
        return rules

    async def _collect_internet_gateways(self) -> list[InternetGatewayResource]:
        raw = await self._paginate("describe_internet_gateways", "InternetGateways")
        result = []
        for igw in raw:
            tags = parse_tags(igw.get("Tags"))
            attached = [
                a["VpcId"]
                for a in igw.get("Attachments", [])
                if a.get("State") == "available"
            ]
            result.append(
                InternetGatewayResource(
                    resource_id=igw["InternetGatewayId"],
                    region=self._region,
                    account_id=self._account_id,
                    name=tags.get("Name"),
                    tags=tags,
                    attached_vpc_ids=attached,
                    raw=igw,
                )
            )
        self._log.info("igws.collected", count=len(result))
        return result

    async def _collect_nat_gateways(self) -> list[NatGatewayResource]:
        raw = await self._paginate(
            "describe_nat_gateways",
            "NatGateways",
            Filter=[{"Name": "state", "Values": ["available", "pending"]}],
        )
        result = []
        for nat in raw:
            tags = parse_tags(nat.get("Tags"))
            addresses = nat.get("NatGatewayAddresses", [{}])
            first_addr = addresses[0] if addresses else {}
            result.append(
                NatGatewayResource(
                    resource_id=nat["NatGatewayId"],
                    region=self._region,
                    account_id=self._account_id,
                    name=tags.get("Name"),
                    tags=tags,
                    vpc_id=nat["VpcId"],
                    subnet_id=nat["SubnetId"],
                    state=nat.get("State", ""),
                    public_ip=first_addr.get("PublicIp"),
                    private_ip=first_addr.get("PrivateIp"),
                    raw=nat,
                )
            )
        self._log.info("nat_gateways.collected", count=len(result))
        return result

    async def _collect_route_tables(self) -> list[RouteTableResource]:
        raw = await self._paginate("describe_route_tables", "RouteTables")
        result = []
        for rt in raw:
            tags = parse_tags(rt.get("Tags"))
            associated_subnets = [
                a["SubnetId"]
                for a in rt.get("Associations", [])
                if "SubnetId" in a
            ]
            is_main = any(a.get("Main") for a in rt.get("Associations", []))
            routes = rt.get("Routes", [])
            # Route to 0.0.0.0/0 via an IGW = public route table
            has_igw_route = any(
                r.get("DestinationCidrBlock") in ("0.0.0.0/0", "::/0")
                and r.get("GatewayId", "").startswith("igw-")
                for r in routes
            )
            result.append(
                RouteTableResource(
                    resource_id=rt["RouteTableId"],
                    region=self._region,
                    account_id=self._account_id,
                    name=tags.get("Name"),
                    tags=tags,
                    vpc_id=rt["VpcId"],
                    associated_subnet_ids=associated_subnets,
                    is_main=is_main,
                    routes=routes,
                    has_igw_route=has_igw_route,
                    raw=rt,
                )
            )
        self._log.info("route_tables.collected", count=len(result))
        return result
