# AWSVisualizer

A production-grade async Python tool that scans an AWS account and builds a full infrastructure knowledge graph, storing it in both **MongoDB** (document store for scan history and raw data) and **Neo4j** (graph database for relationship traversal and visualization).

The tool uses **zero static credentials** — it assumes an IAM role via AWS STS using the executor's own identity (EC2 instance role, ECS task role, Lambda execution role, or OIDC-federated identity). No access keys are ever stored or logged.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Execution Flow](#execution-flow)
3. [Project Structure](#project-structure)
4. [File Reference](#file-reference)
5. [Security Model](#security-model)
6. [Graph Model](#graph-model)
7. [Configuration Reference](#configuration-reference)
8. [Database Schema](#database-schema)
9. [Error Handling and Resilience](#error-handling-and-resilience)
10. [Running the Project](#running-the-project)
11. [Dependencies](#dependencies)

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              main.py: run_scan()                             │
├───────────────┬──────────────────────────────────────────────────────────────┤
│               │                                                              │
│   AUTH        │  CredentialProvider (STS AssumeRole)                         │
│               │  └─ Produces temporary credentials (15-min TTL, auto-refresh)│
│               │                                                              │
├───────────────┼──────────────────────────────────────────────────────────────┤
│               │                                                              │
│   DISCOVER    │  resolve_account_id() → STS GetCallerIdentity                │
│               │  discover_enabled_regions() → EC2 DescribeRegions            │
│               │                                                              │
├───────────────┼──────────────────────────────────────────────────────────────┤
│               │                                                              │
│   COLLECT     │  Per-region (all regions run concurrently):                  │
│  (async)      │  ├─ VPCCollector   → VPCs, Subnets, SGs, IGWs, NATs, RTs     │
│               │  ├─ EC2Collector   → EC2 Instances + ENIs                    │
│               │  ├─ RDSCollector   → DB Instances + Aurora Clusters          │
│               │  └─ S3Collector    → All Buckets (once, first region only)   │
│               │                                                              │
├───────────────┼──────────────────────────────────────────────────────────────┤
│               │                                                              │
│   PROCESS     │  merge_inventories() → single merged RegionInventory         │
│               │  GraphBuilder.build() → GraphDocument                        │
│               │  └─ Nodes + Edges + Group Indexes + ScanMetadata             │
│               │                                                              │
├───────────────┼──────────────────────────────────────────────────────────────┤
│               │                                                              │
│   STORE       │  MongoStore.save_scan()    → Full document in MongoDB        │
│  (parallel)   │  Neo4jStore.ingest_graph() → Nodes + edges in Neo4j          │
│               │                                                              │
└───────────────┴──────────────────────────────────────────────────────────────┘
```

Every stage is fully async. The collector stage runs all regions concurrently, and within each region all four collectors run concurrently behind a semaphore (`PIPELINE_CONCURRENCY`). The store stage runs MongoDB and Neo4j writes in parallel.

---

## Execution Flow

### Step 1 — Authentication

`CredentialProvider` (in `auth/main.py`) calls `STS:AssumeRole` using the executor's ambient identity (no static keys required). It receives temporary credentials with a configurable TTL (default 900 seconds). An `ExternalId` is always passed to prevent [confused-deputy attacks](https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html).

Credentials are cached in memory and automatically refreshed 5 minutes before they expire. A single asyncio lock prevents thundering-herd refreshes when multiple collectors start simultaneously.

### Step 2 — Account and Region Discovery

`resolve_account_id()` calls `STS:GetCallerIdentity` using the assumed-role credentials to get the numeric AWS account ID. This is used as a stable node ID for the account node in the graph.

If `AWS_REGIONS` is not set in the config, `discover_enabled_regions()` calls `EC2:DescribeRegions` (filtered to `opt-in-not-required` + `opted-in`) to get the full list of enabled regions. It falls back to a hard-coded safe set of 8 major regions if the API call fails.

### Step 3 — Collection

For each region, `collect_region()` creates an `aioboto3.Session` pre-loaded with the assumed-role credentials and runs all four collectors concurrently:

| Collector | AWS APIs Called | Resources Produced |
|-----------|----------------|-------------------|
| `VPCCollector` | `DescribeVpcs`, `DescribeSubnets`, `DescribeSecurityGroups`, `DescribeInternetGateways`, `DescribeNatGateways`, `DescribeRouteTables` | VPC, Subnet, SecurityGroup, InternetGateway, NatGateway, RouteTable |
| `EC2Collector` | `DescribeInstances` | EC2Instance (excludes terminated) |
| `RDSCollector` | `DescribeDBInstances`, `DescribeDBClusters` | RDSInstance, RDSCluster |
| `S3Collector` | `ListBuckets` + per-bucket detail calls | S3Bucket (global, collected once) |

Each collector extends `BaseCollector`, which provides:
- Automatic pagination via `_paginate()` — handles `NextToken` / `Marker` transparently
- Retry with exponential backoff + jitter via `tenacity` — retries on throttling, transient AWS errors, and network issues
- `safe_collect()` wrapper — catches all exceptions and returns `(resources, error_string)` so one failing collector never stops the others

Within `S3Collector`, up to 10 bucket enrichments run concurrently (semaphore-limited) to avoid overwhelming the S3 control plane. Each bucket fetches region, tags, versioning, encryption, public-access-block config, bucket policy existence, and logging status.

All resources are returned as Pydantic v2 models (`BaseResource` subclasses in `collectors/resources.py`) with strict typing and tag normalization.

### Step 4 — Graph Construction

`merge_inventories()` combines all per-region `RegionInventory` objects into one (S3 buckets are deduplicated by bucket name since S3 is global).

`GraphBuilder.build()` transforms the merged inventory into a `GraphDocument`:

1. Creates virtual `AWSAccount` and `Region` nodes
2. For each resource type, creates a `GraphNode` (AWS resource ID as the stable node ID) and all outbound `GraphEdge` relationships
3. Every edge carries `source_label` and `target_label` so Neo4j can use index-backed lookups
4. Deduplicates edges (same source + target + type is only written once)
5. Builds pre-computed `ResourceGroups` indexes (by VPC, subnet, app tag, environment tag, region) so the frontend can render different views without scanning all nodes

The full set of relationships created is documented in the [Graph Model](#graph-model) section.

### Step 5 — Storage

MongoDB and Neo4j writes happen in parallel via `asyncio.gather`.

**MongoDB** (`db/mongo.py`):
- Upserts the full `GraphDocument` into the `scans` collection using `scan_id` as `_id` (idempotent re-runs)
- Writes a lightweight summary to the `scan_index` collection for fast listing

**Neo4j** (`db/neo4j_store.py`):
- Writes all nodes first (grouped by label, batched in groups of 100)
- Writes all edges after (grouped by `(source_label, target_label, type)`, batched in groups of 100)
- Uses `MERGE` throughout — re-running a scan updates existing nodes/edges rather than duplicating them
- Uses label-qualified `MATCH` on edges to leverage uniqueness-constraint indexes
- Calls `result.consume()` on every query to ensure writes are committed before the session closes

---

## Project Structure

```
AWSVisualizer/
├── main.py                     # Orchestrator entry point
├── pyproject.toml              # Project metadata and dependencies
├── .env                        # Runtime configuration (see Configuration Reference)
│
├── auth/
│   └── main.py                 # STS credential provider + region discovery
│
├── collectors/
│   ├── base_collector.py       # Abstract base with pagination, retry, safe_collect
│   ├── resources.py            # Pydantic models for all AWS resources
│   └── sub_collectors/
│       ├── vpc.py              # VPC network topology collector
│       ├── ec2.py              # EC2 instance collector
│       ├── rds.py              # RDS instance and cluster collector
│       └── s3.py               # S3 bucket collector (global)
│
├── config/
│   └── main.py                 # Pydantic-settings config models
│
├── core/
│   ├── retry.py                # Tenacity retry factory + AWS error classification
│   └── pipeline.py             # Async pipeline engine (staged execution)
│
├── db/
│   ├── mongo.py                # Motor-based async MongoDB store
│   └── neo4j_store.py          # Neo4j async driver store
│
├── exceptions/
│   └── main.py                 # Domain exception hierarchy
│
├── graph/
│   ├── graph.py                # GraphDocument, GraphNode, GraphEdge, EdgeType models
│   └── graph_builder.py        # RegionInventory → GraphDocument converter
│
└── logger/
    └── logging.py              # Structlog configuration
```

---

## File Reference

### `main.py`

The top-level orchestrator. Intended to be run directly (`uv run main.py`).

| Function | Purpose |
|----------|---------|
| `run_scan(settings)` | Full pipeline: auth → discover → collect → build → store |
| `collect_region(...)` | Runs all collectors for one region concurrently behind a semaphore |
| `resolve_account_id(...)` | Calls STS GetCallerIdentity to get the numeric account ID |
| `_add_to_inventory(...)` | Routes a typed resource to the correct list via structural pattern matching |

---

### `auth/main.py`

| Class / Function | Purpose |
|-----------------|---------|
| `STSCredentials` | TypedDict holding `aws_access_key_id`, `aws_secret_access_key`, `aws_session_token` |
| `CredentialProvider` | Thread/asyncio-safe credential manager. Holds one set of temporary credentials and auto-refreshes them via an `asyncio.Lock` to prevent concurrent refreshes. |
| `CredentialProvider.get_credentials()` | Returns current credentials, refreshing if within 5 min of expiry |
| `CredentialProvider.get_session(region)` | Returns an `aioboto3.Session` pre-loaded with credentials for the given region |
| `discover_enabled_regions(...)` | Lists all opt-in-eligible regions; falls back to 8 safe defaults on error |

---

### `collectors/base_collector.py`

| Class / Method | Purpose |
|---------------|---------|
| `BaseCollector` | Abstract base. Constructed with `(session, region, account_id, pipeline_cfg)`. Subclasses implement `collect()`. |
| `safe_collect()` | Wraps `collect()` — returns `(resources, None)` on success, `([], error_string)` on failure. Never raises. |
| `_paginate(method, key, **kwargs)` | Calls the boto3 method repeatedly following pagination tokens, with retry on each page |
| `_call(method, key, **kwargs)` | Single non-paginated call, also with retry |

---

### `collectors/resources.py`

All models extend `BaseResource` (which uses `ConfigDict(extra="ignore")` to safely ignore unknown AWS API fields).

| Model | Key Fields |
|-------|-----------|
| `VPCResource` | `cidr_block`, `additional_cidrs`, `is_default` |
| `SubnetResource` | `vpc_id`, `cidr_block`, `availability_zone`, `is_public` (derived) |
| `SecurityGroupResource` | `vpc_id`, `inbound_rules`, `outbound_rules` (as `SecurityGroupRule` list) |
| `SecurityGroupRule` | `protocol`, `from_port`, `to_port`, `cidr_ranges`, `source_sg_ids` |
| `InternetGatewayResource` | `attached_vpc_ids` |
| `NatGatewayResource` | `vpc_id`, `subnet_id`, `state`, `public_ip`, `private_ip` |
| `RouteTableResource` | `vpc_id`, `associated_subnet_ids`, `is_main`, `has_igw_route` (derived) |
| `EC2InstanceResource` | `vpc_id`, `subnet_id`, `instance_type`, `state`, `security_group_ids`, `iam_instance_profile_arn`, `network_interfaces` |
| `NetworkInterfaceRef` | `eni_id`, `subnet_id`, `private_ip`, `public_ip`, `security_group_ids` |
| `RDSInstanceResource` | `vpc_id`, `subnet_ids`, `engine`, `instance_class`, `multi_az`, `storage_encrypted`, `db_cluster_identifier` |
| `RDSClusterResource` | `vpc_id`, `engine`, `member_instance_ids`, `storage_encrypted` |
| `S3BucketResource` | `bucket_region`, `versioning_enabled`, `server_side_encryption`, `public_access_block`, `is_public` (derived) |
| `S3BlockPublicAccess` | `block_public_acls`, `ignore_public_acls`, `block_public_policy`, `restrict_public_buckets`, `fully_blocked` (derived) |
| `RegionInventory` | Container for all resources from a region plus `collector_errors` dict |

---

### `collectors/sub_collectors/vpc.py`

Collects the full network topology in a single region. Derives two boolean flags:
- `SubnetResource.is_public` — a subnet is public if its associated route table (or the VPC's main route table) has a route targeting an Internet Gateway
- `RouteTableResource.has_igw_route` — set to `True` if any route targets an IGW

---

### `collectors/sub_collectors/s3.py`

S3 is a global service so this collector is only triggered for the **first region** in a scan. It runs bucket enrichments concurrently (semaphore of 10) to collect: bucket region, tags, versioning state, encryption configuration, public access block config, whether a bucket policy exists, and server access logging status.

`is_public` is derived: a bucket is considered public if `public_access_block.fully_blocked` is `False`.

---

### `core/retry.py`

Centralises retry logic for all AWS API calls using `tenacity`.

| Item | Detail |
|------|--------|
| Retryable error codes | `Throttling`, `ThrottlingException`, `RequestLimitExceeded`, `ServiceUnavailable`, `InternalError`, `InternalFailure`, `RequestTimeout`, `ConnectionError` |
| Backoff strategy | Exponential backoff with full jitter (random wait between `retry_wait_min` and `retry_wait_max`) |
| Max attempts | Configurable via `PIPELINE_MAX_RETRIES` (default: 3) |
| Non-retryable errors | `AuthError`, `CollectorError`, `ProcessingError`, `StorageError`, `PipelineError` |

---

### `core/pipeline.py`

A generic async pipeline engine supporting sequential named stages with per-stage status tracking (`PENDING`, `RUNNING`, `DONE`, `FAILED`, `SKIPPED`) and concurrency control. Available for future use — the current scan flow uses direct `asyncio.gather` in `main.py`.

---

### `config/main.py`

Pydantic-Settings v2 models. Each class reads from environment variables with a prefix, and all read from the `.env` file as a fallback.

| Class | Prefix | Key Settings |
|-------|--------|-------------|
| `AWSSettings` | `AWS_` | `role_arn`, `external_id`, `regions` (JSON array), `session_duration_seconds` |
| `MongoSettings` | `MONGO_` | `uri`, `db`, `tls`, `tls_ca_file` |
| `Neo4jSettings` | `NEO4J_` | `uri`, `user`, `password`, `database` |
| `PipelineSettings` | `PIPELINE_` | `max_retries`, `retry_wait_min_seconds`, `retry_wait_max_seconds`, `concurrency` |
| `LogSettings` | `LOG_` | `level`, `format` (`json` or `console`) |
| `Settings` | (none) | Aggregate of all above |

`get_settings()` is `@lru_cache(maxsize=1)` — the settings object is a singleton for the lifetime of the process.

---

### `graph/graph.py`

The core data contract for the knowledge graph.

| Class | Purpose |
|-------|---------|
| `NodeLabel` | Neo4j node labels: VPC, Subnet, SecurityGroup, InternetGateway, NatGateway, RouteTable, EC2Instance, RDSInstance, RDSCluster, S3Bucket, AWSAccount, Region |
| `EdgeType` | Relationship type enum: IN_VPC, IN_SUBNET, PROTECTED_BY, MEMBER_OF_CLUSTER, etc. |
| `GraphNode` | A single node: stable `id` (AWS resource ID), `label`, `properties`, and denormalized `vpc_id`/`subnet_id`/`region`/`account_id`/`app`/`environment` for fast frontend grouping |
| `GraphEdge` | A directed edge: `source` id, `target` id, `type`, `properties`, `source_label`, `target_label` |
| `ResourceGroups` | Pre-computed grouping indexes: `by_vpc`, `by_subnet`, `by_app`, `by_environment`, `by_region`, `untagged` |
| `ScanMetadata` | Provenance: `scan_id` (UUID), `account_id`, `started_at`, `completed_at`, `regions_scanned`, `collector_errors`, `node_count`, `edge_count` |
| `GraphDocument` | Top-level output: `metadata` + `nodes` + `edges` + `groups`. `to_mongo_dict()` adds `_id = scan_id` for MongoDB |

---

### `graph/graph_builder.py`

Transforms a `RegionInventory` into a `GraphDocument`.

**Node creation:** Every resource becomes a `GraphNode` with:
- `id` = AWS resource ID (stable, globally unique within account)
- `label` = mapped via `RESOURCE_TYPE_TO_LABEL`
- `properties` = type-specific fields (e.g. `instance_type` + `state` for EC2, `cidr_block` for VPC)
- Denormalized `vpc_id`, `subnet_id`, `region`, `account_id`, `app`, `environment` for fast frontend grouping

**Edge creation:** See [Graph Model](#graph-model) for the full relationship matrix.

**Deduplication:** A `set` of `(source, target, type)` tuples prevents duplicate edges in multi-region scans where the same resource may be referenced from multiple regions.

---

### `db/mongo.py`

Uses the Motor async driver. Two collections:

| Collection | Content | Index |
|-----------|---------|-------|
| `scans` | Full `GraphDocument` JSON per scan | `_id` = `scan_id` (unique), `metadata.account_id`, `metadata.started_at` |
| `scan_index` | Lightweight summary per scan | `account_id` + `started_at` |

`save_scan()` uses `replace_one(upsert=True)` — re-running the same scan (same `scan_id`) overwrites rather than duplicates.

---

### `db/neo4j_store.py`

Uses the official Neo4j async Python driver. All writes use `MERGE` for idempotency.

**Node writes** (`_write_nodes`):
```cypher
UNWIND $nodes AS n
MERGE (resource:Label {id: n.id})
SET resource += n.props
SET resource.updated_at = timestamp()
```

**Edge writes** (`_write_edges`):
```cypher
UNWIND $edges AS e
MATCH (src:SrcLabel {id: e.source})
MATCH (tgt:TgtLabel {id: e.target})
MERGE (src)-[r:REL_TYPE]->(tgt)
SET r += e.props
SET r.updated_at = timestamp()
```

Edges are grouped by `(source_label, target_label, type)` so each batch uses label-qualified `MATCH`, hitting uniqueness-constraint indexes rather than doing full node scans. `result.consume()` is called after every `session.run()` to ensure the auto-commit transaction is acknowledged before the session closes.

**Constraints created on first connect (12 total):**
```
UNIQUE id on: AWSAccount, Region, VPC, Subnet, SecurityGroup,
              EC2Instance, RDSInstance, RDSCluster, S3Bucket,
              InternetGateway, NatGateway, RouteTable
```

---

### `exceptions/main.py`

| Exception | Raised When |
|-----------|------------|
| `AWSMapperError` | Base — never raised directly |
| `AuthError` | STS AssumeRole fails or credential refresh fails |
| `CollectorError` | An AWS API call fails fatally after all retries |
| `ProcessingError` | Resource data validation or parsing fails |
| `StorageError` | MongoDB or Neo4j write fails |
| `PipelineError` | Overall pipeline orchestration fails |
| `ThrottleError` | AWS API returns a throttling response — signals the retry layer |

---

### `logger/logging.py`

Configures structlog globally. Two output formats:
- **`json`** (default): machine-readable JSON lines, suitable for CloudWatch Logs, Datadog, etc.
- **`console`**: coloured, human-readable output for local development

`account_id` and `scan_id` are bound as context variables via `structlog.contextvars.bind_contextvars()` once discovered, so every subsequent log line includes them automatically without being passed explicitly.

---

## Security Model

### Zero Static Credentials

The tool never stores, accepts, or logs AWS credentials. The only configuration required is the ARN of the role to assume. The calling identity (EC2 instance role, ECS task role, etc.) is resolved automatically by `aioboto3.Session()` via the standard AWS credential chain.

### STS AssumeRole with ExternalId

Every `AssumeRole` call includes an `ExternalId`. This prevents [confused-deputy attacks](https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html): even if an attacker tricks this tool into assuming a role, the `ExternalId` check on the target role's trust policy ensures only the intended caller can succeed.

### Minimum Required IAM Permissions

The assumed role (`AWS_ROLE_ARN`) needs read-only access:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeVpcs",
        "ec2:DescribeSubnets",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeInternetGateways",
        "ec2:DescribeNatGateways",
        "ec2:DescribeRouteTables",
        "ec2:DescribeInstances",
        "ec2:DescribeRegions",
        "rds:DescribeDBInstances",
        "rds:DescribeDBClusters",
        "s3:ListAllMyBuckets",
        "s3:GetBucketLocation",
        "s3:GetBucketTagging",
        "s3:GetBucketVersioning",
        "s3:GetBucketEncryption",
        "s3:GetBucketPublicAccessBlock",
        "s3:GetBucketPolicy",
        "s3:GetBucketLogging",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    }
  ]
}
```

The executor's identity (e.g. the EC2 instance role) also needs:

```json
{
  "Effect": "Allow",
  "Action": "sts:AssumeRole",
  "Resource": "arn:aws:iam::<target-account-id>:role/<role-name>"
}
```

### Target Role Trust Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::<executor-account-id>:role/<instance-role-name>"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "<your-external-id>"
        }
      }
    }
  ]
}
```

### Credential Lifetime

Temporary credentials live for `AWS_SESSION_DURATION_SECONDS` (default 900s, range 900–3600). They are refreshed 5 minutes before expiry and held in memory only — never written to disk, never logged.

### Secret Handling

`external_id` and all database passwords are declared as `pydantic.SecretStr`. They are never printed, included in `repr()` output, or serialized to logs. Access requires explicitly calling `.get_secret_value()`.

---

## Graph Model

### Node Types

| Label | Represents | Key Properties |
|-------|-----------|----------------|
| `AWSAccount` | The AWS account | `account_id` |
| `Region` | An AWS region | `region` |
| `VPC` | Virtual Private Cloud | `cidr_block`, `is_default` |
| `Subnet` | VPC subnet | `cidr_block`, `availability_zone`, `is_public` |
| `SecurityGroup` | Security group | `vpc_id`, `description` |
| `InternetGateway` | Internet gateway | — |
| `NatGateway` | NAT gateway | `public_ip`, `private_ip`, `state` |
| `RouteTable` | Route table | `is_main`, `has_igw_route` |
| `EC2Instance` | EC2 instance | `instance_type`, `state`, `private_ip`, `public_ip`, `platform` |
| `RDSInstance` | RDS DB instance | `engine`, `instance_class`, `multi_az`, `publicly_accessible`, `storage_encrypted` |
| `RDSCluster` | Aurora cluster | `engine`, `multi_az`, `storage_encrypted` |
| `S3Bucket` | S3 bucket | `bucket_region`, `is_public`, `versioning_enabled`, `server_side_encryption` |

All nodes also carry: `name`, `region`, `account_id`, `resource_type`, `app` (from tag), `environment` (from tag), `updated_at`.

### Relationship Types

| Relationship | From → To | Meaning |
|-------------|-----------|---------|
| `BELONGS_TO_ACCOUNT` | Region → AWSAccount | Region belongs to account |
| `BELONGS_TO_ACCOUNT` | S3Bucket → AWSAccount | S3 is global — belongs directly to account, not a region |
| `IN_REGION` | VPC → Region | VPC exists in this region |
| `IN_VPC` | Subnet → VPC | Subnet is contained in VPC |
| `IN_VPC` | SecurityGroup → VPC | SG is scoped to this VPC |
| `IN_VPC` | NatGateway → VPC | NAT gateway resides in VPC |
| `IN_VPC` | RouteTable → VPC | Route table belongs to VPC |
| `IN_VPC` | EC2Instance → VPC | Instance primary network is in VPC |
| `IN_VPC` | RDSInstance → VPC | DB instance is in VPC |
| `IN_VPC` | RDSCluster → VPC | Cluster is in VPC |
| `IN_SUBNET` | NatGateway → Subnet | NAT gateway is placed in this subnet |
| `IN_SUBNET` | EC2Instance → Subnet | Instance primary ENI is in this subnet |
| `IN_SUBNET` | RDSInstance → Subnet | DB instance has a subnet group member (one edge per subnet) |
| `ATTACHED_TO_VPC` | InternetGateway → VPC | IGW is attached to VPC |
| `HAS_ROUTE_TABLE` | Subnet → RouteTable | Subnet has an explicit route table association |
| `PROTECTED_BY` | EC2Instance → SecurityGroup | Instance is governed by this SG |
| `PROTECTED_BY` | RDSInstance → SecurityGroup | DB instance is governed by this SG |
| `PROTECTED_BY` | RDSCluster → SecurityGroup | Cluster is governed by this SG |
| `MEMBER_OF_CLUSTER` | RDSInstance → RDSCluster | DB instance is a member of this Aurora cluster |
| `SG_REFERENCES_SG` | SecurityGroup → SecurityGroup | An inbound rule references another SG as the traffic source |

### Example Subgraph

```
(AWSAccount: 123456789012)
        ↑ BELONGS_TO_ACCOUNT
(Region: us-east-1)
        ↑ IN_REGION
(VPC: vpc-abc)
   ↑ IN_VPC          ↑ IN_VPC             ↑ ATTACHED_TO_VPC
(Subnet: sub-1)  (SecurityGroup: sg-1)  (InternetGateway: igw-1)
   ↑ IN_SUBNET             ↑ PROTECTED_BY
(EC2Instance: i-123) ──PROTECTED_BY──→ (SecurityGroup: sg-1)
```

### Useful Cypher Queries

```cypher
-- All EC2 instances in a VPC with their security groups
MATCH (i:EC2Instance)-[:IN_VPC]->(v:VPC {id: "vpc-abc"})
MATCH (i)-[:PROTECTED_BY]->(sg:SecurityGroup)
RETURN i.name, i.instance_type, collect(sg.id)

-- All resources in a specific subnet
MATCH (n)-[:IN_SUBNET]->(s:Subnet {id: "subnet-xyz"})
RETURN labels(n), n.id, n.name

-- Public-facing EC2 instances (in a public subnet with a public IP)
MATCH (i:EC2Instance)-[:IN_SUBNET]->(s:Subnet {is_public: true})
WHERE i.public_ip IS NOT NULL
RETURN i.id, i.public_ip, s.id

-- RDS instances that are publicly accessible
MATCH (r:RDSInstance {publicly_accessible: true})
RETURN r.id, r.engine, r.endpoint_address

-- Security groups that allow all inbound traffic (potential risk)
MATCH (sg:SecurityGroup)
WHERE "0.0.0.0/0" IN sg.inbound_cidr_ranges
MATCH (resource)-[:PROTECTED_BY]->(sg)
RETURN resource, sg

-- Full neighbourhood of a resource (2 hops)
MATCH path = (start {id: "i-0abc123"})-[*1..2]-()
RETURN path
```

---

## Configuration Reference

All settings are loaded from environment variables or the `.env` file in the project root. Complex types (lists) must use JSON array syntax.

### AWS Settings (`AWS_` prefix)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AWS_ROLE_ARN` | Yes | — | ARN of the IAM role to assume in the target account |
| `AWS_EXTERNAL_ID` | Yes | — | External ID for confused-deputy protection |
| `AWS_REGIONS` | No | (auto-discover) | JSON array of regions, e.g. `["us-east-1","eu-west-1"]`. Omit to scan all enabled regions |
| `AWS_SESSION_DURATION_SECONDS` | No | `900` | Temporary credential TTL in seconds (900–3600) |

### MongoDB Settings (`MONGO_` prefix)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MONGO_URI` | No | `mongodb://localhost:27017` | MongoDB connection URI |
| `MONGO_DB` | No | `aws_mapper` | Database name |
| `MONGO_TLS` | No | `false` | Enable TLS |
| `MONGO_TLS_CA_FILE` | No | — | Path to CA certificate for TLS |

### Neo4j Settings (`NEO4J_` prefix)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NEO4J_URI` | No | `bolt://localhost:7687` | Neo4j connection URI |
| `NEO4J_USER` | No | `neo4j` | Database username |
| `NEO4J_PASSWORD` | No | `neo4j` | Database password |
| `NEO4J_DATABASE` | No | `neo4j` | Target database name |

### Pipeline Settings (`PIPELINE_` prefix)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PIPELINE_MAX_RETRIES` | No | `3` | Max retry attempts per AWS API call (1–10) |
| `PIPELINE_RETRY_WAIT_MIN_SECONDS` | No | `2.0` | Minimum wait between retries |
| `PIPELINE_RETRY_WAIT_MAX_SECONDS` | No | `30.0` | Maximum wait between retries |
| `PIPELINE_CONCURRENCY` | No | `5` | Max concurrent collectors per region (1–20) |

### Logging Settings (`LOG_` prefix)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LOG_LEVEL` | No | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FORMAT` | No | `json` | Output format: `json` (production) or `console` (development) |

---

## Database Schema

### MongoDB

```
Database: aws_mapper

Collection: scans
  _id:           <scan_id>  (UUID string, unique per scan)
  metadata:
    account_id:       string
    scan_id:          string
    started_at:       datetime
    completed_at:     datetime
    regions_scanned:  [string]
    collector_errors: { service_name: error_message }
    node_count:       int
    edge_count:       int
  nodes: [
    {
      id:           string   (AWS resource ID)
      label:        string   (NodeLabel value)
      properties:   object   (resource-specific fields)
      vpc_id:       string?
      subnet_id:    string?
      region:       string?
      account_id:   string?
      app:          string?  (from App/Application tag)
      environment:  string?  (from Env/Environment tag)
      name:         string?
    }
  ]
  edges: [
    {
      source:       string   (source node id)
      target:       string   (target node id)
      type:         string   (EdgeType value)
      properties:   object
      source_label: string?
      target_label: string?
    }
  ]
  groups:
    by_vpc:         { vpc_id:    [{ id, label, name }] }
    by_subnet:      { subnet_id: [{ id, label, name }] }
    by_app:         { app_tag:   [{ id, label, name }] }
    by_environment: { env_tag:   [{ id, label, name }] }
    by_region:      { region:    [{ id, label, name }] }
    untagged:       [{ id, label, name }]

Collection: scan_index
  _id:        <scan_id>
  account_id: string
  started_at: datetime
  node_count: int
  edge_count: int
  regions:    [string]
  errors:     { service_name: error_message }
```

### Neo4j

```
Node labels (12):
  AWSAccount, Region, VPC, Subnet, SecurityGroup,
  InternetGateway, NatGateway, RouteTable,
  EC2Instance, RDSInstance, RDSCluster, S3Bucket

Uniqueness constraints (one per label):
  REQUIRE n.id IS UNIQUE

Relationship types:
  BELONGS_TO_ACCOUNT, IN_REGION, IN_VPC, IN_SUBNET,
  ATTACHED_TO_VPC, HAS_ROUTE_TABLE, PROTECTED_BY,
  MEMBER_OF_CLUSTER, SG_REFERENCES_SG

All relationships carry:
  updated_at: timestamp (epoch ms)
  + type-specific properties (e.g. protocol/from_port on SG_REFERENCES_SG)
```

---

## Error Handling and Resilience

### Partial Scan Success

Each collector runs independently. If one fails (e.g. RDS returns an access denied), the error is captured in `RegionInventory.collector_errors` and the rest of the scan continues. The final `GraphDocument.metadata.collector_errors` surfaces all failures so they are visible in logs and stored in MongoDB.

### Retry Logic

All AWS API calls go through `core/retry.py`:
- **Retries on:** `Throttling`, `ThrottlingException`, `RequestLimitExceeded`, `ServiceUnavailable`, `InternalError`, network timeouts
- **Does not retry:** auth errors, permanent client errors (4xx), domain exceptions (`AuthError`, `CollectorError`, etc.)
- **Backoff:** exponential with full jitter — random wait between `PIPELINE_RETRY_WAIT_MIN_SECONDS` and `PIPELINE_RETRY_WAIT_MAX_SECONDS`

### Idempotency

- MongoDB uses `replace_one(upsert=True)` keyed on `scan_id` — safe to run twice with the same scan ID
- Neo4j uses `MERGE` on every node and edge — re-running updates `updated_at` without duplicating data

### Neo4j Write Safety

- `result.consume()` is called after every `session.run()` to ensure the auto-commit transaction is flushed before the session closes
- Nodes are always written before edges, preventing dangling reference issues
- Edges are grouped by `(source_label, target_label, type)` so MATCH uses the per-label uniqueness indexes instead of doing expensive full-graph scans that could timeout on large accounts

---

## Running the Project

### Prerequisites

- Python 3.13+
- [uv](https://github.com/astral-sh/uv)
- Running MongoDB instance
- Running Neo4j instance
- An IAM role configured as described in [Security Model](#security-model)

### Local Setup

```bash
cd AWSVisualizer

# Install dependencies
uv sync

# Configure environment
# Edit .env with your role ARN, external ID, and DB connection strings
```

### `.env` Example

```dotenv
AWS_ROLE_ARN=arn:aws:iam::123456789012:role/AWSMapperReadOnlyRole
AWS_EXTERNAL_ID=your-external-id-here
AWS_REGIONS=["us-east-1","eu-west-1"]

MONGO_URI=mongodb://localhost:27017
MONGO_DB=aws_mapper

NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-password

LOG_FORMAT=console
```

### Running

```bash
uv run main.py
```

### Running on EC2

Deploy to an EC2 instance with an IAM instance role that has `sts:AssumeRole` permission on the target role. No credential configuration is needed — the AWS SDK resolves the instance role automatically from the EC2 metadata service.

```bash
# On the EC2 instance
git clone <repo-url>
cd AWSVisualizer
# Edit .env: set role ARN, external ID, and DB endpoints
uv run main.py
```

Ensure:
1. The EC2 instance role has `sts:AssumeRole` permission on `AWS_ROLE_ARN`
2. The target role's trust policy allows the instance role as principal with the matching `ExternalId`
3. `MONGO_URI` and `NEO4J_URI` point to reachable endpoints (update from `localhost` if using remote DBs)

### Log Output (JSON format)

```json
{"scan_id": "abc-123", "event": "scan.start", "level": "info", "timestamp": "2026-04-08T10:00:00Z"}
{"scan_id": "abc-123", "account_id": "123456789012", "event": "regions.configured", "regions": ["us-east-1"], "level": "info", "timestamp": "..."}
{"scan_id": "abc-123", "account_id": "123456789012", "event": "region.collected", "region": "us-east-1", "vpcs": 3, "ec2": 12, "rds": 2, "s3": 5, "errors": [], "level": "info", "timestamp": "..."}
{"scan_id": "abc-123", "account_id": "123456789012", "event": "scan.complete", "elapsed_seconds": 42.1, "nodes": 87, "edges": 134, "errors": null, "level": "info", "timestamp": "..."}
```

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `aioboto3` | ≥15.5.0 | Async AWS SDK — all AWS API calls |
| `motor` | ≥3.7.1 | Async MongoDB driver (built on PyMongo) |
| `neo4j` | ≥6.1.0 | Official Neo4j async Python driver |
| `pydantic` | ≥2.12.5 | Data validation for all resource and config models |
| `pydantic-settings` | ≥2.13.1 | Environment variable and `.env` file loading |
| `pymongo` | ≥4.16.0 | Underlying MongoDB client (used by Motor) |
| `structlog` | ≥25.5.0 | Structured, contextual logging |
| `tenacity` | ≥9.1.4 | Retry with exponential backoff for AWS API calls |
