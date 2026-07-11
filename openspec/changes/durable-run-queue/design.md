## Context

现有配置已有 Redis DSN，但核心包没有 queue protocol，service smoke 只执行 PING。Phase 13 三个 change 的依赖与所有权见 `../phase-13-change-matrix.md`。官方 Redis 文档确认 Streams consumer group 的 pending entry、`XACK` 和 Redis 6.2+ 的 `XAUTOCLAIM` 适合处理 worker 崩溃后的至少一次重投；当前固定 Redis 7.2.4 支持该语义。redis-py 8.0.1 官方元数据仍支持 Redis 7.2 与 Python 3.12-3.14。

## Goals / Non-Goals

**Goals:**
- 建立不依赖 vendor 类型的 run message、delivery 和 queue protocol。
- 让 Redis adapter 具备原子 enqueue 去重、consumer-group pickup、ack 和 reclaim。
- 让 fake/Redis 共享同一公开合同，并保留真实 Redis 条件证据。

**Non-Goals:**
- 不执行 run、不装配 API/worker、不实现完整 dead-letter 管理。
- 不承诺 exactly-once broker；业务 exactly-once 由 run/DBOS 持久化幂等共同完成。

## Decisions

1. **使用 Redis Streams consumer groups，不用 list pop/push。** Streams 的 PEL 与 `XAUTOCLAIM` 能显式表达未确认交付和崩溃恢复；list 的 BRPOPLPUSH 需要额外维护处理中列表和 lease，审计面更差。
2. **message 只携带稳定关联 header 和 operation refs，不复制 run input/identity/grant payload。** worker 以 tenant/run/approval refs 从 PostgreSQL 读取权威状态，避免 Redis 中出现第二份敏感真相源。schema 只接受 Literal 1；未知版本不 ack，交给新版 worker reclaim。
3. **去重身份按逻辑 operation，而不是只按 run。** 初始执行固定 `run:<run_id>:execute`；每次 approval resolution用 `run:<run_id>:approval:<approval_id>:lease:<lease_id>`。Redis namespace使用 tenant/operation；这样同一 operation重试稳定，同一 run的后续 continuation不会与初始执行冲突。initial effective key保留客户端 key或回退到 operation id，approval effective key固定为 operation id。
4. **原子 Lua 完成 operation dedupe record 与 `XADD`。** dedupe key保存受保护字段 canonical hash、首次 payload与 stream id；同 operation相同受保护字段返回原 entry，不同受保护字段返回 conflict；不同 operation独立写 entry。备选 tenant/run单槽会阻断 approval continuation。
5. **receipt 用 owner + delivery count fencing。** pickup/reclaim 后通过 `XPENDING` 取得当前 consumer 与 delivery count；ack Lua 先核对两者再 `XACK`。仅用 stream id 的普通 `XACK` 会允许旧 worker 在 ownership 转移后误确认，不能暴露为公共 seam。
6. **至少一次 delivery，执行确定性收口后才 ack。** worker 中断留下 PEL entry，由 `XAUTOCLAIM` 恢复；adapter 不把 pickup 当成功。端到端不重复由 run transition、effective key 与 DBOS workflow id 共同控制。
7. **fake 保持协议级语义但不冒充 service 证据。** fake 使用可注入 monotonic clock 模拟 idle lease，真实 Redis contract 用独立 stream/group 前缀并在测试后清理。

## Affected Surfaces

- `agent_harness.runtime.queue`：versioned DTO、operation refs、fenced receipt、protocol、fake 和稳定错误。
- `agent_harness.adapters.queue.redis`：redis-py asyncio adapter、Lua enqueue、consumer group 和 reclaim。
- 核心 package exports、依赖与 lockfile。
- queue contract tests 和真实 Redis 条件测试；无 migration、HTTP 或 UI 变化。

## Testing Seams

- 公共 DTO JSON round-trip 与非法字段 validation。
- `RunQueue` contract suite：execute/approval operation identity、缺省/客户端 effective key、request-id retry、version/kind validation、enqueue/pickup/fenced ack、同 operation冲突、跨 operation独立与 lease reclaim。
- 真实 Redis 7.2.4：consumer group 单次分配、pending reclaim、ack 后不可见和资源清理。

## Risks / Trade-offs

- [Redis 与 PostgreSQL enqueue 不是同一事务] → API 先持久化 run，再幂等 enqueue；失败可安全重试同一 message，worker 永远以 PostgreSQL run 为权威。
- [消息长期 pending 或 poison] → Phase 13 只提供可观测 receipt/reclaim；执行错误由 worker 持久化为确定性 failed 后 ack，不确定错误不 ack。
- [Lua/canonical hash 跨版本漂移] → DTO 固定 `schema_version=1`、字段顺序无关 hash 和受保护字段集合；未知版本不 ack。
- [旧 worker stale ack] → receipt 绑定 consumer/delivery count，Lua 在 `XACK` 前核对当前 PEL owner/attempt。

## Migration Plan

新增依赖和模块，不改存量数据库。升级先部署兼容 producer/consumer 的 queue contract，再启用 split service；回滚时停止 producer/worker，清理本模板专用 stream/group/dedupe namespace 后可退回 inline service 行为。

## Open Questions

无；dead-letter retention、吞吐分片与多优先级队列属于 P1 运维能力。
