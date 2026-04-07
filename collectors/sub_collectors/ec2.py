"""EC2 instance collector."""

from __future__ import annotations

from collectors.base_collector import BaseCollector
from collectors.resources import (
    BaseResource,
    EC2InstanceResource,
    EC2State,
    NetworkInterfaceRef,
    parse_tags,
)


class EC2Collector(BaseCollector):
    service = "ec2"

    async def collect(self) -> list[BaseResource]:
        # Exclude terminated instances — they're noise
        reservations = await self._paginate(
            "describe_instances",
            "Reservations",
            Filters=[{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}],
        )
        instances: list[BaseResource] = []
        for reservation in reservations:
            for inst in reservation.get("Instances", []):
                tags = parse_tags(inst.get("Tags"))
                enis = [
                    NetworkInterfaceRef(
                        eni_id=eni["NetworkInterfaceId"],
                        subnet_id=eni.get("SubnetId"),
                        private_ip=eni.get("PrivateIpAddress"),
                        public_ip=eni.get("Association", {}).get("PublicIp"),
                        security_group_ids=[g["GroupId"] for g in eni.get("Groups", [])],
                    )
                    for eni in inst.get("NetworkInterfaces", [])
                ]
                instances.append(
                    EC2InstanceResource(
                        resource_id=inst["InstanceId"],
                        region=self._region,
                        account_id=self._account_id,
                        name=tags.get("Name"),
                        tags=tags,
                        vpc_id=inst.get("VpcId"),
                        subnet_id=inst.get("SubnetId"),
                        instance_type=inst["InstanceType"],
                        state=EC2State(inst["State"]["Name"]),
                        private_ip=inst.get("PrivateIpAddress"),
                        public_ip=inst.get("PublicIpAddress"),
                        security_group_ids=[g["GroupId"] for g in inst.get("SecurityGroups", [])],
                        iam_instance_profile_arn=inst.get("IamInstanceProfile", {}).get("Arn"),
                        image_id=inst.get("ImageId", ""),
                        key_name=inst.get("KeyName"),
                        launch_time=inst.get("LaunchTime"),
                        availability_zone=inst.get("Placement", {}).get("AvailabilityZone"),
                        network_interfaces=enis,
                        platform=inst.get("Platform", "linux"),
                        monitoring_enabled=inst.get("Monitoring", {}).get("State") == "enabled",
                        raw=inst,
                    )
                )
        self._log.info("ec2_instances.collected", count=len(instances))
        return instances
