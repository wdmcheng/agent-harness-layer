## Source Links

- Product-Spec.md: REQ-016 的人工 acceptance gate、approved-only 基础链路与数据保护规则。
- DEV-PLAN.md: Phase 12.5 的 EVL-004 API/CLI、人工 reviewer、policy decision、audit log 与 OpenAPI drift 验收项。
- API-Contract.md: EVL-004 create/read/comparison/accept endpoint、HTTPBearer、幂等、错误 envelope 和 side-effect 约束。
- docs/eval-observability-loop.md: Acceptance Gate 判据与“不得自动改写 harness 输入”边界。
- Design-Brief.md or design artifact: 不涉及 UI，无设计稿依赖。
- CONTEXT.md / ADR: 当前仓库没有适用于本变更的领域上下文或 ADR。

## Why

实验和 comparison evidence 只有通过受认证、租户隔离、policy 控制且可审计的公共入口，才能进入维护者工作流。人工接受必须把判据、reviewer、reason、policy decision 和 audit 绑定到唯一记录，而不是把总分上涨直接当成生产变更。

## What Changes

- 实现 EVL-004 的 experiment create/read/comparison/accept HTTP API 与等价 `agent-harness eval experiment ...` CLI。
- create/read/accept 强制 HTTPBearer、tenant visibility 和权限检查；未认证或未授权请求不得创建 experiment、eval run、acceptance 或 audit side effect。
- create 支持 `Idempotency-Key` 相同 key+body 返回同一 experiment；accept 重试返回同一 acceptance record，不重复 audit。
- accept 只有在 comparison evidence 完整、目标行为改善、holdout 无不可接受退化、关键 regression 通过、请求 version 与已比较 candidate 一致、人工 reviewer 明确决定且 policy 允许时，才写 accepted production binding；rejected 使用同一不可变 decision seam 但不产生 production binding。
- 对标签、holdout、candidate、comparison evidence、状态冲突和 provider degradation 返回稳定 DTO、`ApiErrorEnvelope` 或 degraded summary；OpenAPI 覆盖安全与 401/403/409/422/500。

## Non-Goals

- 不自动修改 prompt、tool description、agent/retrieval/policy config 或生产配置。
- 不实现发布 gate、自动 rollout 或 UI 管理台。
- 不把 draft case 纳入 split/experiment，不放宽 Phase 11 的人工 approve 语义。
- 不涉及 Phase 13 API/worker 分进程或 Phase 15 release automation。

## Capabilities

### New Capabilities

- `eval-experiment-api-acceptance`: 定义 EVL-004 API/CLI、身份与权限、幂等、人工接受、audit 和错误语义。

### Modified Capabilities

- 无。

## Impact

- Service App：扩展既有 `app/api/routes/evals.py` 和唯一 app factory composition，不新增第二套路由层。
- CLI：扩展核心 `agent-harness eval experiment create/compare/accept`，复用同一 service/DTO 语义。
- Policy/Audit：增加 experiment 读写与 harness accept action，复用现有 IdentityContext、PolicyEngine 和 AuditLog seam。
- OpenAPI：增加 EVL-004 schemas、HTTPBearer security 与标准错误响应的 drift tests。
- 存储/核心包：新增独立 `evals/acceptance.py` 编排 acceptance，消费前两个变更的 split、experiment、comparison 和 decision persistence seam，不回改 experiment 算法或直接操作 ORM。
