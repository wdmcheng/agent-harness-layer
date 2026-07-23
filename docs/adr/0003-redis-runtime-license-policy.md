# ADR-0003: Redis runtime version and license-review policy

[English](0003-redis-runtime-license-policy.md) | [简体中文](0003-redis-runtime-license-policy.zh-CN.md)

- Status: Accepted
- Date: 2026-07-20
- Related: [ADR-0001](0001-p0-service-boundaries.md) · [release boundaries](../release-process.md) · [adapter contracts](../adapter-contracts.md)

## Context

The service profile needs a real durable queue to prove the API producer, runtime worker, receipt fencing, consumer groups, `XAUTOCLAIM`, and crash recovery. P0 originally chose the BSD-3-Clause Redis 7.2 line. On 2026-07-11, Compose was changed to `redis:8.0.1` by incorrectly aligning the redis-py client version with the server major version; that was not a valid compatibility or license basis. Phase 15 restored the approved license boundary and selected `7.2.14`, which includes 2026 security fixes in the 7.2 line. The Python client remains independently resolved by `uv.lock`.

## Decision

1. The service profile currently defaults to `redis:7.2.14@sha256:f0707c78ea880b293ccdeb410c9c0a8ccae93fe7128799b751333a698b0a39a7`. The full image references in `templates/service-app/docker-compose.yml` and `compliance/third-party.toml` are authoritative. An environment override must enter the corresponding smoke/release evidence.
2. Redis currently owns Streams consumer groups, claim/ack, and idempotent `RunQueue` only. It is not a session cache. New uses require separate data, capacity, security, and license evaluation.
3. The Redis adapter remains in `adapters/queue/redis.py`; API and worker exchange stable Pydantic refs only. Application code does not depend on Redis client objects.
4. Repository code remains Apache-2.0. Redis server is an external runtime and does not change the repository code license. The official versioned [`COPYING`](https://raw.githubusercontent.com/redis/redis/7.2.14/COPYING) for Redis `7.2.14` is normalized as `BSD-3-Clause`. Redis 7.4+ enters a different license system and cannot arrive as a routine patch under this P0 decision; an upgrade requires separate organizational legal/compliance review.
5. The redis-py client and Redis server are decided independently. Redis's official license page identifies redis-py as MIT; upgrades still recheck then-current lock metadata and upstream licenses.
6. A server/client upgrade, use change, image redistribution, hosted-service change, or first production release requires license/NOTICE review, queue contracts, and real service smoke with recorded image digest/server runtime version. `make license-check` does not replace this work.

Current disposition: the security-review trigger fired. Phase 15 corrected the mistaken `8.0.1` choice by returning to the approved 7.2 license line, selecting Redis `7.2.14`, binding the OCI index digest, and updating the server/client license boundary in `compliance/third-party.toml`. This pin is a reviewable build input only. Production use/release, distribution, hosting, and redistribution still require the review and evidence above.

This ADR records engineering gates and is not legal advice.

## Alternatives

- Continue using `redis:latest`: regression and license decisions cannot bind to an identifiable version. Rejected.
- Continue using Redis `7.2.4`: still BSD-3-Clause, but missing later 7.2 security fixes. Rejected.
- Use Redis 7.4+: enters RSALv2/SSPLv1 or later license boundaries and departs from the approved P0 BSD-3-Clause decision. Rejected as a patch upgrade.
- Switch queue/runtime now: changes validated recovery contracts and exceeds the Phase 14 documentation scope. A separate change is required.
- Skip Redis smoke because the local in-memory queue passes: does not prove cross-process receipt/claim/recovery. Rejected.

## Consequences

- `make smoke-service` is the real Redis-queue acceptance entry; `make smoke-local` cannot replace it.
- An image tag is reviewable but not an immutable digest. Phase 15 release evidence records the actual digest and server version.
- Repository scripts do not decide the organization's Redis license, distribution method, or NOTICE/source obligations.
- A future runtime replacement must preserve `RunQueue` contracts and durable-evidence semantics or change them through a new explicit contract.

## Evidence

```bash
uv lock --check
make license-check
make smoke-service
```

Code and test evidence: `templates/service-app/docker-compose.yml`, `packages/agent-harness/src/agent_harness/adapters/queue/redis.py`, `tests/integration/test_redis_run_queue_contracts.py`, and `tests/contracts/test_durable_run_queue_contracts.py`. License facts reference the official Redis `7.2.14` [`COPYING`](https://raw.githubusercontent.com/redis/redis/7.2.14/COPYING) and [Redis 7.2.14 release](https://github.com/redis/redis/releases/tag/7.2.14); Redis 7.4/8 boundaries reference the [Redis license page](https://redis.io/legal/licenses/).

## Review triggers

- The default or deployed `SERVICE_APP_REDIS_IMAGE` changes.
- The resolved redis-py version or server/client protocol capability changes.
- Redis expands from `RunQueue` into session cache, event store, or another use.
- The image is redistributed, resold, embedded in a hosted product, or the organization prepares a production release.
- Redis licensing, image source, or security-support status changes.
