## Context

20B 建立固定执行顺序，但当前持久化不足以证明跨进程恢复的 at-most-once：migration head 为 `0017_model_route_chain_state`；`tool_invocations` 只对 nullable `approval_id` 唯一，普通 `ToolRegistry.call()` 在 handler 前没有稳定 claim；`context_assemblies` 只有随机 id 和 trace 摘要，没有 loop/turn/input identity；checkpoint JSON 也不能原子协调 model usage、tool claim、context result、outbox 与 terminal。

20C 在不改写 20A/20B 公开行为的前提下增加耐久 identity/state 和恢复协调。它复用既有 usage/shared-budget、approval lease、run evidence outbox、CanonicalEvent 与 repository/UoW 先例，不引入通用 workflow/state-machine framework。

## Goals / Non-Goals

**Goals:**
- 让 loop、turn、model usage、tool call、context assembly、approval/checkpoint 和 event identity 可复算且不可由 caller/provider 改写。
- 对每个 crash/commit-ack 窗口给出 exact replay、可信继续或 needs-review 的唯一结果。
- 让普通与 approved 工具调用都具备 handler 前 durable at-most-once claim。
- 让全局上限、root shared budget、event capacity 与 terminal fencing 跨 worker/restart 保持不变。
- 在 SQLite 与真实 PostgreSQL 上证明迁移、并发、恢复和 downgrade 边界。

**Non-Goals:**
- 不自动处置 needs-review，不重放不确定副作用，不并行执行同一 loop。
- 不增加通用 Saga/State 框架、scheduler、tool streaming、并行工具或 Phase 21 重构。
- 不允许旧 binary 猜测新 loop state，也不把真实 provider/tool smoke变成默认门禁。

## Decisions

### D1. 增加 `0018_model_tool_loop_state`，而不是只扩 checkpoint JSON

Migration 新增 `model_tool_loops`，至少包含 tenant/run/agent、stable `loop_id`、request/operation/catalog identity digests、status、next turn、frozen bounds、cumulative usage、state JSON、result/error refs 和 timestamps，并以 `(tenant_id, loop_id)` 唯一。Status 只允许 `active|waiting_approval|completed|failed|cancelled|needs_review` 的持久化词汇。Migration 同时新增单行 `model_tool_loop_schema_marker`，exact key固定为`model-tool-loop-v1`，字段`evidence_seen`初始false；任何首条loop row或tool/context v1 identity写入都必须在同一UoW先把它单调置true，应用和维护入口均不得清回false或删除该行。

同时扩展：
- `tool_invocations`：nullable legacy-compatible `loop_id`、`turn_ordinal`、unique `tool_call_id`、`binding_json`、`execution_lease_digest`、正整数 `execution_fence`、`execution_lease_expires_at`、nullable `handler_started_at` 与 `not_started_proof_json`。新模型驱动调用的 execution state 只允许 `claimed|executing|completed|failed|needs_review`；首次先插入 `claimed`，不得直接插入 `executing`。
- `context_assemblies`：nullable `loop_id`、`turn_ordinal`、`tool_call_id`、`input_identity_digest`、`output_digest`，新 loop 行以 `(tenant_id, loop_id, turn_ordinal)` 唯一。

Model turn 不另建重复表，继续由既有 `run_evidence_outbox`/usage settlement 以稳定 `usage_call_id` 保存 started/final；loop state 只引用其 ID。这样避免第二套 usage 真相源，同时补齐 ordinary tool/context 的唯一性。

备选是把全部状态塞入 checkpoint JSON，但无法对普通工具、context result 做数据库唯一竞争，也不能在并发 worker 下原子 claim，拒绝。

### D2. 身份采用分层 canonical preimage

`loop_id` 从 `model-tool-loop-v1`、tenant/run/agent/request/trace 和原始 operation key 计算 64 位 SHA-256；request fingerprint 另用 typed fingerprint key，不保存 prompt/tool payload。`usage_call_id` 复用既有 stable function并绑定 `loop:{loop_id}:turn:{n}` 语义槽位。`tool_call_id` 从 loop id、turn ordinal、intent/catalog/arguments digests计算；Context Assembly identity再绑定 tool call/result ref/digest。

持久化 state、checkpoint、approval、usage、tool invocation、context row、event payload各自保存所需 digest，并从上一层 canonical inputs重算。只让多个字段彼此同步但不匹配受信 preimage的篡改仍失败。

### D3. Loop repository 是状态协调真相源，具体副作用仍归各自 owner

`ModelToolLoopRepository` 以 run row/UoW lock或同等 CAS检查 transition、next ordinal、当前 refs 和 terminal eligibility；它不复制 usage/budget/tool/context payload。Model provider是否执行由 usage claim真相源判断，tool handler由tool invocation claim判断，context由context row判断，event由outbox判断。Loop row只在交叉验证这些 owner state后推进。

### D4. 每个恢复点只有三类结果

1. **Exact durable result**：返回/补投既有结果，不重调副作用。
2. **可证明未开始**：无 claim 时可按同一 identity 创建；已有 claim 时只有 `claimed` 且原 lease 已过期，才能由 owner UoW 以 CAS 同时轮换 lease digest、递增 fence、保存 `tool-handler-not-started-v1` proof 后继续。
3. **未知或冲突**：进入 needs-review并保留预算、capacity、lease/claim；不自动 retry。

Model turn 复用 Phase18/19 usage claim/attempt proof。Tool claim 的 `tool-handler-not-started-v1` exact DTO 固定保存 `schema_version/tool_call_id/binding_digest/prior_fence/next_fence/previous_lease_expires_at/reason=claim_lease_expired/proof_digest`；proof digest 由这些 canonical 字段重算。只有 CAS winner 持有的新 lease/fence 可在 lease 有效期内把 `claimed` 提交为 `executing`，该提交确认后 Registry 才铸造进程内 `ToolExecutionPermit` 并立即调用 handler；旧 owner 的 fence 必须在 handler 边界前失败。`claimed→executing` 提交确认未知时本进程不得调用 handler；恢复若读到 `claimed` 可等 lease 过期后按上述 proof 接管，若读到 `executing` 则视为 handler 可能已取得执行权，只能复用 completed/failed exact result或进入 needs-review，绝不降回 claimed。Context row已完成则复用 output；只有不存在 row且前置 tool result exact时才能创建。Event publication只补投稳定 envelope。

### D5. 普通与 approved 工具统一使用 tool-call claim

新 `tool_call_id` unique claim 是模型驱动调用的主键；approved 路径同时保留既有 `approval_id` unique 与 active lease/fingerprint。两者必须指向同一 invocation row，不能各建一次。Claim 插入、tool event capacity reservation 与 `tool.call.started` intent/outbox准备在 handler 前由同一 owner UoW协调；handler结果落库后才可生成final evidence。

### D6. Bound、预算与 deadline 从 loop row恢复

Loop 创建时保存 absolute UTC/monotonic可恢复 deadline（持久化 wall-clock截止时间加单进程monotonic guard）、max turns、total tokens/cost、tool output bytes和catalog digest。每轮使用同一root ledger并从 durable cumulative/owner余额计算剩余上界。Restart/reload/approval不能重新读更宽配置；current hard policy只能进一步拒绝，不能扩权。

### D7. Terminal 由联合 prerequisite 检查发布

Loop terminal前必须确认：所有 model usage settled、tool invocation非executing/unknown、context assembly exact、approval ordered evidence完成、对应 event outbox published/cancelled且shared budget无未决claim。任一 needs-review/unknown保留 terminal reservation并阻止 run terminal。Terminal commit后任何新turn/tool/context写入均失败。

## Affected Surfaces

- Migration/model：新增 `storage/migrations/versions/0018_model_tool_loop_state.py`；增加`model_tool_loops`与单行`model_tool_loop_schema_marker`，更新 migration catalog/model exports、`resource_models.py`、`run_models.py`，SQLite/PostgreSQL同 schema。
- Repositories：新增 `storage/model_tool_loop_repositories.py`；扩展 tool invocation、context assembly、usage/shared-budget、ordered evidence与approval recovery repository。
- Runtime：`runtime/{model_tool_loop,continuation,_run_continuation,orchestrator}` 与 worker startup recovery；不增加全局 scheduler。
- Tool/Model/Approval/Events：消费20A/20B DTO和claims，扩展exact binding/terminal validator，不改变旧人工tool或text/structured行为。
- Tests：migration upgrade/downgrade、SQLite/真实PostgreSQL并发与每个crash窗口、API/worker resume、event/terminal、旧row兼容、20A/20B及Phase18/19回归。
- 文档：Product/API/DEV、storage/runtime/adapter guides、acceptance matrix与living plan。

所有写入由同一 worktree/owner串行完成。实现前必须从20B归档HEAD重校准完整 owner manifest；清单外文件先修订契约并重审。

## Testing Seams

- Public `BoundModelToolLoopService` + `RunOrchestrator`：初始、exact replay、conflict、waiting/resume、terminal。
- `ModelToolLoopRepository` SQLite/真实PostgreSQL：并发创建同loop、ordinal CAS、非法transition、terminal后写入。
- `ToolRegistry.call/call_approved` public seam：handler前/中/后、result persist前/后、commit-ack未知、两worker竞争，调用次数0/1可证明。
- Model usage/public bound seam：每turn stable ID、provider started/unknown/exact replay不重复。
- ContextAssembler repository：同turn exact replay、input/result/catalog漂移冲突、event publish未知补投。
- Migration：0017→0018空库/legacy rows、旧rows nullable；只有从未产生任何 v1 loop/tool/context evidence时允许downgrade，存在过 evidence即使后来删除或置空也必须 fail closed；SQLite/PostgreSQL逐值一致。
- Terminal：usage/tool/context/approval/outbox任一未决均阻止唯一 run terminal。

## Risks / Trade-offs

- [三处表扩展增加迁移面] → 只增加新loop所需nullable列/一张协调表；legacy paths不写新字段，contract锁定旧row兼容。
- [Tool handler本身不可事务化] → handler前claim；handler开始后结果不明一律needs-review，不以数据库事务假装外部副作用原子。
- [Wall-clock在restart间漂移] →持久化绝对deadline并在单进程内同时使用monotonic guard；时间回拨只允许更早拒绝或needs-review，不延长授权。
- [Loop row成为万能状态箱] → row只保存identity/refs/transition摘要，usage/tool/context/event事实仍由各owner repository维护并交叉验证。
- [Downgrade丢失新evidence] → 单行`model_tool_loop_schema_marker.evidence_seen`在首条v1 evidence写入前由同一UoW单调置true；只要为true就拒绝downgrade，应用/维护入口不得删除或清零marker，删除、置空、导出业务证据或停止新binary都不能清除此事实。只有marker仍为false、且再次扫描确认无v1 evidence的0018 schema允许回到0017。

## Migration Plan

1. 公共 red contracts证明当前普通tool/context缺少唯一claim、并发worker可重复副作用或重建不同assembly。
2. 新增0018 migration与repository contracts，先通过SQLite/真实PostgreSQL upgrade/downgrade/legacy兼容。
3. 接loop state、ordinary/approved统一claim、context idempotency，再接worker recovery和terminal validator。
4. 跑每个crash window、全量Phase20聚焦、quality/test/service smoke与fresh review。
5. 回滚仅在无任何v1 loop/tool/context evidence时降回0017；否则migration明确拒绝。旧binary看到0018或新state必须启动失败，不能忽略后执行。

## Open Questions

- 无阻断性问题。`0018_model_tool_loop_state`、loop表、tool/context nullable v1 identity以及 tool claim lease/fence/proof 字段均为当前源码缺失的强制交付；实现不得以等价推断为由撤回 migration。任何字段级优化都属于实质 design/owner变化，必须修订 artifacts 并重启契约审查。
