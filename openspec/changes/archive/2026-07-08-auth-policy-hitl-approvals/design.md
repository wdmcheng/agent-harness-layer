## 背景

当前仓库已经有 `IdentityContext` / `PermissionContext`、`RunOrchestrator`、checkpoint/resume、`CanonicalEvent`、local jsonl evidence、repository/UoW、service-app FastAPI app factory、run routes 和 agent registry。缺口是 service API 没有认证 dependency，policy 只有决策词汇没有执行引擎，`ApprovalWaitState` 只是 runtime seam 还没有可持久化 approval service、HTTP/CLI resolve 入口和 audit evidence。

Phase 7 的设计约束是：Access 层只做认证、请求/响应转换和 dependency 注入；Runtime 仍通过 `RunOrchestrator` 推进 run；PolicyEngine 不耦合 API key、OIDC、DBOS、SQLAlchemy session 或工具实现；audit 走 provider-neutral DTO/repository seam 和 CanonicalEvent/local evidence。

## 目标 / 非目标

**目标：**
- 用 API Key / Bearer Token dependency 把受保护 API 统一转换成 `IdentityContext` 和 `PermissionContext`。
- 提供 `PolicyEngine`、YAML provider 和 DB provider interface，固定 allow/deny/require_approval 三态决策。
- 在 run 创建前接入轻量 `InputGuardrail`，把 prompt injection / 越权指令检测结果写入 trace/audit，并交给 policy 决定 allow、deny 或 require_approval。
- 提供 approval service、HTTP routes 和 CLI，使危险动作可以创建 approval、approve/deny 并驱动 checkpoint resume。
- 让 audit log 成为 policy/approval 的证据面，local SQLite 和 service PostgreSQL 都可验证。
- 在实现 route 前先更新 `API-Contract.md` 并用局部 OpenAPI drift tests 锁住契约。

**非目标：**
- 不实现登录、注册、OIDC/OAuth2、RBAC/ABAC 管理 UI 或 SaaS 管理台。
- 不实现真实 FileTool/ShellTool/MCP Client；Phase 7 只提供危险动作的 policy/approval seam 和测试替身。
- 不实现 eval case API；只把写 approved dataset 纳入默认危险动作。
- 不物理拆分 API/worker 进程；只保证 DTO、checkpoint 和 audit seam 为 Phase 13 保留边界。

## 决策

1. **FastAPI 认证 dependency 只产出身份上下文。** `templates/service-app/app/api/dependencies.py` 解析 `Authorization`，验证 API key / bearer token，并返回 `IdentityContext`；权限判断交给 `PolicyEngine`。local/dev 可显式允许默认身份，测试可 override dependency。
   - 替代方案：在每个 route 内手写 token 解析。拒绝，因为会让 mutating route 漏拦截，也不利于 OpenAPI 统一 401/403。

2. **API key 校验放在核心包 auth seam，API 层只适配协议。** `agent_harness.auth` 暴露 token hash、credential DTO 和 verifier interface；默认 local verifier 可用配置中的开发 token 或默认身份策略，后续 DB/verifier 可替换。
   - 替代方案：只在 service-app 实现认证。拒绝，因为 CLI、worker 和未来 gateway 也需要相同身份语义。

3. **PolicyEngine 不直接执行动作。** engine 输入 `PermissionContext`、resource、action、context，输出 `PolicyDecision`。执行方根据 decision 继续执行、返回 denial 或调用 approval service。这样 Phase 8 的工具和 Phase 11 的 eval 可以接入同一 seam。
   - 替代方案：让 policy provider 直接调用 tool/runtime。拒绝，因为会破坏边界并让测试无法证明动作未执行。

4. **默认 policy 来自 YAML，DB provider 先作为 interface / repository seam。** P0 local profile 使用 `templates/service-app/configs/policy/default.yaml`；DB provider 通过 repository 读取 `policy_rules`，但不需要产品化管理 UI。YAML/DB 返回同一 `PolicyRule` DTO。
   - 替代方案：只硬编码默认规则。拒绝，因为 Product-Spec 要求默认清单可配置，并且修改 policy 本身要可审计。

5. **Approval service 关联 checkpoint，不绕过 RunOrchestrator。** `approval.required` 创建 approval 记录、写 audit 和 event，并保存 run/checkpoint/resume token 关联；approve/deny 调用 service 更新 approval 后，再通过 runtime seam resume 或失败/fallback。API route 不直接改 ORM run state。
   - 替代方案：approve route 直接写 `agent_runs.status`。拒绝，因为会绕过 state transition、event seq 和 worker boundary。

6. **Audit 同时支持 repository 和 CanonicalEvent evidence。** `audit_logs` 是结构化查询面，`CanonicalEvent` / local jsonl 是调试与 smoke 证据面；payload 统一走 redaction，避免 token/secret 进入 audit。
   - 替代方案：只写 local jsonl。拒绝，因为 service profile 需要 PostgreSQL 可验证证据，后续 compliance 也要查询 audit。

7. **契约测试先红后绿。** 先扩展 `API-Contract.md` 和 contract tests，覆盖 OpenAPI path/schema、401/403、`ApiErrorEnvelope`、approval conflict、request_id 和 no-side-effect，再实现 route/service。实现完后同一 tests 成为 drift gate。
   - 替代方案：先写 route 再补文档。拒绝，因为本仓库要求 endpoint 开工前先补 API contract。

8. **InputGuardrail 是 Phase 7 的轻量策略入口，不做完整安全平台。** guardrail 在 run create 的 API/CLI 输入进入 `RunOrchestrator` 前执行，输出 `GuardrailDecision` / `PolicyDecision` 兼容 payload，记录 injection summary、trust marker 和 audit metadata；它不读取外部工具、retrieval 或 MCP output，那些属于 Phase 8/9 的输入边界。
   - 替代方案：把 prompt injection 检查推迟到工具或 retrieval 阶段。拒绝，因为 Product-Spec 的 AC-024 明确要求创建 run 时就记录并按策略处理明显 prompt injection 或越权指令。

9. **AGT-001 在 Phase 7 只做认证和可见性过滤，不改变 descriptor 形状。** `GET /api/v1/agents` 接入同一 identity dependency 和 policy visibility check；响应仍使用 Phase 6 的 public descriptor DTO，不新增私有字段。
   - 替代方案：为 agent 管理新增管理 API。拒绝，因为 P0 不做 SaaS 管理后台，Phase 7 只补已实现 list endpoint 的认证/可见性。

## 影响表面

- 核心包：`agent_harness.auth`、`agent_harness.policy`、`agent_harness.approvals`、`agent_harness.audit`、`agent_harness.runtime` approval wait/resume seam、`agent_harness.storage` models/repositories/migration。
- service-app：`app/api/dependencies.py`、`app/api/routes/agents.py`、`app/api/routes/runs.py`、`app/api/routes/approvals.py`、`app/api/routes/policies.py`、`app/main.py` router/dependency/error handler、`app/runtime.py` 组件构造、CLI approval/policy 子命令。
- API 契约：`API-Contract.md` 的 `APR-001`、`APR-002`、`POL-001`，以及 `AGT-001` / run 现有 route 的认证增强说明。
- 配置：profile auth/policy 字段、`templates/service-app/configs/policy/default.yaml`、`.env.example` 的开发 token 说明。
- 数据：新增或扩展 `api_keys`、`policy_rules`、`approvals`、`audit_logs` repository seam 和 Alembic migration；local SQLite 与 service PostgreSQL 都要跑 migration/smoke。
- 测试：contract tests、API/OpenAPI drift tests、CLI tests、storage repository tests、smoke-local、smoke-service、import boundary。

## 测试接缝

- API seam：FastAPI `create_app(...)` 后调用 `GET /api/v1/agents`、`/api/v1/agents/{agent_id}/runs`、`/api/v1/runs/{run_id}/approvals`、`/api/v1/runs/{run_id}/approvals/{approval_id}`、`/api/v1/policies/check`，断言 401/403/409/200、`ApiErrorEnvelope`、request_id、tenant/identity visibility 和 OpenAPI schema。
- CLI seam：`uv run agent-harness approvals list/approve/deny` 和 `uv run agent-harness policy check` 使用同一 service/core seam，输出稳定 JSON 或人可读摘要。
- Module seam：`ApiKeyVerifier`、`InputGuardrail.check()`、`PolicyEngine.evaluate()`、YAML provider、DB provider interface、`ApprovalService.require/approve/deny()`、`AuditService.record()`。
- Persistence seam：SQLite/PostgreSQL repository contract 覆盖 `api_keys`、`policy_rules`、`approvals`、`audit_logs` 的创建、查询、状态冲突和事务回滚。
- Runtime seam：approval required 后 run waiting，approve 后通过 checkpoint/resume 继续，deny 后 failed/fallback；错误 resume token 或跨 run approval 不推进状态。
- Static seam：import boundary 证明 service route、业务 agent 和 runtime core 不直接依赖 vendor SDK、DBOS API 或 SQLAlchemy session。

## 风险 / 取舍

- [Risk] Phase 7 依赖 Phase 8/11 的真实危险动作实现。→ Mitigation：用 policy/approval 测试替身覆盖 shell、delete、approved dataset 等动作类别，真实 tool/eval 后续接同一 seam。
- [Risk] 把 auth 默认身份做得太宽会弱化 401 测试。→ Mitigation：local/dev 明确区分 allow-default-identity 与 required token；contract tests 同时覆盖 invalid token no-side-effect。
- [Risk] approval resume 容易直接写状态绕过 event seq。→ Mitigation：ApprovalService 只调用 runtime/checkpoint seam，contract tests 验证 event seq 和 terminal/waiting transition。
- [Risk] audit payload 泄漏 token。→ Mitigation：复用 secret redaction，新增 secret-like audit tests。
- [Risk] 新增 migration 影响 service smoke 时长。→ Mitigation：沿用 Phase 3/6 migration pattern，local SQLite 与 service PostgreSQL 分开证明。

## 迁移计划

1. 扩展 `API-Contract.md` 和 contract tests，先锁定 endpoint 与 error envelope。
2. 新增 OpenSpec strict validate 和 artifact review，通过后进入实现。
3. 新增 migration / models / repositories，再实现 auth、policy、approval、audit core modules。
4. 接入 FastAPI dependencies/routes、CLI 和 runtime component 构造。
5. 运行局部 contract tests、`make quality`、`make test`、`make smoke-local`、`make smoke-service`、`make build`、`make license-check` 和 pre-commit。
6. 同步主规格、归档 change、更新 DEV-PLAN 并提交。回滚时删除新增 routes/modules/tests/migration，并恢复 API 契约到 Phase 6 状态。

## 开放问题

- 无阻塞问题。真实密钥轮换、OIDC/OAuth2、产品化 policy 管理 UI、真实工具动作和 eval API 均属于后续 Phase。
