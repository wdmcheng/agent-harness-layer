# ADR-0001: P0 service-profile deployment boundaries

[English](0001-p0-service-boundaries.md) | [简体中文](0001-p0-service-boundaries.zh-CN.md)

- Status: Accepted
- Date: 2026-07-11
- Related: [root README](../../README.md) · [architecture boundaries](../architecture/README.md) · [ADR-0002](0002-vendor-adapter-isolation.md) · [ADR-0003](0003-redis-runtime-license-policy.md)

## Context

The local profile supports offline development, but it cannot prove cross-process recovery across the HTTP producer, durable queue, DBOS workflow, checkpoints, and event stream. P0 needs copyable real-deployment evidence without presenting logical modules that are not physically separated as microservices.

## Decision

1. The service profile uses one wheel-only image with different commands for migration, FastAPI API, and runtime worker; PostgreSQL and Redis use separate containers.
2. The API does not execute an executor. RUN-001 persists execution context before writing a Pydantic queue DTO containing stable refs only to Redis. The worker restores identity and input truth from PostgreSQL.
3. The worker uses a stable DBOS executor ID. If a hard exit occurs after application run owner/workflow persistence, a replacement process must acquire singleton ownership before resuming the same DBOS workflow and `XAUTOCLAIM`-ing the original Redis entry.
4. `CanonicalEvent` uses a PostgreSQL sink. Database row locks, stable event IDs, and a unique terminal constraint provide cross-loop/cross-process atomicity. The in-process EventBus is not a distributed lock.
5. Isolated smoke generates service credentials temporarily. The database stores hashes only; plaintext never enters the repository, profiles, images, logs, or artifacts. Default cleanup removes this run's containers, network, volume, Redis namespace, credentials, and temporary files.
6. Future split order is runtime worker (completed by this ADR) → tool/model gateway → observability/event pipeline. Storage separates only after repository contracts stabilize.

## Boundary invariants

- Cross boundaries with Pydantic DTOs, `CanonicalEvent`, and repository/provider/facade interfaces only. Never pass ORM sessions, raw DBOS/provider objects, or mutable in-process globals.
- Queue messages retain `request_id`, effective `idempotency_key`, `tenant_id`, and `run_id`.
- `source_ref`, `trust_level`, context-assembly trace, guardrail/audit, and applicable correlation fields survive separation.
- Approval denial creates no continuation. Infrastructure failure after approval retains a re-enqueueable state; the handler is not replayed.

## Consequences

- `make smoke-service` is the acceptance entry for the service profile; local smoke and mocks cannot substitute.
- API and worker temporarily share PostgreSQL through stable repository contracts. This does not mean a storage service already exists.
- DBOS currently uses a singleton worker. Parallel scale under the same executor ID requires Conductor or a new coordination decision.
