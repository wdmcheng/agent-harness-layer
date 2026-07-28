## Source Links

- Product-Spec.md: FLOW-006；REQ-004、REQ-012、REQ-014、REQ-019、REQ-024、REQ-025；AC-077 至 AC-084；P0 完成定义
- DEV-PLAN.md: Phase 18「受控真实文本模型运行时」；Phase 18.1 顺序依赖；风险表中的 fake-only composition、ambient provider env、endpoint 外泄、线程池 timeout 项
- API-Contract.md: 5.29 `ModelUsageEvidence`、MOD-001、MOD-002、CFG-001、入口 / 调用方映射和 transport-only 流式边界
- ADR: `docs/adr/0001-p0-service-boundaries.zh-CN.md`、`docs/adr/0002-vendor-adapter-isolation.zh-CN.md`
- Design-Brief.md / CONTEXT.md: 本 change 不涉及 UI，仓库当前未提供这些文件

## Why

当前核心已经有 provider-neutral router、共享预算与 Pydantic AI adapter，但 runtime composition 仍拒绝所有非 `fake` provider，现有同步线程池 timeout 也不能证明网络调用已取消。Phase 18 现在必须把真实非流式文本调用纳入 typed config、只缩权路由、预算、审计和 durable usage 边界，同时保住默认 local/CI 完全离线。

## What Changes

- 把 model 配置扩展为 deployment-aware typed settings，以 canonical lower-snake identity 冻结稳定 deployment、模型范围、受信 endpoint policy 与 model/input-bound/price catalogs、credential、deadline/逐状态 retry/bulkhead、每 attempt 上界和 capability 合同；deployment 只引用受信 model catalog，不自报 envelope/价格后自证。
- 让 credential 只从 `AGENT_HARNESS_*` typed secret 字段或受控 `_FILE` 进入 composition root；provider 原生 ambient key、admin key、organization、project、webhook、custom headers、base URL 与 proxy 不形成第二条隐式配置或出站身份路径。锁定 SDK 即使内部检查 ambient env，也必须由不修改进程全局环境的私有 lazy client factory 与出站 transport allowlist 隔离，确保其不能改变 client identity、目标或任何出站 header。
- 在 application startup 前校验 endpoint exact origin、credential-origin 绑定和 local loopback 例外，拒绝 userinfo/query/fragment、明文正式 endpoint 与未批准 origin。
- 以 deployment → Agent descriptor → request 的顺序逐层缩权并冻结 immutable route plan；越权、空交集、能力/catalog/凭据不完整时先失败，再通过既有 `PolicyEngine`/`AuditService` 执行 soft policy/fallback/approval，只有获准后才建立预算 reservation；deny 或 approval-required 未获批准时 reservation/permit/client/mark/network 均为零。Require-approval 复用既有 durable checkpoint/ApprovalRecord/ApprovalGrant/continuation，批准只绕过同一 soft gate并重检 hard route/current balance，不得提高 shared hard limit或重复调用 provider。
- 把 provider interface 和 Pydantic AI adapter 改为真正可取消的异步非流式调用，以 provider-neutral permit 固定 Bulkhead → durable mark → send 顺序；在 total deadline 内只对完成状态明确的响应实施有限 retry，并按全部 attempts 的可信 actual 聚合结算；任一已启用维度 actual 不可得时保留原调用 reservation 并进入 `needs_review`。
- 将新预算树快照显式升级为 `budget-tree-v2`；完整旧 v1 fake 快照只从持久化旧字段投影，部分、混合版本或真实 provider v1 fail closed，不从当前配置补齐历史。
- runtime composition 按受控 deployment 注册真实 provider 的私有 lazy client factory；动态 route hard eligibility 与预算 preflight 通过后才构造 SDK client lease，并把 text、usage、latency、route、attempt、cost 与脱敏 error 归一化到既有 provider-neutral evidence / settlement；业务 Agent 不接触 SDK 对象。
- 保留显式 `fake` deployment 和默认离线 quality/test/eval/smoke；真实 provider smoke 仅在另行授权、显式 opt-in、隔离凭据和受信 endpoint 齐全时执行，任一前置缺失唯一记录为 `hosted-unverified`，条件齐全后遭遇外部阻断才记录 `external-blocked`。
- 在实现前同步公开 module/config/error/evidence 契约与 AC-077 至 AC-084 producer/test 映射；本 change 不新增 HTTP endpoint 或公开 SDK 类型。

## Non-Goals

- 不实现 token/event streaming、`model.output.delta` producer 或新的 SSE/CLI reader；这些属于后继 Phase 18.1 `controlled-model-streaming`。
- 不实现 structured output、structured streaming、reasoning 暴露、模型驱动工具循环或 provider SDK tool call。
- 不实现多 provider 运维控制面、动态 deployment 管理、自动跨 deployment failover 或请求自选 endpoint/credential。
- 不引入 Vault、KMS、通用 `SecretProvider`、依赖升级、破坏性迁移或新的公开 HTTP endpoint。
- 除用户已明确授权的“本 change 契约严格 `1+2` 全员 PASS 后一次 scoped 本地契约提交”外，不执行其他 commit、push、release、deploy、OpenSpec sync/archive 或未经单独授权的真实 provider 请求；该提交不包含红灯测试或生产实现。

## Capabilities

### New Capabilities

- 无。

### Modified Capabilities

- `typed-config`: 增加真实模型 deployment、typed credential reference、endpoint policy、受信 model/input-bound/price catalog、deadline/retry/bulkhead、pricing/capability 配置和 startup fail-closed 语义，同时保留 fake/local 离线合并行为。
- `agent-registry-model-context`: 把 ModelRouter 从请求可直接指定 provider/model 的宽松路由改为 deployment、Agent descriptor 与 request 逐层缩权的 immutable route plan，并让 composition root 受控注册真实非流式 provider。
- `model-usage-evidence`: 增加真实 provider attempt、retry/deadline/cancel/bulkhead 与 side-effect unknown 的 provider-neutral evidence 和结算要求，不扩张 SDK 或 secret 公开面。

## Impact

- 核心代码：`config/{__init__,schemas,settings,secret_files,model_endpoints,model_catalog}.py`、`registry/**`、`models/{__init__,providers,router,invocation,_invocation_settlement,usage,usage_events}.py`、`adapters/models/{fake,pydantic_ai}.py`、`policy/engine.py`、`audit/service.py`、`approvals/{service,_continuation}.py`、`runtime/{services,shared_budget,executor,continuation,_run_continuation}.py`、`storage/_shared_budget_repository_records.py`、`scaffold_templates.py` 与 `_scaffold_support.py`；scaffold 生成后正式 Registry 校验及离线运行回归由 `tests/contracts/test_agent_scaffold_validation_atomicity_contracts.py` 冻结。Vendor 隔离机械门禁同步拥有 `contracts/boundaries.py`、`scripts/import_boundary_check.py` 与 `tests/contracts/test_vendor_boundary_doctor_contracts.py`，允许根固定为仓库相对前缀 `packages/agent-harness/src/agent_harness/adapters/`，不按任意同名路径片段放行。
- 模板与配置：service/local profiles、Agent config、`.env.example`、`templates/service-app/app/runtime.py`、CLI/worker 共用 lifecycle、维护 README，以及固定为 `scripts/smoke_live_model.py` 的 opt-in smoke 入口；对应 `Makefile`、`scripts/{ci_evidence,acceptance_matrix_policy}.py`、`docs/acceptance-matrix.md` 和 GitHub/GitLab producer artifact / required acceptance consumer 均属于同一独占范围。
- 公共 seam：typed config schema、Agent model policy、`ModelRequest`/`ModelRoutePlan`/`ModelResponse`、稳定错误码和 `ModelUsageEvidence.decision`；不新增 HTTP route。
- 数据与依赖：默认不新增 migration，不升级 `pydantic-ai==2.5.0` 或当前 lock 的 `openai==2.44.0`；因生产 adapter 直接构造 `AsyncOpenAI`，把 `openai>=2.44.0,<3` 提升为核心包有界直接依赖，并同步 `packages/agent-harness/pyproject.toml`、`uv.lock` 的直接依赖关系、`compliance/third-party.toml`、依赖范围/许可证合同。lock 的 package `(name,version,source)` identity 必须保持不变；若解析尝试升级或改变来源则停止。若设计证明必须持久化新 identity，则先回到契约拆分迁移方案。
- 生命周期边界：现有 API lifespan、worker 与 eval runner 已统一调用 `RuntimeComponents.close()`；本 change 只在该统一 close seam 和 CLI 直连 composition 路径补 provider lease 关闭。若红灯证明必须修改 repository、`app/main.py` 或 `app/workers/runtime_worker.py`，先更新契约所有权并重新 review，不在实现中临时扩面。
- 验证：先在 `tests/contracts/test_controlled_real_model_{config,routing,runtime_composition,retry_budget,budget_snapshot,offline,policy_approval}_contracts.py`、`tests/contracts/test_agent_scaffold_validation_atomicity_contracts.py`、`tests/contracts/test_vendor_boundary_doctor_contracts.py`、既有 dependency/license contracts 与 `tests/integration/test_controlled_real_model_live_smoke.py` 建立 AC-077 至 AC-084 的失败 regressions，再通过离线 doubles、fake 回归、quality、smoke-local、触及 composition 后的 smoke-service 与 fresh 实现审查收口；真实 smoke 单独报告外部条件。公共 async provider 协议迁移还精确拥有 `design.md` 与 change matrix 所列 18 个既有同步 provider double、同步 Pydantic AI adapter seam、直接 adapter caller 或 `ModelRouter.route()` caller 文件，先将其改为 async/await 并保持原断言语义；未列出的既有测试若被红灯或静态扫描证明必须修改，先更新契约并重新 review。精确文件所有权以 change matrix 与 `design.md` 的冻结映射为准，不得用“相关测试”替代。
