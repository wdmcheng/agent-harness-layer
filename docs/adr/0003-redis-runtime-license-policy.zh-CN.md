# ADR-0003：Redis Runtime 版本与许可证复审策略

[English](0003-redis-runtime-license-policy.md) | [简体中文](0003-redis-runtime-license-policy.zh-CN.md)

- 状态：Accepted
- 日期：2026-07-20
- 关联：[ADR-0001](0001-p0-service-boundaries.zh-CN.md) · [Release 边界](../release-process.zh-CN.md) · [Adapter 合同](../adapter-contracts.zh-CN.md)

## 背景

service profile 需要真实 durable queue 来证明 API producer、runtime worker、receipt fencing、consumer group、`XAUTOCLAIM` 和 crash recovery。P0 原决策为使用 BSD-3-Clause 的 Redis 7.2 线；2026-07-11 曾因误把 redis-py client 版本与 server 主版本对齐，将 Compose 改为 `redis:8.0.1`，这不构成有效的兼容性或许可证依据。Phase 15 恢复原许可证边界，并把 7.2 线更新到包含 2026 安全修复的 `7.2.14`。Python client 的解析版本继续由 `uv.lock` 独立管理。

## 决策

1. service profile 当前默认使用 `redis:7.2.14@sha256:f0707c78ea880b293ccdeb410c9c0a8ccae93fe7128799b751333a698b0a39a7`；版本真相以 `templates/service-app/docker-compose.yml` 和 `compliance/third-party.toml` 的完整 image reference 为准。环境变量覆盖后，覆盖值必须进入该次 smoke/release evidence。
2. Redis 当前只承担 Streams consumer group、claim/ack 与幂等 RunQueue；不承担 session cache。扩展用途必须另行评估数据、容量、安全和许可证边界。
3. Redis adapter 保持在 `adapters/queue/redis.py`，跨 API/worker 只传稳定 Pydantic refs；应用代码不依赖 Redis client object。
4. 仓库代码继续使用 Apache-2.0；Redis server 是外部 runtime，不因此变成本仓库代码许可证。Redis `7.2.14` 的版本许可证事实以该 tag 的官方 [`COPYING`](https://raw.githubusercontent.com/redis/redis/7.2.14/COPYING) 为准，规范化记录为 `BSD-3-Clause`。Redis 7.4+ 已进入不同许可证体系，不得作为普通补丁升级混入当前 P0 决策；若升级，必须另行完成组织法律/合规审查。
5. redis-py client 与 server 分开裁决；Redis 官方许可页说明 redis-py 为 MIT。版本升级时仍需以当时的 lock metadata 和上游许可证重新核验。
6. Redis server/client 升级、用途改变、镜像再分发、托管服务形态变化或首次生产发布前，必须重跑 license/NOTICE review、queue contract、真实 service smoke，并记录 image digest/server runtime version。当前 `make license-check` 不替代这些工作。

当前处置状态：安全复审条件已经触发；Phase 15 重新选择补丁版本，将 service profile 从错误的 `8.0.1` 恢复到批准的 7.2 许可证线，并选择 Redis `7.2.14`、绑定 OCI index digest，同时更新 `compliance/third-party.toml` 的 server/client license 边界。该 pin 只代表仓库可复核的构建输入；生产使用或发布前，以及生产分发、托管和再发布场景，仍须按本 ADR 重跑 license/NOTICE review、queue contract、真实 service smoke，并记录实际 runtime 版本。

本文记录工程门禁，不提供法律意见。

## 替代方案

- 继续用 `redis:latest`：无法把回归和许可证判断绑定到可识别版本，拒绝。
- 继续使用 Redis `7.2.4`：仍是 BSD-3-Clause，但缺少 7.2 后续安全修复，拒绝。
- 使用 Redis 7.4+：进入 RSALv2/SSPLv1 或其后续许可边界，偏离 P0 已批准的 BSD-3-Clause 决策，拒绝作为本次补丁升级。
- 当前阶段切换其他 queue/runtime：会改变已验证的 recovery 合同，超出 Phase 14 文档范围；如需切换必须单独提出 change。
- 因 local in-memory queue 通过而省略 Redis smoke：不能证明跨进程 receipt/claim/recovery，拒绝。

## 后果

- `make smoke-service` 是 Redis queue 的真实验收入口；`make smoke-local` 不能替代。
- 镜像 tag 可复核，但 tag 本身不等于不可变 digest；Phase 15 发布证据需要记录实际 digest 和 server version。
- 组织选择的 Redis 许可、分发方式和所需 NOTICE/源代码义务不由仓库脚本自动决定。
- 若未来切换 runtime，`RunQueue` 合同和 durable evidence 语义必须保持或由新契约显式修改。

## 证据

```bash
uv lock --check
make license-check
make smoke-service
```

代码与测试证据：`templates/service-app/docker-compose.yml`、`packages/agent-harness/src/agent_harness/adapters/queue/redis.py`、`tests/integration/test_redis_run_queue_contracts.py`、`tests/contracts/test_durable_run_queue_contracts.py`。许可证事实参考 Redis `7.2.14` 官方 [`COPYING`](https://raw.githubusercontent.com/redis/redis/7.2.14/COPYING) 和 [Redis 7.2.14 release](https://github.com/redis/redis/releases/tag/7.2.14)；7.4/8 的版本边界参考 [Redis 官方许可页](https://redis.io/legal/licenses/)。

## 复审触发条件

- `SERVICE_APP_REDIS_IMAGE` 默认值或实际部署镜像变化。
- redis-py 解析版本或 server/client 协议能力变化。
- Redis 从 RunQueue 扩展为 session cache、event store 或其他用途。
- 镜像被再分发、转售、嵌入托管产品，或组织准备生产发布。
- Redis 官方许可证、镜像来源或安全支持状态变化。
