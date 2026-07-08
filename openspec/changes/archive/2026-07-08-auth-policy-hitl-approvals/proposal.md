## Source Links

- Product-Spec.md：`SCOPE-011` API Key / Bearer Token 认证、`SCOPE-012` PolicyEngine 与权限拦截内核、`SCOPE-013` HITL 审批协议和 CLI/HTTP 入口、`FLOW-003` 危险动作审批与恢复、`REQ-009` 租户/身份/认证、`REQ-010` PolicyEngine/权限拦截/HITL。
- DEV-PLAN.md：`Phase 7: 认证、PolicyEngine 与 HITL 审批`；覆盖矩阵中的 `REQ-008`、`REQ-009`、`REQ-010`。
- API-Contract.md：`4.2` 认证与身份、`4.5` 错误响应格式、`RUN-001` 到 `RUN-005` 的认证增强说明、`AGT-001` 的 Phase 7 认证/可见性过滤说明、保留 API 索引中的 `APR-001`、`APR-002`、`POL-001`。
- 设计稿 / 架构图：`docs/architecture/pydantic-ai-agent-architecture.drawio` 的 Access / Runtime / HITL 回路、信任边界和 audit/observability 边界；无产品化前端 UI。
- CONTEXT.md / ADR：当前仓库无相关领域上下文或 ADR。

## Why

Phase 6 已经把 agent registry、模型路由、上下文组装和 embedding cache 建成公共边界，但 service API 仍处在无认证、无策略决策、无审批闭环状态。Phase 7 需要把身份注入、PolicyEngine、approval/resume/audit 固定成可测试契约，否则后续工具、MCP、retrieval 和 eval 都会绕过权限与审计边界。

## What Changes

- 扩展 `API-Contract.md`，把 `APR-001`、`APR-002`、`POL-001` 从保留索引扩展为完整 endpoint 条目，并补充 mutating P0 API 的 401/403、`ApiErrorEnvelope`、request_id 和 OpenAPI drift 验收。
- 新增 API Key / Bearer Token 认证能力：解析 `Authorization: Bearer <token>` 或等价 API key，注入 `IdentityContext`；local/dev 未启用多租户时使用 `tenant_id="default"`、`user_id="local-user"`。
- 新增 `PolicyEngine`、YAML provider、DB provider interface、默认危险动作策略和 policy check API/CLI seam；decision 使用公开 `allow` / `deny` / `require_approval` 值。
- 新增轻量 `InputGuardrail` 接入：用户/API/CLI 输入进入 run 前执行 prompt injection / 越权指令检测，写入 trace/audit，并按 policy 决策 allow、deny 或 require_approval。
- 新增 HITL approval service、HTTP routes 和 CLI：approval required、list/read、approve、deny，并与 checkpoint/resume seam 关联。
- 新增 audit service：记录 policy decision、approval required/resolved、审批人、动作、结果、tenant/agent/run/trace/request 关联字段，且不泄漏 secret。
- 扩展 runtime/API 行为：无效 token 调用受保护 P0 API 必须返回认证错误且不创建 run；`GET /api/v1/agents` 按 tenant/identity 可见性过滤并覆盖 401/403；approval approve 后可恢复等待中的 run；deny 后按策略失败或 fallback。

## Non-Goals

- 不做用户注册、登录页、组织邀请、计费、OIDC/OAuth2 或 SaaS 管理后台。
- 不实现 Phase 8 的 ToolRegistry、FileTool、ShellTool 或 MCP Client；本 change 只定义这些危险动作进入 policy/approval 的决策 seam 和测试替身。
- 不实现 Phase 11 的 eval case 管理 API；仅把“写 approved eval dataset”列入默认危险动作策略。
- 不实现复杂 RBAC/ABAC 管理 UI；P0 只提供 policy engine、provider interface、默认规则和 API/CLI 检查入口。
- 不把 service profile 物理拆成 API/worker 多进程；Phase 13 再处理部署形态，本 change 只保持跨边界 DTO 和 resume/audit seam。

## Capabilities

### New Capabilities

- `auth-policy-hitl-approvals`：API Key / Bearer Token 认证、PolicyEngine、默认危险动作策略、HITL approval、checkpoint resume 关联、audit log、API/CLI 和 OpenAPI drift 契约。

### Modified Capabilities

- 无。本 change 通过新增 Phase 7 能力消费 `identity-context`、`runtime-checkpoint-runs`、`canonical-events-artifacts`、`storage-migration-uow` 和 `agent-registry-model-context` 的既有契约，不修改它们的已归档要求。

## Impact

- 受影响文档：`API-Contract.md`、`DEV-PLAN.md`、OpenSpec change 和归档后的主规格。
- 受影响代码：`packages/agent-harness/src/agent_harness/auth/**`、`policy/**`、`approvals/**`、`audit/**`、`agent_harness/cli.py` 或 CLI 子模块、runtime approval wait/resume seam、service-app API dependencies/routes、模板 policy config。
- 受影响 API：`GET /api/v1/agents`、`/api/v1/agents/{agent_id}/runs`、`/api/v1/runs/{run_id}`、`/api/v1/runs/{run_id}/events`、`/api/v1/runs/{run_id}/cancel`、`/api/v1/runs/{run_id}/resume` 的认证/可见性增强，以及新增 `/api/v1/runs/{run_id}/approvals`、`/api/v1/runs/{run_id}/approvals/{approval_id}`、`/api/v1/policies/check`。
- 受影响数据：`api_keys`、`policy_rules`、`approvals`、`audit_logs` 表或 repository seam；local SQLite 和 service PostgreSQL 都必须有证据。
- 受影响测试：auth/policy/approval/audit contract tests、API/OpenAPI drift tests、approval CLI/API 行为测试、runtime resume/audit tests、smoke-local 和 smoke-service。
