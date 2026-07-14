## 1. Usage DTO 与可用性合同

- [ ] 1.1 先增加 `ModelUsageEvidence` red contract tests，逐字段固定 API Contract 5.29 的 `usage_kind`、tenant/provider/model、nullable token、`cost_usd`/`cost_status`、latency、decision、run/agent、optional request、required trace 形状及 extra-field 拒绝；逐一拒绝 bool、负 token/cost/latency、NaN/Infinity、`reported|estimated` 配 null cost、`unavailable` 配非 null cost，并证明真实零合法；estimated 必须在 `decision` 内含安全 price source ref/version。
- [ ] 1.2 实现 provider-neutral evidence DTO 和公共 import seam，明确 reported/estimated/unavailable `cost_status`、nullable token 与真实零值的区分；DTO、repository、EventBus 与 aggregation 只接受同一非负有限不变量，禁止自由 dict 或第二套同义 DTO 进入公共 evidence。
- [ ] 1.3 增加 fake model、Pydantic AI adapter 替身和 OpenAI-compatible embedding 替身映射测试与实现，验证三者输出同一 shape 且不暴露 provider SDK object/raw response；与 `embedding-cache-tenant-isolation` 联合覆盖 hit 仍有 started/final evidence、本次 lookup latency、null token/cost + unavailable、`cache_status=hit/provider_called=false`、首次 provider latency 不复用且 provider side effect 为零。

## 2. 路由、预算与调用生命周期

- [ ] 2.1 让 ModelRouter/embedding composition 在 provider 副作用前生成稳定 `usage_call_id`，绑定 `run-trace-correlation` 提供的 canonical trace并发布 `model.request.started`；完成、受控拒绝和 provider 失败恰好产生一条 `model.usage.updated`。Embedding 复用这两个精确 event type并以 `usage_kind=embedding` 区分，禁止新增等价事件名。
- [ ] 2.2 把实际 provider/model、route/fallback、budget/policy decision 与已知 token/cost/latency 归一化进 evidence；分别覆盖 fallback 只调用实际备用 provider，以及 hard budget/policy intervention/rejection 零 provider side effect，另测 timeout 和 provider exception，所有调用级最终 usage 均保持 `terminal=false`。
- [ ] 2.3 增加 import/static boundary tests，证明业务 agent、template agent 和 API route 不解析 raw usage、不导入 provider client，也不创建或修改 `ModelUsageEvidence`。

## 3. Local-First Event 与 Telemetry

- 技术前置：`0014` 的 `down_revision` 固定为 `0013a_run_trace_event_hardening`，不得直接依赖存在历史 shape 漂移的 `0013_run_trace_correlation` stamp。
- [ ] 3.1 在 trace revision `0013` 后增加 `0014` `run_evidence_outbox`/usage settlement 与 per-run event capacity reservation migration、typed model、repository 和 UoW contracts；run 创建时预留 terminal，受信版本化 typed registry 从封闭 operation kind 派生最大 prerequisite event 数，业务输入不能自报，provider/tool/approval/delegation operation 在副作用前原子预约，保证 `highest_persisted_seq + outstanding + terminal <= 2147483647`，且预约消费、event 插入与 high-water mark 推进同事务。Migration 在 writers 停止后为旧 non-terminal run/已知活跃状态回填预约，未知/矛盾状态在 mutation 前拒绝。覆盖 SQLite/真实 PostgreSQL 的 registry 防绕过、`{1, 2147483646}` 稀疏高 seq、并发容量竞争、容量不足零副作用、实际结算/确定释放、未知结果保持预约并阻止 terminal、写前失败、确认丢失与重启补投，证明 provider 不重放、`terminal=false`、`usage.seq < terminal.seq`、三种 run terminal 均显式 `visibility=public`，且 EventBus/local/PostgreSQL sink 在 seq 消耗前拒绝 non-public terminal；有 evidence 时 downgrade 拒绝删除并保留兼容读取。
- [ ] 3.1a 增加公共 `canonical_event_bytes()` 与 CanonicalEvent `65536` bytes envelope red contracts：统一 UTF-8、`ensure_ascii=false`、排序键、紧凑分隔符和 NaN 拒绝，payload 超限先 artifact 化并重算，仍超限以 `event.envelope_too_large` 在持久化/fan-out/seq 消耗前拒绝；legacy/direct-write 超限 row 以 `event.envelope_state_invalid` fail closed，不截断、不返回空页忙循环。覆盖等于/超过上限、中文/转义字符、键插入顺序和 local/DB/SSE 复用同一 byte count。
- [ ] 3.2 按 `MODIFIED Requirement` 保留 TelemetryFacade 对 ordinary record 的 local/jsonl 职责，同时让 EventBus 成为 canonical usage 的唯一 local durable 写入者，成功后把同一个已持久化 event 交给只做 provider fan-out 的 Facade；增加 direct publish 拒绝、未配置 provider、provider 成功/失败和 local sink 失败测试，证明每个调用恰好一条调用级最终 local usage，Facade 不二次写 local sink 或创建 CanonicalEvent，degraded status 只进入有界 result 或独立非 usage 幂等 evidence。
- [ ] 3.3 使用 prompt、embedding、vector、headers、secret、raw exception/response fixtures 扫描 DTO、event、trace、error、local/provider payload，补齐双出口 redaction 与封闭失败状态。
- [ ] 3.4 把 approval API/CLI、runtime continuation、service app API/worker 与示例 agent 的确定性 completed/failed/deny 结果接入同一有序证据 outbox：API 只原子提交仲裁与 outbox/approve enqueue，`approval.resolved` 先于 public run terminal，二者耐久持久化后才公开 resolution；覆盖 sink 写前失败、写后确认丢失、进程重启、稳定 event id、重复 resolve、non-public terminal 零持久化和 tool handler 不重放。
- [ ] 3.5 修改 `auth-policy-hitl-approvals`、`runtime-checkpoint-runs`、`service-app-shell`、`service-deployment-boundaries` 与 `p0-example-agents` 长期 capability delta，并让真实 PostgreSQL/Redis service smoke 证明 deny 零 continuation、公开状态可见时点、`approval.resolved` 先于 terminal、worker 消息确认前置条件和故障恢复。

## 4. Runtime 组合与性能门禁

- [ ] 4.1 在 service-app runtime/model/embedding composition 注入已认证 tenant/run/request/agent 与 `run-trace-correlation` 提供的 canonical trace context，验证 local JSONL 与 PostgreSQL event sink 都能按关联字段读取 evidence。
- [ ] 4.2 扩展 `scripts/smoke_local.py`，用固定 fake provider 从公开 single-agent run 入口计时到唯一 terminal，断言总时延小于 5 秒并输出不含 secret 的阶段时延与关联标识。
- [ ] 4.3 增加超限负向测试，证明 smoke 非零失败且不会跳过、放宽阈值或用单元测试内部墙钟替代入口验收。

## 5. 验证与收口

- [ ] 5.1 运行 model/embedding/event/observability 定向 contract/integration/eval tests、`make eval`、`make smoke-local` 和真实 PostgreSQL/Redis `make smoke-service`，分别记录 local-first 与 service persistence 证据。
- [ ] 5.2 运行 import/secret scans、OpenAPI drift、`make quality`、`make test`、`make build`、`make license-check`、pre-commit、`git diff --check` 和 `openspec validate model-usage-evidence --type change --strict`；migration contracts 必须覆盖空库无/非法/重复 opt-in 拒绝、精确 `-x allow_empty_evidence_downgrade=true` 允许，以及任一 outbox/settlement/capacity evidence 即使 opt-in 也拒绝。保持 delegation/SSE 与 Phase 14/15 在范围外。
