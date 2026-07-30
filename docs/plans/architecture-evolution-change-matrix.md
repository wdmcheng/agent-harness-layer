# 架构演进 Change Matrix

> 首次冻结：2026-07-27
>
> 上位计划：[`architecture-evolution-plan.md`](architecture-evolution-plan.md)
>
> 状态依据：当前 Git、OpenSpec 和磁盘文件；聊天摘要与历史关系图不能单独更新状态

本矩阵同时记录 change 的依赖、共享接口、共享验收、预计文件所有权、并行级别和 worktree 建议。矩阵中的未来名称是计划 identity，不表示目录已经创建；只有 `openspec list --json` 能证明 active change 状态。

## 1. 状态定义

| 状态 | 含义 |
|---|---|
| `文档中` | 仅治理/计划文档正在落盘；不代表行为实现 |
| `计划` | 只有长期计划，尚未获授权创建 OpenSpec change |
| `契约中` | 已创建 change，proposal/spec/design/tasks 尚未全部 strict + review PASS |
| `可实现` | strict validation 与要求的 fresh 契约审查已 PASS，依赖和 owner 已冻结 |
| `实现中` | 已有实现 diff，但尚未完成全部验证和 review |
| `验收中` | 实现冻结，正在完成 fresh review、证据和生命周期收口 |
| `已归档` | 用户已授权 sync/archive，active list、归档目录和侧车引用均已核对 |
| `外部阻塞` | 仅凭据、网络、配额或外部服务阻塞；不得写成 PASS |

状态迁移必须有证据。`openspec validate`、测试或文档勾选都不能单独把 change 推进到“可实现”或“已归档”。

## 2. 默认依赖 DAG

```text
Phase 17 governance baseline (non-OpenSpec documentation batch)
  -> acceptance-criteria-identity-uniqueness (governance correction)
    -> controlled-real-model-runtime
      -> controlled-model-streaming
        -> controlled-multi-provider-failover
          -> provider-neutral-structured-output
            -> provider-neutral-tool-call-contract
              -> policy-gated-tool-loop
                -> durable-tool-loop-resume
                  -> Phase 21 implementation changes

Phase 17 governance baseline
  -> architecture-hotspot-rebaseline (read-only inventory)
    -> Phase 21 implementation changes
```

Phase 21 的只读 inventory 可以提早进行；写入型 change 默认等 Phase 20 稳定。任何例外都必须先更新本矩阵，给出无顺序依赖、无共享接口、无共享验收、无文件所有权冲突的证据。

## 3. Change 关系与所有权

| 阶段 | Change / 批次 | 目标 | 直接依赖 | 共享接口 | 共享验收 | 预计文件所有权 | 并行级别 | Worktree 建议 | Codex 时间估计 | 状态 |
|---|---|---|---|---|---|---|---|---|---|---|
| 17 | `architecture-governance-baseline`（非 OpenSpec 文档批次） | 冻结工程原则、贡献规则、living plan、handoff 与矩阵；同一 v1.20 修订补充 Phase 18.1 规划 | 无 | 工程原则、贡献流程、OpenSpec/Agent Pack 门禁语言 | 文档互链、事实优先级、状态语义、handoff 可恢复 | `docs/plans/architecture-evolution-*.md`；`docs/engineering-principles*.md`；`CONTRIBUTING*.md` 按本轮独立 owner 分配；Product Spec/DEV/API Contract 由主控整合 | 可做认知并行；写入必须按文件独占 | 当前工作树即可；无需为纯文档另开实现 worktree | 4-8 小时；Phase 18.1 规划补充另计本轮执行 | `已完成`；治理基线、Phase 18.1 规划与审查修订已落入 `4922784d`，不代表 checker 或模型能力已实现 |
| 17.1 | `acceptance-criteria-identity-uniqueness` | 消除重复 `AC-070` 对 Spec、矩阵和 policy/checker 语义的覆盖，建立机械唯一性门禁和历史追溯 | Phase 17 文档冻结；用户已授权 | AC identity grammar、Product Spec、acceptance matrix、required producer/test policy | 每个 live AC identity 唯一；dependency lock 保留 `AC-070`，API docs 迁移为 `AC-089`，两者各自保留正确行为、producer、test 和 evidence；历史引用可追溯 | `Product-Spec.md`、`Product-Spec-CHANGELOG.md`、`DEV-PLAN.md`、`docs/acceptance-matrix.md`、`scripts/acceptance_matrix{,_policy}.py`、`tests/contracts/test_dependency_version_policy_contracts.py`、`tests/contracts/test_acceptance_identity_uniqueness_contracts.py` 与相关 contract tests；本计划/矩阵由主控同步 | 串行；不与会修改 Product Spec/验收矩阵的 change 并行 | 单一 worktree；禁止局部顺手重编号 | 2-5 小时 | `已归档`；TDD、冻结 evidence、direct validator、fresh review 与主规格同步已闭合，归档路径为 `openspec/changes/archive/2026-07-28-acceptance-criteria-identity-uniqueness/` |
| 18 | `controlled-real-model-runtime` | 受控接入一个真实 text completion deployment，同时保留显式 fake 路径 | Phase 17 文档冻结并审查；Phase 17.1 已验证、提交并归档；用户已授权 | typed config、agent descriptor、`ModelRequest`/`ModelRoutePlan`/`ModelResponse`、model catalog、Policy/HITL continuation、budget/evidence、composition | deployment∩agent allowlist；请求只缩权；secret/endpoint/model-catalog/timeout/retry/bulkhead/price/capability；审批完整绑定与单次消费；离线兼容与 opt-in smoke | `config/{__init__,schemas,settings,secret_files,model_endpoints,model_catalog}.py`；`models/{__init__,providers,router,_router_contracts,_router_current,_router_snapshot,invocation,_invocation_execution,_invocation_evidence,_invocation_settlement,_settlement_contracts,_settlement_validation,_settlement_evidence_validation,_settlement_publication,usage,usage_events}.py`；`adapters/models/{fake,pydantic_ai,_pydantic_ai_client}.py`；`policy/engine.py`；`audit/service.py`；`approvals/{service,_continuation}.py`；`runtime/{services,shared_budget,_shared_budget_common,_shared_budget_snapshot,_shared_budget_identity,_shared_budget_recovery,executor,continuation,_run_continuation}.py`；`storage/{_shared_budget_repository_records,_delegation_claim_repository}.py`；`registry/**`；`contracts/boundaries.py`；`packages/agent-harness/src/agent_harness/{cli.py,cli_access.py,scaffold_templates.py,_scaffold_support.py}`；`packages/agent-harness/pyproject.toml`；`uv.lock`；`compliance/third-party.toml`；`templates/service-app/{app/runtime.py,scripts/service_admin_budget_race.py,scripts/service_admin_budget_topology.py}`；profile/agent config；`scripts/{smoke_live_model.py,import_boundary_check.py,ci_evidence.py,acceptance_matrix_policy.py}`；Phase 18 config/router/fallback/runtime/budget/offline/policy tests、`model_usage_recovery_test_support.py` 及 durable-failure/approval-outbox/publication/ack-loss/unknown-terminal 分组恢复合同；delegation claim 与 service producer contracts；live smoke integration；`Makefile`；双 CI；验收矩阵；API Contract 与双语维护文档 | **串行**。config/model/router/fake+real adapter/vendor boundary/dependency+license/policy/approval/settlement/shared-budget/storage validator/scaffold/composition/CLI/template/CI/tests/docs 全部共享；现有 API/worker/eval caller 继续复用 `RuntimeComponents.close()`，若红灯要求修改其入口或其他 repository，先扩充契约并重新 review | 单一 worktree、单一 owner；禁止拆成并行 provider/config/dependency/scaffold/policy/approval/settlement/storage/CLI/CI 子 change | 20-32 小时；真实 smoke 另 1-3 小时墙钟 | `已归档`；32/32 tasks，三组 delta specs 已同步主规格，归档路径为 `openspec/changes/archive/2026-07-29-controlled-real-model-runtime/`，并由本地提交 `ff0c49b` 交付。fresh Reviewer 1/2/3 在同一冻结实现身份上分别 Stage 1/2 PASS，均为 0 findings。AC-081/083 真实托管 completion 保持 hosted-unverified；未 push、发布、部署或再次调用真实 provider |
| 18.1 | `controlled-model-streaming` | 在 Phase 18 route/provider lifecycle 上生产有界、可持久化、可恢复的 provider-neutral 普通文本增量，复用既有 RUN-006/CLI reader | Phase 17.1 与 Phase 18 已验证、fresh review、同步并归档；用户已通过 `/goal` 授权 | `build_execution_context()`、`BoundModelInvocationService.stream/stream_approved`、provider stream protocol、route/attempt、CanonicalEvent、event capacity/outbox、usage/budget settlement、RUN-006/CLI committed reader | bounded delta/coalescing；稳定 chunk identity；可信普通/审批 call identity；跨 chunk 安全；completed/usage/terminal 顺序；reader 断线不取消 run；取消/unknown 不重试不记零；Last-Event-ID 不重放 provider；offline/live 时延证据 | 生产：`packages/agent-harness/src/agent_harness/models/providers.py`、`router.py`、`invocation.py`、新建 `_invocation_streaming.py`/`_streaming_contracts.py`/`_streaming_consumption.py`/`_streaming_events.py`/`_streaming_settlement.py`/`streaming.py`、`_invocation_evidence.py`、`_invocation_settlement.py`、`_settlement_publication.py`、`usage_events.py`、`models/__init__.py`、`runtime/executor.py`/`services.py`/`_orchestrator_base.py`、`config/schemas.py`/`settings.py`、`adapters/models/pydantic_ai.py`/`_pydantic_ai_client.py`/`fake.py`、`events/capacity.py`/`bus.py`/`types.py`/`local_capacity.py`、`events/sinks/postgresql.py`、`storage/event_capacity_repositories.py`/`evidence_repositories.py`/`usage_evidence_repositories.py`/`_shared_budget_replay_repository.py` 与新建 `stream_evidence_repositories.py`、`templates/service-app/app/runtime.py`。测试：`tests/contracts/controlled_real_model_policy_approval_test_support.py`、`test_controlled_model_streaming_approval_contracts.py`、`test_controlled_model_streaming_provider_contracts.py`、`test_controlled_model_streaming_runtime_success_contracts.py`、`test_controlled_model_streaming_runtime_interruption_contracts.py`、`test_controlled_model_streaming_runtime_replay_contracts.py`、`test_controlled_model_streaming_runtime_guardrail_contracts.py`、`test_controlled_model_streaming_security_contracts.py`、`test_controlled_model_streaming_capacity_contracts.py`、`test_controlled_model_streaming_recovery_contracts.py`、`test_controlled_model_streaming_postgresql_contracts.py`、`test_controlled_model_streaming_transport_contracts.py`、`test_controlled_model_streaming_live_smoke_contracts.py`、既有 SSE/CLI 五文件及 `tests/integration/test_controlled_model_streaming_live_smoke.py`。集成：新建 `scripts/live_model_stream_contract.py`、`scripts/live_model_stream_probe.py`、`scripts/live_model_stream_execution.py` 与薄 CLI `scripts/smoke_live_model_stream.py`，修改 `Makefile`、`.github/workflows/ci.yml`、`.gitlab-ci.yml`、`scripts/ci_evidence.py`、`docs/acceptance-matrix.md`、API/OpenSpec/计划文档；无 migration，清单外 owner 先修约并重审 | **串行**。provider、可信 façade、event capacity、settlement、recovery、tests/CI/docs 是同一安全不变量 | 单一 worktree、单一 owner；只读威胁建模可并行，禁止把 stream adapter 与 capacity/outbox 拆开写 | 20-30 小时；若需 migration 暂按 24-36 小时；live smoke 另 1-3 小时墙钟 | `第三十名 Reviewer 1 以 fragment 左侧 Unicode 单词边界与 blanket suppression/Any 两项的 2 MEDIUM 判定 Stage 1/2 FAIL；10 个边界样例与两个类型节点形成 9 FAIL 后已 RED→GREEN，安全文件 39/39、1,409 条文本/31,716 次差分 0 mismatch、Phase 18.1 聚焦 168 passed / 6 skipped，全仓 quality 719 files/Pyright 0/import boundary、change/all strict 33/33、acceptance 41 passed / 1 skipped 与 diff check 均 PASS，当前重冻结并启动第三十一名 fresh Reviewer 1`；真实 provider 已获测试授权但按分层门禁等待静态 `1+2` PASS 后运行；公开 façade、DTO、事件顺序与事务原子性不变，最终重型门禁只执行一次 |
| 18.2 | `controlled-multi-provider-failover` | 以有序 `(deployment_id, model_id)` route chain 支持多个真实 deployment/provider 的安全 fallback | Phase 18.1 已验证、fresh review、同步并归档；用户另行授权 | typed route refs、Agent policy/request 缩权、immutable chain、provider factory/client lifecycle、shared budget settlement/recovery、usage/evidence、streaming 首 delta 围栏 | 候选 endpoint/credential/catalog/Bulkhead 隔离；只在 safe not-started 后切换；unknown/response/usage/text/delta 停止；逐候选 reservation release/transfer 原子；fake 不做隐式尾项；双 deployment offline/live 证据 | `config/{schemas,settings,model_catalog,model_endpoints}.py`；registry descriptor/loader；`models/{providers,router,_router_contracts,_router_current,_router_snapshot,invocation,_invocation_execution,_invocation_evidence,_invocation_settlement}.py`；`adapters/models/{pydantic_ai,_pydantic_ai_client,fake}.py`；`runtime/{services,shared_budget,_shared_budget_snapshot,_shared_budget_recovery}.py`；相关 SQLite/PostgreSQL repositories；`scripts/smoke_live_model.py`；CI、API Contract、验收矩阵、双语维护文档与 Phase 18.2 contracts/integration tests；migration 仅在 design 证明需要时纳入 | **串行**。Config、route chain、provider factory、预算 transfer、recovery、tests/docs 共同决定 safe failover 安全不变量 | 单一 worktree、单一 owner；只读厂商能力/威胁建模可并行，禁止把配置多 provider 与运行时切换拆开写 | 24-40 小时；双真实 deployment smoke 另 2-4 小时墙钟；dependency/migration 触发后重估 | `计划`；Product Spec/API Contract/DEV-PLAN/living plan/matrix 已补齐，OpenSpec 目录未创建 |
| 19 | `provider-neutral-structured-output` | 增加稳定 schema identity、结构化结果、验证与失败语义，不泄漏 SDK 类型 | Phase 18.2 已验收并归档 | Phase 18/18.1/18.2 route-chain/provider/result seam；`ModelRequest`/`ModelResponse`；agent input/output schema refs；usage/evidence | text/non-stream/route-chain 兼容；schema invalid/unsupported/repair exhausted 可追踪；修复尝试受预算和次数约束；structured streaming/failover 仍非目标 | `models/providers.py`、新的 structured DTO/service、Pydantic AI adapter、registry schema refs、contract/eval tests、相关 docs | 串行；与 Phase 18/18.1/18.2 不并行 | 从 Phase 18.2 归档 HEAD 新开 worktree；不得在旧基线开发 | 16-28 小时 | `计划` |
| 20A | `provider-neutral-tool-call-contract` | 模型只产出稳定 tool intent；完成参数 schema/来源校验但不执行工具 | Phase 19 已归档 | structured result、tool intent DTO、`ToolRegistry` metadata、CanonicalEvent | provider SDK tool-call 不外泄；未知工具/无效参数零工具副作用 | model/tool DTO、adapter normalization、`tools/registry.py` 只读/解析 seam、events/tests/docs | 与 20B/20C 串行；认知调查可并行 | 独立 worktree可以，但只能在 Phase 19 新 HEAD 上；同一时刻不启动 20B | 8-12 小时 | `计划` |
| 20B | `policy-gated-tool-loop` | 经 ToolRegistry、PolicyEngine、audit、HITL 执行受控工具循环 | 20A 已归档 | tool intent、registry allowlist、policy decision、approval/checkpoint、context assembly | approval 前零副作用；agent/request 不能扩权；工具结果按 untrusted 输入回注 | runtime executor/loop、tools、policy、approval、audit、context、events、tests/docs | **串行**；共享核心 runtime/tool/policy seam | 单一 worktree、单一 owner | 9-15 小时 | `计划` |
| 20C | `durable-tool-loop-resume` | 冻结 loop/turn/tool-call identity，完成 checkpoint/resume、exact replay、unknown 与 terminal fencing | 20B 已归档 | run state、checkpoint、usage ledger、tool evidence、CanonicalEvent、queue/recovery | replay 不重复模型/工具副作用；turn/token/cost/time 有上界；unknown 不自动重试 | runtime lifecycle/state/continuation、storage repositories/migration（如确需）、worker、events、tests/docs | **串行**；与任何 runtime/storage hotspot change 冲突 | 单一 worktree；完成后重新冻结 Phase 21 基线 | 11-18 小时 | `计划` |
| 21-0 | `architecture-hotspot-rebaseline`（只读 inventory） | 用最新 CodeGraph、源码和测试固定 service locator、storage 扩散、run state 与两个语义 SCC 的真实 blast radius | Phase 17；可读到当时最新 HEAD | symbol/call graph、测试覆盖、文件集合；不改公共接口 | inventory 可复现；不把旧图或生成图当行为真相 | 只更新本计划、矩阵或该阶段获批的分析附件；不改生产代码 | 可与 Phase 18、18.1、19、20 的认知工作并行，不能抢写共享文档 | 无需独立实现 worktree；若更新矩阵，由主控独占 | 2-4 小时 | `计划` |
| 21A | `typed-agent-execution-services` | 用 typed capability container 取代字符串键 service locator，限制 executor 可见依赖 | 20C 已归档；21-0 重基线 | `build_agent_execution_services`、executor service injection、CLI/API/worker composition | 行为等价；缺失/错误 capability 启动或解析时 fail closed；不可序列化进 DTO/checkpoint | `runtime/services.py`、`runtime/executor.py`、registry executor seam、CLI/template composition、contract tests | 默认串行；很可能与 21B/21C 共享 composition | 单一 worktree；在重基线后冻结 owner | 8-14 小时 | `计划` |
| 21B | `storage-port-seams` | 将核心用例对具体 `SQLAlchemyStorage` 的依赖缩到最小 ports/UoW seam | 21-0；默认在 21A 后 | storage repository/UoW、audit/event/budget/runtime service constructors | SQLite/PostgreSQL 逐值一致；事务、锁、replay 与恢复语义不变 | `storage/**` ports/exports、受影响 service constructors、composition、contract/integration tests | 默认串行；只有按 domain 拆分且无共享 UoW/exports 时有限并行 | 首次 change 单一 worktree；后续 domain cut 逐项证明独立 | 12-20 小时 | `计划` |
| 21C | `run-state-transition-kernel` | 把分散状态分支收敛为显式、可穷举、可审计的 transition seam | 20C；21-0；通常在 21A/21B 公共 seam 稳定后 | run status、checkpoint/resume、queue claim、approval、terminal event、recovery | 合法 transition 完整；非法/重复/并发 transition fail closed；既有 API/event 语义不漂移 | `runtime/state.py`、lifecycle/continuation/orchestrator、run repositories、worker、tests/docs | **串行**；与 runtime/storage changes 高冲突 | 单一 worktree、单一 owner | 10-18 小时 | `计划` |
| 21D | `cross-domain-cycle-cuts` | 对重基线确认的两个跨域语义 SCC 逐个建立单向 seam | 21-0；相关 typed/storage/state seam 已稳定 | 以重基线记录的实际 symbol/API 为准；不得按历史名称猜边界 | 每次 cut 行为等价；依赖方向可机械验证；无新反向 import/service lookup | 重基线后按每个 SCC 分配互斥文件集、checker 和 contract tests | **有限并行候选**；只有两个 SCC 无共享接口/验收/文件/顺序依赖时才拆为 A/B | 默认串行；满足四项独立证据后可用两个 worktree | 10-20 小时 | `计划` |
| 21E | `architecture-checker-expansion` | 把已稳定且可机械判断的层依赖、public/internal seam、vendor/ORM/config 规则逐条接入 checker/contract/CI | 21-0；相关边界 change 已稳定 | import boundary declarations、checker diagnostics、CI required jobs | 新规则有稳定正反例；既有合法路径不误报；无法稳定机械判断的规则仍留在 review checklist | `scripts/import_boundary_check.py` 或独立 checker、contract fixtures、CI/Make 入口、维护文档 | 可与无共享规则/fixture/CI owner 的行为 change 做认知并行；集成写入串行 | 默认由主控集成；每批只加入一个可解释规则族 | 10-18 小时 | `计划` |

### 3.1 Phase 18.1 CI owner 补充

本节与上表 Phase 18.1 行及 3.20～3.39 节共同构成精确所有权；上表 18.1 状态格只保留截至第三十轮的历史摘要，当前状态以本节、最新轮次和文末“当前唯一下一动作”为准。除表中已列路径外，单一写 owner 还精确拥有 `compliance/ci-jobs.toml`、`tests/contracts/test_ci_pipeline_contracts.py` 与 `tests/contracts/controlled_model_streaming_context_typecheck.py`。`ci-smoke-live-model-stream` 已接入 GitHub/GitLab，两个 `acceptance-validate` 均依赖并下载其 `model-stream-live-smoke/v1` 安全 artifact；pipeline contract 已逐值校验 manifest、两个 workflow、Make target、依赖和下载证据路径一致。实现候选 `361678bf…` 的静态 `1+2` 与最终重型门禁已由 3.38 闭合，最终证据审查、主 specs 同步与归档由 3.39 闭合。

### 3.2 Phase 18.1 路由、快照与结算 owner 补充

本节同样与上表 Phase 18.1 行共同构成精确所有权。单一写 owner 还精确拥有 `packages/agent-harness/src/agent_harness/models/_router_current.py`、`packages/agent-harness/src/agent_harness/models/_router_snapshot.py`、`packages/agent-harness/src/agent_harness/models/_settlement_evidence_validation.py` 与新增 `tests/contracts/test_controlled_model_streaming_routing_contracts.py`。当前配置规划、冻结快照恢复、结算/重放/恢复 route evidence 必须逐值接受精确 `text_stream`，保持 `text_completion` 行为不变，并继续拒绝未知 capability、快照与当前 route 不一致及证据篡改；副作用前拒绝与恢复测试都必须证明不会重新调用 provider。AC-085 的 producer 包含当前/快照 router，AC-086 的 producer 包含快照 router 与 settlement evidence validator；对应 routing contract 与 runtime/recovery/PostgreSQL contracts 共同形成测试 producer。

### 3.3 Phase 18.1 冻结前 owner 补充

实现与全量验证实际写入了实现前逐名清单之外、但仍属于同一结算/测试/状态安全面的路径：`packages/agent-harness/src/agent_harness/models/_settlement_validation.py`、`tests/contracts/model_streaming_sdk_event_test_helpers.py`、`tests/contracts/model_usage_recovery_test_support.py`、`tests/contracts/test_model_usage_approval_outbox_recovery_contracts.py`、`tests/contracts/test_model_usage_local_crash_recovery_contracts.py` 与 `Product-Spec.md`。其中生产 validator 只增加 `cancelled` 的封闭 outcome/error 组合；SDK helper 防止 vendor import 越界；三个 recovery 文件只补齐旧 `completed` 夹具缺失的合法 response；Product Spec 只同步 AC-085～AC-088 与当前 `hosted-unverified` 状态。上述路径已在最终冻结前补回 proposal/design/tasks，本次最终 Reviewer 1 必须按完整 diff 重新审查，不能复用实现前 owner verdict。

### 3.4 Phase 18.1 Reviewer 1 修复 owner 补充

首轮最终 Reviewer 1 的 4 HIGH / 2 MEDIUM 修复把 `packages/agent-harness/src/agent_harness/storage/_shared_budget_replay_repository.py`、`tests/contracts/controlled_real_model_policy_approval_test_support.py` 与新增 `tests/contracts/test_controlled_model_streaming_approval_contracts.py` 纳入单一 owner。前者只让 exact replay 逐值接受 usage/预算同一 `attempt_review` 的 needs-review 状态，绝不发布 final 或重启 provider；后两者经真实 `build_execution_context()`、orchestrator、approval lease 与容量感知 EventBus 验证普通/审批入口、九字段伪造、单次调用以及 partial/unknown 的 usage/父账本围栏。fake adapter 的脚本 seam 和 live script 的真实 RUN-006 ASGI SSE 探针仍属于原 owner。上述实质修订使旧冻结身份与 verdict 全部失效，完整回归后必须从新的 Reviewer 1 重启。

### 3.5 Phase 18.1 第二轮诊断修复 owner 补充

第二名 fresh Reviewer 1 因自身末检 manifest 聚合异常中止、没有正式 verdict，但其旧内容诊断证明 `packages/agent-harness/src/agent_harness/models/_settlement_publication.py` 是成功流式完成原子边界的必改 owner。该文件只把既有 final result/shared-budget 持久化抽为调用方可复用的同 UoW 私有 seam；`_invocation_streaming.py` 在同一事务写 completed intent、usage result、预算结算与尾部释放，提交后仍按 completed → usage 公开。`test_controlled_model_streaming_runtime_success_contracts.py`、`test_controlled_model_streaming_runtime_interruption_contracts.py`、`test_controlled_model_streaming_runtime_replay_contracts.py`、`test_controlled_model_streaming_runtime_guardrail_contracts.py` 同时覆盖双 token partial unknown charge 与 completed 首次发布失败后的零 provider replay。该修订不改变非流式公开 API、事件顺序、schema 或 Phase 18.2 范围。

### 3.6 Phase 18.1 第三轮 Reviewer 1 修复 owner 补充

第三名 fresh Reviewer 1 对逐文件 manifest 可复算身份以 1 HIGH / 1 MEDIUM 阻断 Stage 1：`_invocation_streaming.py` 只看 `finality=complete`，会让缺 input/output 或启用成本缺 cost 的 stopped usage 越过 review；`API-Contract.md` 两处仍写 producer 未实现。修复 owner 限定为 `packages/agent-harness/src/agent_harness/models/_invocation_streaming.py`、`packages/agent-harness/src/agent_harness/models/_streaming_contracts.py`、`packages/agent-harness/src/agent_harness/models/_streaming_consumption.py`、`packages/agent-harness/src/agent_harness/models/_streaming_events.py`、`packages/agent-harness/src/agent_harness/models/_streaming_settlement.py`、`packages/agent-harness/src/agent_harness/storage/usage_evidence_repositories.py`、`tests/contracts/test_controlled_model_streaming_approval_contracts.py` 与 `API-Contract.md`：统一可信度谓词，允许不完整 complete 进入既有封闭 attempt-review schema，并用真实 composition/shared-budget/exact replay public seam 覆盖三个负形状。该修订不改变 DTO、schema、事件顺序或 Phase 18.2 范围。

### 3.7 Phase 18.1 第四轮 Reviewer 1 契约修订

第四名 fresh Reviewer 1 在可逐值复算身份上以 1 HIGH 阻断 Stage 1：`specs/controlled-model-streaming/spec.md` 错误允许 stopped 且 usage 为 null/partial 时取消未使用 stream 占位，与同 change 的 usage spec、API、design/tasks、实现及 66 槽保留合同冲突，并遗漏 complete 但启用 token/cost 维度不完整。修订 owner 仅为该 delta spec 与状态计划文档：统一为不取消占位，同一 UoW 保留全部剩余 stream/usage 容量、reservation、outbox、预算和 lease 后 needs-review；不改生产代码、schema、事件顺序或 Phase 18.2 范围。

### 3.8 Phase 18.1 第五轮 Reviewer 1 质量修复 owner 补充

第五名 fresh Reviewer 1 对匹配冻结身份完成两阶段审查，以状态证据漂移、核心编排/测试超长和 blanket Pyright 抑制/`Any` 越界共 3 MEDIUM 判定 FAIL。修复 owner 精确增加 `packages/agent-harness/src/agent_harness/models/_streaming_contracts.py`、`_streaming_consumption.py`、`_streaming_events.py`、`_streaming_settlement.py`，并修改协调层 `_invocation_streaming.py` 与类型证据 `_invocation_evidence.py`；原 `test_controlled_model_streaming_runtime_contracts.py` 按成功、取消/unknown、恢复/replay、deadline/guardrail 四个故障域拆为四个文件。重构不改变公开 façade、DTO、事件顺序、事务原子性或 schema；恢复回归曾以 2 FAIL 暴露旧 `_publish_persisted_stream` seam 丢失，补窄委托后转绿。DEV、OpenSpec、acceptance matrix 与 living plan 同属本轮状态/owner 校准范围，任何后续实质修订仍须重新冻结并从 Reviewer 1 重启。

### 3.9 Phase 18.1 第六轮 Reviewer 1 完成一致性修复

第六名 fresh Reviewer 1 对匹配冻结身份以 1 HIGH 阻断 Stage 1：`_streaming_consumption.py` 的最终文本/delta 冲突只抛普通异常，`_invocation_streaming.py` 会继续信任合法的 stopped+complete 关闭证明，使 `_streaming_settlement.py` 取消占位并发布 failed final usage。修复 owner 限定为 `packages/agent-harness/src/agent_harness/models/_streaming_consumption.py`、`packages/agent-harness/src/agent_harness/models/_invocation_streaming.py`、`tests/contracts/test_controlled_model_streaming_runtime_interruption_contracts.py` 与本轮状态/契约 owner 文档。public bound seam 先以一个 durable delta、冲突 final 和 stopped+complete 形成 RED，再强制 stable unknown、丢弃完整计量停止证明并保留 64 个未用 stream 占位与 final usage 容量。该修复不改变公开 DTO、schema、正常 stopped 结算或 Phase 18.2 范围。

### 3.10 Phase 18.1 第七轮 Reviewer 1 live smoke 证据修复

第七名 fresh Reviewer 1 对匹配冻结身份以 1 HIGH 阻断 Stage 1：`scripts/smoke_live_model_stream.py` 捕获 `RunOrchestrator.start_run()` 后会清空 result，导致 provider response 已观察后的本地 terminal/capacity/shared-budget/publication 失败被误写为 `external-blocked/provider_rejected`，甚至 `provider_called=false`。修复 owner 限定为该脚本、`tests/contracts/test_controlled_model_streaming_live_smoke_contracts.py`、typed-config delta、Product/API/DEV 与 living plan/matrix；公共 smoke 分类 seam 先稳定 RED，再把本地失败封闭为 `failed/contract_failure` 与退出 1，按 response/delta 保留调用事实，仅稳定 provider/network 错误映射 external-blocked。该修复不改变生产模型 façade、DTO、事件顺序、事务原子性或 Phase 18.2 范围。

### 3.11 Phase 18.1 第八轮 Reviewer 1 failure domain 修复

第八名 fresh Reviewer 1 对匹配冻结身份以 1 HIGH / 1 MEDIUM 阻断 Stage 1：通用 `model.provider_failed` 同时承载本地完整结果 guardrail 与外部 provider 故障，旧 classifier 仍会把本地失败误报 external-blocked；living plan 12.6 仍残留第七轮当前状态。修复 owner 增加 `packages/agent-harness/src/agent_harness/models/_settlement_contracts.py`、`_streaming_consumption.py`、`_streaming_settlement.py`、`_invocation_streaming.py` 与 guardrail/live 两个合同文件，并同步 API/typed-config/design/tasks/DEV/living plan/matrix。实际 bound guardrail 与 `LiveStreamSmokeExecutor` 两个公共 seam 先 RED，再用不进入 artifact 的封闭 `failure_domain=provider|runtime` 逐层传递来源；本地 policy/guardrail/capacity/cancel/stream 安全和未知编排错误统一 runtime，只有 provider domain 映射 external-blocked。该修复不改变公开 DTO、事件顺序、事务原子性或 Phase 18.2 范围。

### 3.12 Phase 18.1 第九轮 Reviewer 1 live smoke 收口与类型修复

第九名 fresh Reviewer 1 对匹配冻结身份完成两阶段审查，以 live smoke 本地编排异常可逃逸、committed/client 时延竞态、595 行脚本职责过载、两个新增核心文件残留 blanket Pyright 抑制/`Any` 共 4 MEDIUM 判定 FAIL。修复 owner 增加 `scripts/live_model_stream_contract.py`、`scripts/live_model_stream_probe.py`、`scripts/live_model_stream_execution.py`，保留 `scripts/smoke_live_model_stream.py` 为兼容导出与薄 CLI；同时修改 `_streaming_events.py`、`models/streaming.py`、`storage/stream_evidence_repositories.py` 与 live smoke contract。完整 `run()` migration 故障、并发 durable reader 和 CLI artifact I/O 先 RED，再由外层安全失败边界、commit→client happens-before、stdout 安全降级、TypedDict/准确 DML result 类型转绿。该修复不改变 artifact schema、公开模型 façade、事件顺序、事务原子性、真实调用授权或 Phase 18.2 范围。

### 3.13 Phase 18.1 第十轮 Reviewer 1 前驱与线性复杂度修复

第十名 fresh Reviewer 1 对匹配冻结身份完成两阶段审查，以 provider-domain 错误后本地 cleanup 仍误报 external-blocked、只核对现存前驱而允许缺失 ordinal、当前状态误写 33 个 untracked，以及 consumer/adapter 每片重建完整文本的二次方复制共 4 MEDIUM 判定 FAIL。修复 owner 增加 `scripts/live_model_stream_execution.py`、`storage/evidence_repositories.py`、`storage/stream_evidence_repositories.py`、`events/sinks/postgresql.py`、`models/_streaming_consumption.py`、`adapters/models/pydantic_ai.py` 与对应 live/capacity/PostgreSQL/provider contracts；完整 `run()` 组合失败、SQLite 缺失前驱、重复/非连续 validator 和高碎片结构不变量先 RED，再由本地最终归因、完整唯一 `1..n-1` 前缀校验、数据库唯一约束与线性列表累计转绿。该修复不改变 artifact schema、公开模型 façade、事件顺序、容量公式、真实调用授权或 Phase 18.2 范围。

### 3.14 Phase 18.1 第十一轮 Reviewer 1 SDK usage 与 start-run 失败修复

第十一名 fresh Reviewer 1 对匹配冻结身份执行 Stage 1，以 SDK usage accessor 返回 bool/负数/非整数时 `result()` 抛出原始校验异常、`aclose()` 再次读取并重复逃逸，导致 durable needs-review 未执行的 1 HIGH，以及 `RunOrchestrator.start_run()` 本地异常被吞入空 result、在 executor 返回 provider-domain 错误且 cleanup 成功时误报 external-blocked 的 1 MEDIUM 判定 FAIL，Stage 2 未执行。修复 owner 保持在 `adapters/models/pydantic_ai.py`、`scripts/live_model_stream_execution.py` 与既有 provider/runtime/live contracts；public bound invocation 和完整 live `run()` 先 RED，再由单次缓存 usage 读取、安全 unknown close 与独立 start-run 失败事实转绿。该修复不改变公开 DTO、artifact schema、事件顺序、容量公式、真实调用授权或 Phase 18.2 范围。

### 3.15 Phase 18.1 第十二轮 Reviewer 1 context 创建前 deadline 修复

第十二名 fresh Reviewer 1 对匹配冻结身份执行 Stage 1，以 Pydantic adapter 在调用方已请求迭代、但 SDK context 创建前 deadline 已耗尽时仍按 `_started` 返回 unknown，错误保留 66 个槽位、预算、lease 并进入 needs-review 的 1 HIGH 判定 FAIL，Stage 2 未执行。修复 owner 保持在 `adapters/models/pydantic_ai.py` 与 runtime interruption contract；真实 Pydantic public bound invocation 先 RED，再由“迭代请求”与“SDK context 已创建”的精确事实分离转绿。context 尚未创建时安全 close 返回 not-started；一旦创建，仍默认 unknown，不把本地清理冒充远端停止。该修复不改变公开 DTO、事件顺序、容量公式或 Phase 18.2 范围。

### 3.16 Phase 18.1 第十三轮 Reviewer 1 唯一绝对 route deadline 修复

第十三名 fresh Reviewer 1 对匹配冻结身份执行 Stage 1，以 invocation 在 `prepare_stream()` 后重启完整 `total_timeout_ms`、使完整结果 guardrail/尾部分片与 delta 持久化发布可越过冻结剩余 deadline 的 1 HIGH，以及 Product Spec 当前授权状态漂移的 1 MEDIUM 判定 FAIL，Stage 2 未执行。修复 owner增加 `_invocation_streaming.py`、runtime guardrail contract 与 Product Spec；prepare 已耗时 + 尾部 delta sink 变慢的 public bound 节点先 RED，再由 prepare 前建立的唯一 `asyncio.timeout_at` 覆盖 prepare、消费、guardrail、尾部分片和 delta 持久化/发布后转绿。Product Spec 仅同步“已授权、尚未运行、成功未建立”，不伪造外部 PASS。该修复不改变公开 DTO、事件顺序、容量公式或 Phase 18.2 范围。

### 3.17 Phase 18.1 第十四轮 Reviewer 1 durable delta 与 collector 前置上限修复

第十四名 fresh Reviewer 1 对匹配冻结身份执行 Stage 1，以 delta intent 已提交但公开失败时本地 `chunks` 计数仍偏小、使 stopped+complete 结算错误取消 `result_persisted` 槽位，以及单个任意大 provider fragment 先进入 adapter/runtime collector并形成整块 UTF-8 bytes 后才检查总上限的 2 HIGH 判定 FAIL，Stage 2 未执行。修复 owner增加 `models/providers.py`、`streaming.py`、`_streaming_consumption.py`、`_streaming_events.py`、`_streaming_settlement.py`、`adapters/models/pydantic_ai.py` 与 provider/interruption contracts；两个公共节点先 RED，再由“intent 提交后登记 durable chunk + 未公开 durable 前缀强制 needs-review”以及 DTO 边界无整块复制的有界 UTF-8 计数、合法字节数缓存和 adapter 累计前校验转绿。该修复不改变公共序列化字段、event identity/schema、容量公式、真实调用授权或 Phase 18.2 范围。

### 3.18 Phase 18.1 第十五轮 Reviewer 1 内嵌配置键脱敏同义性修复

第十五名 fresh Reviewer 1 对匹配冻结身份执行 Stage 1，以 incremental guard 给既有无左边界的 `api_key|password|secret|token` 规则额外添加 `(?<![A-Za-z0-9_])`，使 `OPENAI_API_KEY`、`db_password`、`client_secret`、`access_token` 等常见配置键值保留原文的 1 HIGH 判定 FAIL，Stage 2 未执行。修复 owner增加 `models/streaming.py`、security/runtime-success contracts、Product/API/OpenSpec/DEV 与 living plan/matrix；direct guard 的任意单切点/逐字符输入和真实 bound invocation 的 outbox/公共事件两层用例先 RED，再通过精确复用既有 `redact_secrets()` 边界转绿。该修复不改变公共序列化字段、event identity/schema、容量公式、真实调用授权或 Phase 18.2 范围。

### 3.19 Phase 18.1 第十六轮 Reviewer 1 commit-ack 与 scheme-only 修复

第十六名 Reviewer 1 对匹配冻结身份执行 Stage 1，以 delta intent commit 已成功但返回确认丢失时本地 `chunks` 仍为 0、结算只扫描空前缀而漏掉 durable delta 的 1 HIGH，以及 scheme-only `authorization: Bearer|Basic ` 与既有正则回退不同义的 1 MEDIUM 判定 FAIL，Stage 2 未执行。修复 owner增加 `_streaming_settlement.py`、`streaming.py`、security/interruption contracts 与 API/OpenSpec/DEV/living plan/matrix；3 个参数节点先 RED，再由完整 durable group 扫描和 scheme-only 流结束回退转绿。该修复不改变公共序列化字段、event identity/schema、容量公式、真实调用授权或 Phase 18.2 范围。

### 3.20 Phase 18.1 第十九轮 Reviewer 1 文件职责拆分

第十九名 Reviewer 1 对匹配身份判定 Stage 1 PASS，但 Stage 2 以三个生产文件和两个测试文件超过 500 有效代码行默认门槛判定 1 MEDIUM FAIL。拆分 owner精确增加 `adapters/models/_pydantic_ai_streaming.py`、`events/sinks/_postgresql_streaming.py`、`storage/usage_attempt_review_repository.py`、`tests/contracts/test_controlled_model_streaming_capacity_config_contracts.py` 与 `tests/contracts/test_controlled_model_streaming_runtime_cancellation_contracts.py`，并同步修改原五个文件、SDK event helper、provider structure contract 与 owner 文档。拆分只移动惰性 stream lifecycle、PostgreSQL stream 校验、同 `_session` attempt-review 以及配置/取消故障测试，不改变公共导出、UoW 属性、connection/session 所有权、事件顺序、测试节点语义或 schema；拆后相关十文件的非空非注释行上界均低于 500。

### 3.21 Phase 18.1 第二十轮 Reviewer 1 验收节点路径修正

第二十名 Reviewer 1 对匹配身份判定 Stage 2 PASS，但 Stage 1 发现 `docs/acceptance-matrix.md` 的 AC-086 仍引用职责拆分前 `test_controlled_model_streaming_runtime_interruption_contracts.py::test_cancelled_stream_respects_provider_stop_proof`，该旧节点现场退出 4，因而以 1 MEDIUM FAIL 阻断。修正仅把矩阵映射改为真实的 `test_controlled_model_streaming_runtime_cancellation_contracts.py::test_cancelled_stream_respects_provider_stop_proof`；新节点 `2 passed`、验收文档合同 `23 passed`，不改变生产代码、测试语义、公开 API、schema、事件顺序或 Phase 18.2 范围。

### 3.22 Phase 18.1 第二十一轮 Reviewer 1 started 后 telemetry 取消修复

第二十一名 Reviewer 1 对匹配身份在 Stage 1 发现：双预留、65 个 stream 占位与内部 started 已耐久后，`_invocation_streaming.py` 在统一 `try/except` 之外等待 telemetry；此时取消会泄漏原始 `CancelledError`，保留 65 个 started 占位与 started usage、outstanding 66，且 provider 尚未 prepare/迭代，因而以 1 HIGH FAIL 阻断，Stage 2 未执行。新增 `tests/contracts/test_controlled_model_streaming_runtime_started_cancellation_contracts.py` 的真实 bound façade 节点先稳定 1 FAIL；生产修复只把 started 后 telemetry await 纳入既有 not-started 取消结算，节点转为 1 PASS，全部 runtime 合同 `24 passed`，quality 718 files、Pyright 0、import boundary、change/all strict 33/33 与 diff check PASS。该修复保持 started/high-water、取消全部 65 占位、发布 `provider_called=false` 的 cancelled usage final 并把 outstanding 收敛为 0，不改变公开 DTO、schema、事件顺序、provider adapter 或 Phase 18.2 范围。

### 3.23 Phase 18.1 第二十二轮 Reviewer 1 runtime deadline not-started 修复

第二十二名 Reviewer 1 对匹配身份确认 3.22 的显式取消已闭合，但 Stage 1 发现 route deadline 在 started telemetry 前计算、`asyncio.timeout_at` 在 telemetry 后才生效；慢 telemetry 加阻塞 prepare 会在 SDK context/provider 迭代前自然到期，却被通用异常分支记为 `model.provider_failed/outcome=failed`，因而以 1 HIGH FAIL 阻断，Stage 2 未执行。同一公共 bound 测试文件新增慢 telemetry + runtime natural timeout 节点，禁止外部 `task.cancel()` 或篡改 adapter deadline，先稳定 1 FAIL；修复把绝对 deadline 恢复到 telemetry 后、prepare 前建立，并将 `TimeoutError + close.state=not_started` 统一归为 `model.invocation_cancelled/outcome=cancelled`，节点转为 1 PASS，runtime `25 passed`、quality 718 files、Pyright 0 与 import boundary PASS。该修复保留 started/high-water、取消 65 个占位、发布零调用 cancelled usage 并把 outstanding 收敛为 0，不改变 context 已创建后的 unknown、尾部 deadline、公开 DTO、schema、事件顺序或 Phase 18.2 范围。

### 3.24 Phase 18.1 最终 Reviewer 2/3 owner 与真实 adapter 证据修复

第二十三名 fresh Reviewer 1 对身份 `9accf651…` Stage 1/2 PASS、0 findings；随后 fresh Reviewer 2/3 独立收敛到同两项缺口并均在 Stage 1 停止：精确 owner 清单漏列 `packages/agent-harness/src/agent_harness/models/_settlement_contracts.py` 与 `Product-Spec-CHANGELOG.md`，任务 3.7 又缺少真实 `PydanticAIModelProvider` 在 telemetry 后由 runtime 自然 deadline 取消 client acquisition/permit 的 public-seam 证据。两路径现已补回 design/tasks/AC-086 producer mapping；新节点不改 `PreparedPydanticStream.deadline`、不外部 `task.cancel()`，阻塞真实 adapter 的 client acquisition，由 runtime `asyncio.timeout_at` 自然取消，并在 durable cancelled 收口后用同一 provider 再次 prepare 证明 permit 已释放。测试先因 ORM 行越出 UoW 形成 RED 1 FAIL，改为事务内提取纯值后 GREEN 1 PASS；runtime `26 passed`、验收语义合同 `34 passed, 1 skipped`，quality 718 files/Pyright 0/import boundary、change/all strict 33/33 与 diff check PASS。本轮 owner 为上述两个既有路径、`tests/contracts/test_controlled_model_streaming_runtime_started_cancellation_contracts.py`、OpenSpec design/tasks、acceptance matrix、DEV/living plan/matrix；当前重冻结并从第二十四名 Reviewer 1 重启。

### 3.25 Phase 18.1 第二十四轮 Reviewer 1 DEV 精确 owner 修复

第二十四名 fresh Reviewer 1 对身份 `06381892…` 独立匹配后确认 AC-085～088 与 3.24 的真实 Pydantic client/permit 自然 deadline 节点未见第二项偏差，但 `DEV-PLAN.md` 的“冻结文件所有权与 producer”仍漏列 `packages/agent-harness/src/agent_harness/models/_settlement_contracts.py` 与 `Product-Spec-CHANGELOG.md`，因此以 1 MEDIUM 判定 Stage 1 FAIL、Stage 2 未执行。修复只把两路径及其 failure-domain/AC-087 维护边界补入 DEV 精确清单，不改生产、测试、OpenSpec 行为或验收状态；旧 verdict 失效，当前重冻结并从第二十五名 Reviewer 1 重启。

### 3.26 Phase 18.1 第二十五轮 Reviewer 1 composition close 修复

第二十五名 fresh Reviewer 1 对身份 `489ffe1d…` 在 Stage 1 证明 `ModelInvocationService.aclose()` 只经 router/provider 关闭 client factory，活动 `PreparedPydanticStream` 的 SDK context、阻塞 pull 与 permit 均未被拥有：关闭返回后 `context_exits=0`、调用任务未结束且 permit 仍为 0，因而以 1 HIGH FAIL 阻断，Stage 2 未执行。修复 owner 精确增加 `packages/agent-harness/src/agent_harness/adapters/models/pydantic_ai.py`、`packages/agent-harness/src/agent_harness/adapters/models/_pydantic_ai_streaming.py` 与新增 `tests/contracts/test_controlled_model_streaming_runtime_composition_close_contracts.py`，并同步 OpenSpec design/tasks、AC-086 矩阵、DEV/living plan/matrix。public bound 节点先稳定 RED，再由 provider 活动 stream 登记、外部取消并等待 invocation durable unknown 结算、重入防死锁、SDK context 退出、permit 释放和 client factory 最后关闭转为 GREEN；最终保持唯一 started 事件、usage needs-review、66 个容量围栏与稳定 `model.provider_side_effect_unknown`。受影响 Pydantic/streaming 11 文件全绿，quality 719 files、Pyright 0、import boundary PASS；当前完成 strict/diff 后重冻结并从第二十六名 Reviewer 1 重启。

### 3.27 Phase 18.1 第二十六轮 Reviewer 1 prepare-close 竞态修复

第二十六名 fresh Reviewer 1 对身份 `102514e8…` 初末检一致，确认 3.26 的活动 context/pull/permit 修复，但证明 `prepare_stream()` 只在 permit/client acquisition 完成后登记 active stream：composition close 对正在 prepare 的调用快照为空，返回后 invocation 仍活跃，并可在 queue timeout 后误记 `model.bulkhead_saturated/outcome=failed`，因而以 1 HIGH FAIL 阻断，Stage 2 未执行。修复 owner仍为 `adapters/models/pydantic_ai.py`、`adapters/models/_pydantic_ai_streaming.py` 和 composition-close contract；新增的 client-acquisition 与单 permit waiter 两个 public bound 节点先稳定 2 FAIL。私有 `PydanticStreamLifecycle` 现于取得 permit 前登记 invocation task，以同一 lock 原子转移到 active prepared stream；close 先取消并等待 prepare task 完成 durable cancelled/not-started 结算，再关闭 active context/permit，最后关闭 client factory。三类 composition 节点 3/3、受影响 11 文件、quality 719 files/Pyright 0/import boundary、change/all strict 33/33、diff check 与验收语义/identity 22 项均 PASS；当前重冻结并从第二十七名 Reviewer 1 重启。

### 3.28 Phase 18.1 第二十七轮 Reviewer 1 router 并发关闭修复

第二十七名 fresh Reviewer 1 对身份 `e220af91…` 初末检一致，确认 3.26～3.27 的活动 context、prepare task、permit 与 client 生命周期闭合，但证明首个 `ModelRouter.aclose()` 阻塞在 provider/client close 时，并发第二个 close 因 `_closed` 已置位直接返回，允许上层 `RuntimeComponents` 提前释放 storage，因而以 1 HIGH FAIL 阻断，Stage 2 未执行。修复 owner 精确增加 `packages/agent-harness/src/agent_harness/models/router.py`，继续复用 composition-close contract，并同步 OpenSpec design/tasks、AC-086 矩阵、DEV/living plan/matrix。public service 并发 close 节点先稳定 RED；router 现以共享完成事件等待同一 provider 关闭结果，provider close 失败或首个关闭者取消均保存为失败事实，后续 close 显式失败而不伪装成功。composition 6/6、受影响 12 文件与 quality 719 files/Pyright 0/import boundary PASS；当前完成 strict/diff、重冻结并从第二十八名 Reviewer 1 重启。

### 3.29 Phase 18.1 第二十八轮 Reviewer 1 authorization 跨块安全修复

第二十八名 fresh Reviewer 1 对身份 `beba0c21…` 初末检一致，逐项核对 20 个 Requirement/60 个 Scenario，确认 AC-085～087 和 3.28 的 router 并发关闭修复，但证明 `authorization=&secretvalue` 被增量 guard 在值起点当成无值 false-positive，原文先写入 public durable intent，因而以 1 HIGH FAIL 阻断，Stage 2 未执行。修复 owner 仍为 `packages/agent-harness/src/agent_harness/models/streaming.py`、安全合同与 runtime-success public bound 合同，并同步 OpenSpec tasks、DEV/living plan/matrix。逐切点 guard 与 public bound SQLite 两节点先稳定 2 FAIL；authorization 现使用与既有正则同义、允许 `&` 进入凭据值的独立终止集合，通用 key/value 仍以 `&` 分隔字段。相关两文件 27/27、Phase 18.1 聚焦套件、quality 719 files/Pyright 0/import boundary、change/all strict 33/33、diff check 与验收语义/identity 22 项均 PASS；当前重冻结并从第二十九名 Reviewer 1 重启。

### 3.30 Phase 18.1 第二十九轮 Reviewer 1 cookie parity 与 retained hard bound 修复

第二十九名 fresh Reviewer 1 对身份 `f3f48096…` 初末检一致，逐项核对 20 个 Requirement/60 个 Scenario；确认 AC-085～087、authorization 和 lifecycle 修复，但证明 cookie/set-cookie 空值或分号起始被过度脱敏，且配置 128-byte 候选会先保留 136 bytes 再抛错，因而以 2 MEDIUM 判定 Stage 1/2 FAIL。修复 owner仍为 `models/streaming.py`、安全合同与 runtime-success public bound 合同，并同步 OpenSpec tasks、DEV/living plan/matrix。首批逐切点/public/hard-bound 节点稳定 9 FAIL；209 组全 split 差分又将 authorization scheme 分隔符回退与 cookie 前导换行固化为 8 FAIL。cookie 回溯同义状态、scheme fallback 和追加前 UTF-8 字节预算闭合后，两文件 43/43、差分 0 mismatch、Phase 18.1 聚焦套件、quality 719 files/Pyright 0/import boundary、change/all strict 33/33、diff check 与验收语义/identity 22 项均 PASS；当前重冻结并从第三十名 Reviewer 1 重启。

### 3.31 Phase 18.1 第三十轮 Reviewer 1 左侧单词边界与类型边界修复

第三十名 fresh Reviewer 1 对身份 `995091d7…` 初末检一致并完成 Stage 1/2；Stage 1 证明 `_potential_suffix_start()` 发布安全前缀后丢失 authorization/cookie 的左侧 Unicode `\b` 上下文，Stage 2 证明 `event_capacity_repositories.py` 新增文件级 Pyright suppression、`pydantic_ai.py` 的 stream protocol 新增 `Any` 返回，使静态门禁说明与源码不符，因而以 2 MEDIUM 判定 FAIL。修复 owner限定为 `models/streaming.py`、`storage/event_capacity_repositories.py`、`adapters/models/pydantic_ai.py`、`adapters/models/_pydantic_ai_streaming.py`、security/provider contracts、OpenSpec tasks 与状态文档。10 个边界样例和两个结构节点先形成 9 FAIL；guard 现携带已发布输出的 Unicode 词字符状态并支持跳过无效 `set-cookie` 起点后识别合法重叠 `cookie`，仓储在 `object` 边界局部收窄，SDK stream 返回窄 context protocol。安全文件 39/39、受影响四文件全绿，1,409 条文本/31,716 次差分 0 mismatch，聚焦 `168 passed, 6 skipped`；全仓 quality 719 files/Pyright 0/import boundary、change/all strict 33/33、acceptance 41 passed / 1 skipped 与 diff check 均 PASS。当前重冻结并从第三十一名 fresh Reviewer 1 重启。

### 3.32 Phase 18.1 第三十一轮 Reviewer 1 类型门禁真实性修复

第三十一名 fresh Reviewer 1 对身份 `c21fd24…` 初末检一致并逐项审完 20 个 Requirement/60 个 Scenario；Stage 1 PASS，Stage 2 证明 `test_pydantic_stream_agent_protocol_has_narrow_context_return_type` 只排除 `Any`，将返回注解退化为 `object` 或 `dict[str, object]` 仍会通过，因而以 1 MEDIUM 判定 FAIL。修复 owner限定为 `adapters/models/pydantic_ai.py`、`adapters/models/_pydantic_ai_streaming.py`、provider contract、新增静态类型夹具、OpenSpec tasks 与状态文档。宽类型 mutant 先稳定 RED；门禁现精确断言 `StreamEventContext` 与公开导出，adapter 内 `TYPE_CHECKING` 赋值由 Pyright 证明锁定 SDK 返回值兼容窄 protocol，独立夹具证明本地正确 double 兼容且错误 shape 必须失败。该静态正例同时发现旧 protocol 的 `__aexit__` 使用 `object` 参数并不兼容标准 async context manager，现已收窄为异常三元组的标准位置参数与 `bool | None` 返回。聚焦 `169 passed, 6 skipped`，全仓 quality 720 files/Pyright 0/import boundary、change/all strict 33/33、acceptance 41 passed / 1 skipped 与 diff check 均 PASS。当前重冻结并从第三十二名 fresh Reviewer 1 重启。

### 3.33 Phase 18.1 第三十二轮 Reviewer 1 静态类型夹具 owner 对齐

第三十二名 fresh Reviewer 1 对身份 `0d69f9f4…` 初末检一致并逐项审完 20 个 Requirement/60 个 Scenario；行为全部匹配，Reviewer31 类型修复复核 PASS，Stage 2 也以 0 findings PASS。Stage 1 发现新增 `tests/contracts/controlled_model_streaming_context_typecheck.py` 虽已进入 change matrix，但 OpenSpec design、tasks 8.5 与 DEV-PLAN 的精确 owner 清单漏项，和“清单外 owner 已补回”声明矛盾，因而以 1 MEDIUM 判定 FAIL。该路径现已逐处补入 design 新增聚焦测试、tasks 8.5、DEV-PLAN 冻结合同与本矩阵；生产、测试行为和类型实现不变。strict、acceptance 与 diff 复验后重冻结，并从第三十三名 fresh Reviewer 1 重启。

### 3.34 Phase 18.1 Reviewer 2/3 当前证据与测试说明修复

第三十三名 fresh Reviewer 1 对身份 `23698bdb…` 初末检一致并以 20/60、Stage 1/2、0 findings 判定 PASS。Reviewer 2/3 对同一身份均确认行为完整，但独立发现 living plan 的 Handoff/evidence 表仍以 43 untracked、174 collected/168 passed/6 skipped、719 files 描述“当前”候选；Reviewer 3 另按 code-review 规则发现 12 个测试模块说明和一个 completion-only helper docstring 写入 `Phase 18.1/Phase 18` 开发阶段标签。Reviewer 2 判定 Stage 1 的 1 MEDIUM、Stage 2 PASS；Reviewer 3 判定 Stage 1/2 各 1 MEDIUM，静态 `1+2` 未通过。living plan 现统一为 44 untracked、175 collected/169 passed/6 skipped、quality 720 files；测试说明改用稳定能力名称，`tests/**` 不再含开发阶段标签，行为不变。当前完成轻量复验、重冻结并从第三十四名 fresh Reviewer 1 重启。

### 3.35 Phase 18.1 第三十四轮 OpenSpec 当前证据与长期 API 标签修复

第三十四轮候选 `6407d310…` 的 Reviewer 1/2 均 Stage 1/2 PASS、0 findings。Reviewer 3 重新核对 20/60 后确认行为完整，但发现 active `tasks.md` 仍把最新聚焦结果写成 168 passed / 6 skipped，而当前候选为 169/6；同时 `API-Contract.md` 五处新增或修改的长期正文使用 `Phase 18.1` 开发阶段标签，因而判定 Stage 1/2 各 1 MEDIUM。tasks 当前证据已校准为 169/6；长期 API 正文改用稳定 `controlled-model-streaming` 与 `MOD-004` 标识，不改变契约行为。旧 verdict 全部失效，当前轻量复验、重冻结并从 Reviewer 1 重启。

### 3.36 Phase 18.1 重型门禁 live stream 可执行入口修复

候选 `25cb9b53…` 的 Reviewer 1/2/3 均在同一身份上完成 Stage 1/2 PASS、0 findings；静态 `1+2` 后，真实 PostgreSQL 5/5、全量 `1712 passed, 230 skipped`、eval 11/11、local/service smoke、build 与 license 均 PASS，AC-065 在同一全量运行中通过且未调整 5 秒阈值。随后按用户授权执行 `make smoke-live-model-stream` 时，在 provider 预检前稳定失败于 `ModuleNotFoundError: No module named 'scripts'`：Makefile 以文件路径运行会破坏脚本的包导入。`tests/contracts/test_controlled_model_streaming_live_smoke_contracts.py` 先增加 `python -m scripts.smoke_live_model_stream` 入口合同并形成 1 FAIL，再将 Make target 切换为模块入口后 1 PASS。修复后流式目标诚实返回 `hosted-unverified/credential_missing/provider_called=false`，completion 目标返回 `hosted-unverified/typed_preflight_missing/provider_called=false`；两者都未触发网络或 token 消耗。该 Makefile 与合同测试修订属于实质 diff，使 `25cb9b53…` 的 verdict 和重型证据身份失效。修订后聚焦 `169 passed, 6 skipped`、quality 720 files、acceptance 41/1、change/all strict 33/33 与 diff check 均 PASS；当前必须重新冻结并从 Reviewer 1 开始，最终静态 `1+2` PASS 后再对最终实现身份运行一次重型门禁。

### 3.37 Phase 18.1 Reviewer 1 测试文件职责拆分

候选 `c67f5506…` 的 Reviewer 1 初末身份一致；Stage 1 对 20 Requirements/60 Scenarios、AC-085～088、MOD-004、owner/producer/CI/范围全部 PASS。Stage 2 证明新增入口断言令 `tests/contracts/test_controlled_model_streaming_live_smoke_contracts.py` 从门槛上的 500 增至 501 有效行，且该文件同时承载 schema、时钟、runtime、CLI 与 CI 接线职责，以 1 MEDIUM FAIL。Make/manifest/双 CI/artifact 接线合同已整体迁移到职责匹配、规模较小且已在精确 owner 清单内的 `tests/contracts/test_ci_pipeline_contracts.py`；不新增 helper、生产行为、schema 或 CI 行为。修订后聚焦 168/6、pipeline 7/7、受影响两文件 20/20、quality 720 files、acceptance 41/1、change/all strict 33/33 与 diff check 均 PASS；实质修订使 `c67f5506…` verdict 失效，当前重冻结并从 Reviewer 1 重启。

### 3.38 Phase 18.1 最终静态审查与重型门禁

职责迁移后的实现候选 `361678bf…` 由 Reviewer 1/2/3 在同一 HEAD、tracked diff、44 条 untracked manifest 上分别完成 Stage 1/2 PASS、0 findings；20 Requirements/60 Scenarios、AC-085～088、MOD-004、owner/producer/CI、483/146 有效行职责和安全/类型/并发/恢复边界全部闭合。静态 `1+2` 后只运行一次最终重型门禁：临时 PostgreSQL 16 合同 5/5；全量 `1712 passed, 230 skipped in 899.54s`，AC-065 在同一运行中通过且未改 5 秒阈值；eval 11/11、local fake 1.889 秒、service smoke、build、license 均 PASS。已授权 stream/completion 目标分别因 `credential_missing` / `typed_preflight_missing` 返回 hosted-unverified、`provider_called=false`，没有网络或 token 消耗。随后最终证据候选 `e59eb16c…` 的 Reviewer 1/2/3 也均 Stage 1/2 PASS、0 findings，并写入 `clean`。

### 3.39 Phase 18.1 主规格同步与归档

用户明确授权同步、归档与本地提交后，OpenSpec 将六组 delta 中的 20 条新增 Requirement 同步到 `agent-registry-model-context`、`canonical-events-artifacts`、`controlled-model-streaming`、`model-usage-evidence`、`sse-event-streaming` 与 `typed-config` 主规格；没有 modified、removed 或 renamed 项。`controlled-model-streaming` 的 proposal、design、六组 delta specs 与 37/37 tasks 已整体移动到 `openspec/changes/archive/2026-07-30-controlled-model-streaming/`，`openspec list --json` 当前为空。归档只改变规格落点与维护状态，不改变已审实现，故只运行 strict、delta/main 一致性、引用链与 diff 检查，不重跑最终重型行为门禁。

## 4. 首个 Change 的串行边界

### 4.0 Phase 18 async 协议既有测试所有权补充

Phase 18 把公共 `ModelProvider.complete()`、`ModelRouter.execute()`、兼容入口 `ModelRouter.route()` 与 fake provider 迁移为 async seam；`route()` 只允许 plan 后 `await execute()`，不得引入同步桥接。除第 3 节 Phase 18 行已列的新增测试外，同一 change 的单一 owner 还精确拥有以下既有同步 provider doubles、同步 Pydantic AI adapter seam、直接 adapter callers 或 route caller：

- `tests/contracts/agent_delegation_service_runtime_test_support.py`
- `tests/contracts/test_embedding_usage_lifecycle_contracts.py`
- `tests/contracts/test_model_usage_idempotency_contracts.py`
- `tests/contracts/test_model_usage_invocation_contracts.py`
- `tests/contracts/test_model_usage_local_capacity_contracts.py`
- `tests/contracts/test_model_usage_postgresql_concurrency_contracts.py`
- `tests/contracts/test_model_usage_recovery_contracts.py`
- `tests/contracts/model_usage_recovery_test_support.py`
- `tests/contracts/test_model_usage_approval_outbox_recovery_contracts.py`
- `tests/contracts/test_model_usage_publication_recovery_contracts.py`
- `tests/contracts/test_model_usage_ack_loss_recovery_contracts.py`
- `tests/contracts/test_model_usage_unknown_terminal_fencing_contracts.py`
- `tests/contracts/test_model_usage_runtime_composition_contracts.py`
- `tests/contracts/test_shared_parent_budget_invocation_contracts.py`
- `tests/contracts/test_usage_execution_authority_contracts.py`
- `tests/contracts/test_usage_identity_boundary_contracts.py`
- `tests/contracts/test_usage_invalid_provider_result_contracts.py`
- `tests/contracts/test_agent_registry_adapter_error_contracts.py`
- `tests/contracts/test_pydantic_ai_usage_validation_contracts.py`
- `tests/contracts/test_model_usage_provider_adapter_contracts.py`
- `tests/contracts/test_agent_registry_router_model_contracts.py`
- `tests/integration/test_service_approval_delegation_contracts.py`
- `tests/integration/test_service_delegation_ordering_resume_contracts.py`

这些文件只允许为 async/await 协议迁移及保持原有断言语义而修改，不能借机弱化预算、恢复、usage、错误或 delegation 合同。实现必须先观察 coroutine/await、`Agent.run()` 或 deadline 合同不匹配的可解释红灯，再迁移 doubles/callers；`test_model_usage_provider_adapter_contracts.py` 的同步 helper/agent 替身迁移必须保留 timeout secret 脱敏、成功 usage 持久化与 missing-usage 断言。静态扫描或红灯若证明还有第 19 个未列既有测试必须修改，先停止实现，更新 proposal/design/tasks/本矩阵并重新完成契约 `1+2` review，不能用“相关测试”扩权。

`controlled-real-model-runtime` 不允许按“配置组、provider 组、测试组”拆为并行 worktree，原因是它们共同决定同一个安全不变量：

```text
typed deployment config
  -> deployment 与 agent allowlist 交集
    -> 请求缩权
      -> 冻结 route plan
        -> budget / policy / audit
          -> provider side effect
            -> usage / cost / latency / error settlement
```

以下文件或语义任一变化都会使其他部分的契约与 review 失效：

- `ModelSettings`、agent descriptor 与 profile/agent YAML shape；
- `ModelRequest`、`ModelRoutePlan`、`ModelRouter.plan/execute`；
- provider factory/client 的 endpoint、credential、timeout、retry 和 bulkhead；
- shared budget 的价格 identity、reservation 与 settlement；
- CLI/API/worker composition；
- negative contract、integration、smoke 与维护文档。

可以并行派 sub-agent 做只读代码调查、威胁建模或独立审查，但写入由单一 owner 在一个 worktree 串行整合。

Phase 18.1 同样不能拆成“provider stream 组”和“event/usage 组”。一旦 adapter 观察到 delta，event capacity、durable prefix、部分 usage、budget reservation、retry 禁止和 terminal fencing 就成为同一个不可分割的安全不变量：

```text
frozen route / budget / stream capacity
  -> provider delta normalization
    -> cross-chunk safety
      -> durable chunk commit
        -> completed / usage settlement
          -> run terminal
```

RUN-006 / CLI reader 可以保持独立的 transport seam，但其 contract tests 与 API 文档属于 Phase 18.1 集成范围。reader 断线只停止读取；任何 worktree 都不得借 reconnect 引入 provider cursor、SDK resume token 或第二次 provider 调用。

## 5. 共享接口和验收约束

### 5.1 Phase 18 → 18.1

- Phase 18.1 只扩展 Phase 18 已冻结的 deployment/route/provider/cancel/usage seam，不得重新引入请求直选 provider、endpoint 或 credential。
- Phase 18 的非流式 text behavior、默认 fake/local 离线门禁和 opt-in live smoke 必须保持兼容。
- 任何 delta 之前仍须完成 route、budget 和受信最大 event capacity reservation；已观察 delta 后禁止 retry/fallback 到新 attempt。
- SSE/CLI 只读 committed CanonicalEvent；subscriber 生命周期不拥有 durable run/provider 生命周期。

### 5.2 Phase 18.1 → 18.2

- Phase 18.2 复用 Phase 18.1 已冻结的 stream/cancel/partial-result seam；首个 delta 之前也只有受信 not-started 才允许切换，观察或提交任一 delta 后永久禁止 failover。
- Route chain 只扩展 deployment/provider 候选，不重做 CanonicalEvent/SSE/CLI reader；reader disconnect/reconnect 不拥有 provider 选择权。
- 每个候选独立绑定 endpoint、credential、catalog、Bulkhead、retry 和预算；禁止把同 provider model fallback 的共享 client/price 假设带入跨 deployment/provider chain。
- Phase 18.2 与 Phase 18.1 共享 router/invocation/adapter/settlement/recovery/tests/docs，必须从 Phase 18.1 归档 HEAD 串行实现。

### 5.3 Phase 18.2 → 19

- Phase 19 只扩展 Phase 18/18.1/18.2 已冻结的 deployment/route-chain/provider/result seam，不得重新引入由请求直接选 provider 的旁路。
- text-only public behavior 必须保持兼容；structured capability 必须显式声明，未声明时 fail closed。
- price、timeout、retry、attempt 和 usage evidence 继续由同一 route identity 关联。
- Phase 18.1 只交付普通文本增量，Phase 18.2 只交付普通文本 route-chain failover；structured delta 拼装、repair、schema stream/failover 仍须在 Phase 19 或其后独立契约中决定。

### 5.4 Phase 19 → 20

- tool intent 与业务 structured output 使用不同判别类型，不能靠“某个 JSON 字段看起来像 tool”触发执行。
- provider adapter 只归一化 tool intent，不拥有 ToolRegistry、PolicyEngine、HITL 或实际执行权限。
- Phase 20 三个 change 共享 tool intent、loop identity、CanonicalEvent 和 replay 验收，因此默认是关联 change；各自 strict/review 之外还需联合审查。

### 5.5 Phase 20 → 21

- Phase 21 是结构演进，不得借 typed services、ports 或 state kernel 改写已经验收的 tool loop 行为。
- characterization/contract tests 必须先固定 Phase 20 的公开行为，再移动依赖方向。
- 任何新 DTO、port 或 event 状态如果改变公开 schema，先返回行为 change 更新 Product Spec/API Contract/OpenSpec，而不是藏在 refactor 中。

## 6. 并行与 Worktree 决策规则

### 6.1 Sub-agent

适合并行：

- 读取不同模块并返回 symbol/call-path 证据；
- 独立威胁建模、测试缺口分析、契约审查；
- 在不写共享文件时验证技术假设。

不适合并行：

- 多个 Agent 同时修改同一配置、DTO、router、composition、公共 export、矩阵或集成测试；
- 用一个 Agent 的摘要代替另一个 Agent 读取完整 Spec/change 原文；
- 让执行型 Agent 自行改变 change 范围、commit、sync 或 archive。

### 6.2 Worktree

只有同时满足以下四项，才允许两个写入型 change 并行 worktree：

1. 无顺序依赖；
2. 无共享公共接口或 schema；
3. 无共享验收、迁移或 review gate；
4. 无文件所有权冲突，包括集成文件和文档。

证明必须写入本矩阵并绑定当时的 Git HEAD。只满足“文件列表暂时不同”不够；共享 DTO、共同 budget/replay 语义或同一 smoke 都会使 changes 相关。

### 6.3 集成文件

`Makefile`、CI workflow、checker manifest、公共 `__init__.py` export、`DEV-PLAN.md`、本计划与本矩阵默认由主控独占。并行 worktree 只生产自己的稳定 seam 和证据，最后按 DAG 在主控 worktree 串行接入集成文件。

## 7. 状态更新协议

每次 session 开始：

1. 运行 Git/OpenSpec 基线命令；
2. 读取 active change 全部 artifacts；
3. 把本矩阵状态与磁盘事实比较；
4. 确认 owner 和 worktree 后才写入。

每次 session 结束：

1. 只把有证据的行推进一个状态；
2. 更新直接依赖、共享 seam、验收和文件集的任何变化；
3. Phase 18 非流式 smoke 缺授权、opt-in、隔离 credential 或受信 endpoint 任一前置时保持零调用并记录 hosted-unverified/CI skipped；仅当前置完整且已授权后发生网络、配额或 provider 故障时记录 external-blocked，二者都不得记成通过；
4. 把下一 owner、下一文件和下一门禁同步回上位计划的 Handoff Snapshot。

如果 change 产生了超出矩阵的共享接口、验收或文件，立即停止并行写入，先更新 proposal/design、本矩阵和 review 范围。任何 post-review 实质 diff 都使旧 verdict 失效。

## 8. 当前下一步

1. 当前提交基线为 `develop@f552451`；Phase 18 与 Phase 18.1 均已同步并归档，当前无 active change。Phase 18.1 实现候选 `361678bf…` 与最终证据候选 `e59eb16c…` 的 Reviewer 1/2/3 均 Stage 1/2 PASS、0 findings，最终 PostgreSQL、全量、eval、local/service smoke、build 与 license 均 PASS。已授权真实目标因本机前置不完整保持 hosted-unverified、零调用；当前只待用户已授权的本地提交。
2. 三类 complete/recover 嵌套省略场景先复现 6 个发布前失败；受控 started route/final 调用事实识别后，同 6 项、bulkhead 精确回归、完整 replay 58 项、usage/recovery 五文件 102 项执行及 quality 通过。真实 MiMo completion 仍未建立。
3. 编码 dot-segment 缺口已 RED 4 → GREEN 4，配置/组合根/retry 37 项及 quality 通过；主规格同步与归档只执行 strict、delta/main 一致性、引用链和 diff 检查，不重跑十分钟级行为门禁。
4. 实现与最终证据 `1+2`、最终重型门禁、主规格同步和归档均已闭合。唯一下一动作是完成本地提交后停下；真实调用未发生且不得读取或输出凭据，不自动 push、发布、部署或创建 Phase 18.2 change。

当前不存在 active change；Phase 18.2 仍需用户另行授权。
