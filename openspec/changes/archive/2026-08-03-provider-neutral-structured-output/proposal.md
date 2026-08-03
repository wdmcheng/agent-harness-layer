## Source Links

- `Product-Spec.md`: `SCOPE-034`、`TASK-016`、`REQ-028`、`AC-096` 至 `AC-103`
- `Product-Spec-CHANGELOG.md`: `v1.24 Provider-neutral Structured Output 契约`
- `DEV-PLAN.md`: `Phase 19: Provider-neutral Structured Output`
- `API-Contract.md`: `MOD-005 Provider-neutral Structured Output`
- `docs/plans/architecture-evolution-plan.md`: `7.5 Phase 19`、`Progress / Surprises & Discoveries / Decision Log / Handoff Snapshot`
- `docs/plans/architecture-evolution-change-matrix.md`: Phase 19 owner 行与 `5.3 Phase 18.2 → 19`
- `docs/adr/0002-vendor-adapter-isolation.zh-CN.md`: 厂商 SDK 只留在 adapter 边界
- `Design-Brief.md` / 设计稿：不适用；本 change 不涉及 UI 或交互视觉

## Why

当前生产 runtime 只把 provider 结果转成 `ModelResponse.output_text`，Agent Registry 也只验证后丢弃未版本化的 Python schema reference。业务无法在不耦合 Pydantic AI/provider 类型的前提下取得可版本化、可预算、可耐久重放且 fail closed 的结构化结果。

Phase 18.2 已冻结受控 route、provider、副作用、shared-budget 与 recovery seam；现在必须沿这条 seam 增加结构化输出，并明确 repair、unknown 和 replay identity，避免示例或 SDK 默认行为成为第二套控制面。

## What Changes

- Registry 将 Agent `output_schema` 编译为严格 canonical JSON Schema，并生成稳定 `schema_ref/version/digest`；descriptor 只公开 provider-neutral identity。
- 增加 `BoundModelInvocationService.complete_structured(...)`、provider-neutral structured request/final result，以及adapter到核心validator唯一的`StructuredProviderCandidate` exact DTO与冻结protocol签名；核心显式拥有repair×transport双层循环，每对ordinal使用fresh prepared call，send只执行一次外部request且禁止隐藏retry；核心 JSON Schema validator 与可复算 replay identity 同时保留 `ModelResponse.output_text`。
- typed deployment 增加显式 `structured_output` capability 与 `0..2` repair 上限；结构化调用只允许未声明`fallback_routes`的legacy非流式单route，repair只在同一冻结deployment/provider/model内推进。
- 在任何 provider 副作用前按 `transport_attempt_limit * (1 + repair_limit)` 冻结 token/cost reservation；price values与price source identity分别成对校验，cost关闭仍可保留完整catalog/source identity；所有实际 provider request 都进入连续 attempt、usage、shared budget 和 durable evidence。
- 在 durable claim 之后区分可证明的 `failed` 与不可证明的 `needs_review`：只有核心vendor-neutral retryable prepare错误可在send前生成proof并推进transport ordinal；一旦到达send或收到HTTP response就计为provider request并停止structured retry，classifier只服务既有text路径。Send前/后取消、deadline和prepared close失败按冻结优先级收口；not-started proof只证明provider request未发生，不证明durable mark事务未提交。副作用、mark commit ack、usage、cleanup或repair/request基数不确定时以逐维度显式null保留事实与预算围栏，不伪造0或正数。
- 以判别式 provider-neutral structured attempt 冻结 global/repair/transport ordinal、schema/prompt identity、repair trigger/validation codes 与可复算 not-started proof；普通文本 attempt 的既有 exact 序列化不增加字段。
- 为 valid、invalid、extra fields、unknown/conflicting schema、repair-policy invalid、capability/protocol unsupported、budget rejection、repair exhausted、replay conflict 与 provider result unknown/needs-review 定义唯一、去敏、可恢复的终态；structured协调器不得泄漏底层通用capability错误。
- 以至少两个不同 provider identity 的 doubles/fakes 从公开 bound seam 建立 red→green contract/integration/eval 证据，并回归 text-only、streaming 与 Phase 18.2 route-chain。

本 change 不包含 breaking change：既有文本、非流式、流式、route-chain 与 `ModelResponse.output_text` 行为保持兼容；新增字段为可判别的 nullable provider-neutral surface。

## Non-Goals

- structured streaming 或增量 JSON/schema 拼装。
- 跨 provider structured fallback；Agent 只要显式声明任意非空`fallback_routes`就保持Phase18.2 route-chain identity，即使请求缩权后只剩一个candidate也在provider副作用前拒绝，不降级为legacy单route。
- tool call、工具循环、工具执行，或把 provider tool output 当业务结构化结果。
- fake 作为真实调用的隐式后备，或真实 provider 请求/live PASS。
- Phase 21 service locator、storage port、状态机或依赖环重构。
- 由 ticket triage 等示例 Pydantic model 反向定义核心 DTO；示例只能在公共 seam 稳定后迁移。
- 迁移ticket triage。其注册schema是含调用后trace引用的最终Agent输出，不是provider本次生成schema；若未来需要模型生成分类schema，必须另行冻结受信schema授权与最终输出组装契约。
- OpenSpec 主规格 sync、archive、commit、push、release 或 deploy。

## Capabilities

### New Capabilities

- `provider-neutral-structured-output`: 稳定 schema identity、bound structured invocation、统一结果 DTO、核心验证、有限 repair、失败/replay/recovery 与兼容边界。

### Modified Capabilities

- `agent-registry-model-context`: Registry 持久持有 schema catalog，并让业务执行器通过可信 bound seam 取得当前 Agent 的结构化 schema。
- `typed-config`: deployment 显式声明结构化能力与 repair 上限，配置和快照保持可恢复边界。
- `model-usage-evidence`: 结构化每次实际 provider request 的 attempt、usage/cost、validation、replay 与 unknown evidence 进入既有耐久生命周期。
- `shared-parent-budget-ledger`: reservation 覆盖 transport retry 与有限 repair 联合最坏情况，exact replay/unknown 不释放未决预算。
- `controlled-multi-provider-failover`: 明确 Phase 19 不授权跨 provider structured fallback，也不改写既有文本 route-chain 顺序。

## Impact

- 公共核心：`ModelRequest` / `ModelResponse`、新的 structured request/final result/candidate DTO与validator、冻结structured provider/prepared协议、`BoundModelInvocationService`、route identity、semantic replay identity。`models/_invocation_structured.py`只保留structured入口、hard preflight、Policy/HITL与组件编排；`_invocation_structured_support.py`承载纯planning/attempt/recovery辅助，`_invocation_structured_execution.py`独占transport×repair执行循环，`_invocation_structured_result.py`独占provider-neutral结果、evidence与最终结算投影，`structured_schema.py`承载compiler与validator；扩展既有`models/_invocation_approval_identity.py`从durable approval continuation恢复原structured usage/operation/schema/repair identity。`storage/_structured_usage_evidence_repository.py`只从既有UoW session绑定started replay seed。三个共享兼容owner只做窄适配：`_invocation_chain_base.py`同步可选structured replay结算签名，`_invocation_streaming.py`同步共享finalization签名并直接复用既有event helper，`_router_snapshot_chain.py`把冻结的`max_structured_repair_attempts`带入既有chain snapshot；不得借此实现structured chain或structured streaming。该内部拆分保持公共出口、schema、事务与生命周期顺序，不进入Phase21通用重构，也不把纵向控制器塞入已承载complete/stream/recovery的公共invocation façade。
- Registry / config：Agent schema loader/catalog/descriptor，deployment capability/repair 配置与 shared-budget snapshot。
- Provider adapter：fake/doubles 与 `adapters/models/pydantic_ai.py`、`adapters/models/_pydantic_ai_client.py`、`adapters/models/_pydantic_ai_structured.py` 只按冻结protocol返回`StructuredProviderCandidate`；私有structured helper独占SDK事件归一化与单次prepared call，映射JSON字符串/object与唯一provider-neutral attempt。Candidate不重复携带顶层计量，prepare/call异常使用核心公开的vendor-neutral类型，不引入 provider-native 公共类型、裸tuple/object或`ModelResponse`候选旁路。
- Usage / durability：`models/usage.py`持有公开`ModelUsageEvidence.decision`校验与构造；既有 evidence outbox、shared-budget result，以及 `models/_settlement_contracts.py`、`models/_settlement_evidence_models.py`、`models/_structured_settlement_evidence_models.py`、`models/_settlement_validation.py`、`models/_settlement_evidence_validation.py`、publication/recovery validator 承载稳定错误、route/reservation形状；其中structured evidence models独占structured started/final投影，并由公共validator交叉校验结构化摘要、response、attempt 与 charge。预期不新增表或 migration，若 red contract 证明相反，必须先修订 design/tasks 并重审。
- 示例兼容：公共structured seam稳定后把`templates/service-app/agents/examples/dev_assistant/{schemas,agent}.py`的宽松工具结果改为严格DTO，把`templates/service-app/agents/examples/rag_assistant/{schemas,agent}.py`的宽松组裁字典改为`no_source + {}` / `completed + 六字段`两个互斥exact object的封闭联合，并以既有Registry、审批工具流、RAG流、示例flow/eval与service app surface合同证明兼容；该迁移不定义核心DTO，不伪造零计数或assembly记录，不增加或执行新工具，也不改写Context Assembly耐久schema。
- Descriptor兼容：`output_schema_identity`是公开descriptor必填字段；所有直接构造`AgentDescriptor`的生产脚本/测试夹具与所有exact API/OpenAPI断言均是冻结owner，必须显式提供与测试schema匹配的identity，禁止用default/null/伪digest绕过。
- 验收：新增 AC-096 至 AC-103 producer/tests/eval 映射，并同步 `docs/acceptance-matrix.md`、API Contract 和 living plan。
- Policy/HITL：legacy structured单route继续复用既有`model.invoke`软门禁；DENY零调用，REQUIRE_APPROVAL只返回durable waiting。新增`complete_structured_approved`只接受与原usage/operation/request/schema/repair/tenant/run/agent绑定且处于有效lease的grant，恢复原usage/operation identity并重算hard route、余额和总reservation；arguments preimage、continuation、approval metadata、checkpoint与grant hash使用版本化exact对象和唯一canonical bytes逐值交叉校验，不授权无grant旁路、第二次策略解释或任意单项/组合换包。
- Policy/HITL回归：新增`tests/contracts/test_provider_neutral_structured_policy_contracts.py`，只从公开bound seam证明DENY零claim/零send、REQUIRE_APPROVAL durable waiting、active grant恢复、hard bounds重算，以及usage/operation/request/schema/repair/grant/lease、record/checkpoint、extra/missing/type drift的单项与组合篡改均在provider前关闭失败；单独覆盖只同步修改record/checkpoint两份continuation identity且其他字段不变的负路径。
- 依赖：优先复用现有 Pydantic/JSON Schema 能力；任何新增直接 runtime 依赖必须先补 license/lock/依赖契约，不在实现中偷偷引入。
