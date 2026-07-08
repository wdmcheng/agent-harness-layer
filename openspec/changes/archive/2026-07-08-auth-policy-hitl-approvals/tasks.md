## 1. API 契约与红灯测试

- [x] 1.1 扩展 `API-Contract.md`，把 `APR-001`、`APR-002`、`POL-001` 写成完整 endpoint 条目，并同步 `AGT-001` 与现有 run route 的认证、tenant/identity 可见性、401/403、request_id 和安全规则说明。
- [x] 1.2 新增 Phase 7 API/OpenAPI drift contract tests，覆盖 `GET /api/v1/agents`、`/api/v1/runs/{run_id}/approvals`、`/api/v1/runs/{run_id}/approvals/{approval_id}`、`/api/v1/policies/check`、`ApiErrorEnvelope`、401/403、409 approval conflict 和 request_id；实现前这些 tests 应证明当前缺口存在。
- [x] 1.3 新增认证 no-side-effect tests，覆盖无效 Bearer Token 调用受保护 agents list/run create 时不创建 run、checkpoint、event、approval 或 audit side effect。

## 2. 存储、配置与核心 DTO

- [x] 2.1 新增 Alembic revision、SQLAlchemy models 和 repository seam，覆盖 `api_keys`、`policy_rules`、`approvals`、`audit_logs`，并提供 SQLite/PostgreSQL 一致的 contract tests。
- [x] 2.2 扩展 profile/config schema、`.env.example` 和 `templates/service-app/configs/policy/default.yaml`，支持 local/dev 默认身份、API key / bearer token、policy YAML 路径和默认危险动作清单。
- [x] 2.3 实现核心 DTO 和错误类型：auth credential/verifier、`PolicyDecision`、`PolicyRule`、approval status/request/response、audit record；DTO 必须可 JSON 序列化且拒绝未声明字段。

## 3. Auth 与 PolicyEngine

- [x] 3.1 实现 `agent_harness.auth` API Key / Bearer Token verifier，包含 token hash/secret redaction、local default identity 和无效 token 结构化错误。
- [x] 3.2 实现 `InputGuardrail`，在 API/CLI run create 进入 runtime 前检测 prompt injection / 越权指令，写入 trust marker、trace/audit metadata，并按 policy 返回 allow、deny 或 require_approval。
- [x] 3.3 实现 `PolicyEngine`、YAML provider、DB provider interface 和默认危险动作策略，覆盖 allow、deny、require_approval 三态决策及 matched rule/audit metadata。
- [x] 3.4 新增 `POST /api/v1/policies/check` route 和 `agent-harness policy check` CLI，二者通过同一 PolicyEngine seam，contract tests 覆盖三态决策和 401/403。

## 4. Approval、Runtime Resume 与 Audit

- [x] 4.1 实现 `ApprovalService` 的 require/list/read/approve/deny，状态机防止重复 resolve、跨 run resolve 和错误 resume token 推进其他 run。
- [x] 4.2 将 approval required 接入 runtime checkpoint/resume seam：危险动作产生 `approval.required` event 和 waiting checkpoint，approve 后 resume 原 run，deny 后按策略 failed 或 fallback。
- [x] 4.3 实现 `AuditService`，记录 policy decision、approval required/resolved、审批人、动作、结果、tenant/user/session/agent/run/trace/request 关联字段，并测试 secret 不进入 audit payload。
- [x] 4.4 新增 approval HTTP routes 和 `agent-harness approvals list/approve/deny` CLI；contract tests 覆盖 list/read、approve、deny、409 conflict、audit evidence 和 request_id。
- [x] 4.5 将 auth dependency 和 policy visibility check 接入 `GET /api/v1/agents`，保持 Phase 6 public descriptor DTO，不暴露本地绝对路径、secret、callable 或 provider client。

## 5. 验证、审查与 Phase 收口

- [x] 5.1 运行 Phase 7 局部 tests：auth/policy/approval/audit contract tests、API/OpenAPI drift tests、approval CLI/API 行为测试。
- [x] 5.2 运行完整门禁：`uv sync`、`make quality`、`make test`、`make smoke-local`、`make smoke-service`、`make build`、`make license-check`、`uv run pre-commit run --all-files`。
- [x] 5.3 派 code-reviewer 完成 Stage 1/2 review；如有任何实质性后续 diff，重跑相关验证并重新 review，通过后再写 `.agents/.needs-review=clean`。
- [x] 5.4 同步 delta spec 到 `openspec/specs/`，运行 `openspec validate --all --strict`，归档 `auth-policy-hitl-approvals`，确认 `openspec list --json` 为 `changes: []`。
- [x] 5.5 更新 `DEV-PLAN.md` Phase 7 状态、验证证据、剩余工作和下一步，最后创建本地 commit，不 push。
