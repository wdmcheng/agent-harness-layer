## Source Links

- Product-Spec.md：`SCOPE-033`、`TASK-015`、`FLOW-008`、`REQ-027` 与 `AC-090`～`AC-095`。
- Product-Spec-CHANGELOG.md：`v1.22` 受控跨 deployment/provider fallback 规划与 `v1.23` 当前实现/取消终态事实。
- DEV-PLAN.md：Phase 18.2 `controlled-multi-provider-failover` 的范围、依赖、交付物和验证门禁。
- API-Contract.md：`MOD-003` 受控跨 deployment/provider fallback 先行契约，以及既有 `MOD-002`/`MOD-004` route、usage、streaming 语义。
- docs/plans/architecture-evolution-plan.md：7.4、Phase 18.2 Progress、D-049、12.7、13.4 与 Handoff Snapshot。
- docs/plans/architecture-evolution-change-matrix.md：Phase 18.2 单 change、单 worktree、单 owner 的串行边界。
- docs/adr/0002-vendor-adapter-isolation.zh-CN.md：供应商 SDK、credential 与 client 只存在于 adapter/composition 边界。
- Design-Brief.md 或设计稿：不适用；本变更不新增 UI。
- CONTEXT.md：不存在，沿用 Product Spec、API Contract 与既有公共 DTO 术语。

## Why

现有 runtime 只能在同一 deployment/provider 内按 model ID fallback，无法安全表达跨 endpoint、credential、catalog、价格与 Bulkhead 的真实候选链。Phase 18.2 必须在 Phase 18.1 已归档的 streaming/unknown 围栏上增加受控跨 provider 切换，避免重复生成、重复计费、凭据错发和恢复时重放已开始副作用。

## What Changes

- 将 Agent model policy 的 fallback扩展为有序 `(deployment_id, model_id)` route refs；chain mode既有单值字段只作为请求缩权前原始 Agent首 route兼容投影且不能授权后继。Router在任何预算或 provider副作用前逐候选校验并冻结完整 route chain、候选 ordinal与去敏 chain identity，请求只能按原顺序删减候选；即使删除原始首 route，Agent投影与完整授权仍不改写，缩权只由 candidates表达。
- 每个候选从自己的 typed deployment 派生 provider kind、endpoint/credential policy、model catalog、capability、deadline/retry、Bulkhead、token/cost 上界与 lazy client；候选之间不得复用 secret、client lease、permit 或价格身份。
- 静态不合格候选可以在零 provider副作用时跳过；soft review threshold选择有限 fallback或 current owner余额不足的候选必须耐久记为零 attempt/proof/reservation的 `budget_ineligible/soft_budget|balance`。当前 reservation在同一 owner lock/CAS UoW中跨过中间不可用候选直达首个 eligible后继；没有后继时原子 actual-zero结算、释放并 exhausted，恢复先重放首次 durable决定而不按新余额重选。每个实际 attempt只接受两类安全收敛事实：client/send前可证明请求未越过进程边界的 `client_not_started`，或请求已发送且收到端点绑定 classifier与显式 `cross_provider_failover_http_statuses`共同证明“业务未开始且无计费”的 `trusted_business_not_started`。每次下一同 route retry前先按全局 attempt不可覆盖地耐久追加 proof；只有 candidate proof list覆盖全部实际 attempts且无悬空项才可跨 provider。调用级 claim、candidate聚合高水位与逐 attempt事实分层；后续 attempt仍可凭自身 `client_not_started`收敛，但不得抹除早期 started/response或重放已耐久记录的 provider attempt。无受信证明的 write/read timeout、任意含糊 transport结果、response identity、usage、text或 delta都停止 chain并进入原错误或 unknown。
- 每个候选独立执行 Policy/HITL；前一候选的 allow/approval不授权后继。Route chain在首次可信入口、approval record前从原始语义 operation key冻结 usage call id与 operation identity；后继 require approval时原子释放旧 reservation、以同一 identity的零 impact和 request binding暂停。ApprovalService独立提交绑定同一身份的 approval lease后，shared-budget以 usage identity/request/grant digest CAS重检目标 current balance：足够则激活原 claim，不足则耐久 budget-ineligible并让更后候选重新执行独立 policy；grant不得跨候选复用。Resume从私有 checkpoint重算，禁止 approval id rekey或第二 claim；两个 UoW通过 fencing与幂等恢复衔接，不虚构跨 repository原子消费。
- 将逐 attempt started identity创建、proof/lifecycle关闭、逐候选 reservation release/transfer、下一 reservation建立、settlement、attempt/evidence与恢复放入可重放的原子边界。显式chain唯一顺序为`policy/audit → reservation → attempt started identity → permit → client/prepare → send/iterate`，legacy单route顺序不变；started mark后permit/client/send前、send后proof/settlement前或commit-ack未知都保留原reservation、进入needs-review且不退款、不记零、不重发或继续provider。
- 扩展普通文本 streaming，使首个 delta 前只有 `client_not_started|trusted_business_not_started` 才能切换；观察或提交任一 delta 后永久围栏，取消、deadline、reader 断线、恢复和重连都不能触发后续 provider。
- 为 route-chain 显式取消/deadline补充唯一可信actual终态：只有provider-neutral关闭结果证明`stopped`、usage `finality=complete`且启用维度完整，并且不存在durable delta intent或发布不确定性时，当前attempt才以`cancelled/invocation_cancelled`结算实际用量，清空active/waiting/selected与reservation，不发布completed且不fallback；close失败、not-started、partial/null/unknown或无明确request/result观察仍按unknown/needs-review处理。稳定取消或unknown分类形成后的本地cleanup异常不得覆盖该稳定错误，`prepared`本身不构成provider调用事实。
- 分离不可变 `model-route-chain-v1` identity 与可变 `model-route-chain-state-v1` state；state以`model-route-attempt-identity-v1`和全局连续`attempt_lifecycle[]`唯一判断下一attempt是否尚未开始。Legacy预算身份逐字保持`budget-operation-v1`，显式chain以`budget-operation-v2`和chain digest/count防止同key换链；字段级冻结候选、attempt、reservation transition、exhausted error 与 live smoke exact shape；新增默认零网络、双真实 deployment opt-in live smoke 和 CI/evidence producer。

## Non-Goals

- 不做动态 provider discovery、健康评分/权重、负载均衡、热重载控制面、自动成本套利、多区域运维或跨 provider 账单对账。
- 不做 structured output、structured streaming、tool-call delta、模型工具循环或 provider raw DTO 的跨候选 fallback。
- 不新增 HTTP endpoint，不允许业务请求声明 endpoint、credential、SDK adapter 或重排 Agent route chain。
- 不把 fake 作为真实 route chain 的隐式尾项，不读取或输出真实 secret；缺少两个隔离真实凭据或受信 deployment 时保持零调用 `hosted-unverified`。
- 不执行 OpenSpec sync/archive、push、发布或部署，不启动 Phase 19；本批次只在契约严格 `1+2` 通过后创建用户已授权的一次本地契约提交。

## Capabilities

### New Capabilities

- `controlled-multi-provider-failover`：定义有序跨 deployment/provider route chain、只缩权、逐候选隔离、安全切换、原子预算/恢复、streaming 首 delta 围栏与离线/live 证据。

### Modified Capabilities

- `agent-registry-model-context`：把 Agent model policy、request 与冻结 route 从单 deployment model fallback 扩展为有序 route refs 和完整候选链。
- `typed-config`：增加多 deployment route refs、逐候选隔离组合和双真实 deployment live smoke 前置/状态契约。
- `model-usage-evidence`：增加 route-chain identity、候选 ordinal、去敏 failover reason、逐候选 attempt 与 reservation transition 的封闭证据。
- `shared-parent-budget-ledger`：增加逐候选 reservation 原子 release/transfer/settlement、unknown fencing 与 crash replay 语义。
- `controlled-model-streaming`：增加首个 delta 前两类可信 not-started 切换，以及其他 started/response/timeout、unknown 与首 delta 后永久禁止跨 provider fallback 的围栏。

## Impact

- 代码：typed config、registry descriptor/loader、provider-neutral route DTO/Router、普通与 streaming invocation、settlement/recovery、Pydantic AI/fake adapters、runtime composition/shared budget、SQLite/PostgreSQL repositories 和 opt-in smoke。
- API/证据：不新增 HTTP route；扩展 `MOD-003` 的 provider-neutral route-chain、attempt、reservation transition 与 exhausted error 字段级契约。
- 数据：新增 nullable route-chain state 以复用现有 route snapshot、usage evidence、shared-budget claim/outbox 与 stream reservation；只要产生任一 non-null chain state，数据库降级就为保护 completed/cancelled/active/needs-review 证据而关闭失败。
- 测试/CI：新增 AC-090～AC-095 public route/invocation/composition/recovery 合同，覆盖两个不同 deployment/provider doubles、SQLite/PostgreSQL crash recovery、预算竞争、streaming 首 delta 围栏、fake/local 零网络及双凭据 opt-in smoke；live artifact还需以两条单attempt route、连续ordinal、selected/唯一completed、attempt/proof count、usage/cost与durable evidence逐值绑定后才允许PASS；同步 acceptance matrix、CI producer 和双语维护文档。
- 依赖：不新增 vendor SDK；继续遵守现有 Pydantic AI/OpenAI adapter 隔离和锁定版本。
