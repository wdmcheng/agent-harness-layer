## 1. 先写公共契约测试

- [x] 1.1 新增失败的 public contract tests，覆盖 DTO serialization、error envelope、trust/source/context ref、guardrail decision、identity/permission context、config merge diagnostics、import boundary scanning 和 doctor local profile output。

## 2. 核心契约与身份上下文

- [x] 2.1 实现 `agent_harness.contracts` 公共 DTO、error、trust/context、policy/guardrail 和 boundary declarations，使 contract tests 通过。
- [x] 2.2 实现 `agent_harness.identity` identity / permission context models，覆盖默认 local tenant/user/session 行为。

## 3. 类型化配置

- [x] 3.1 新增 runtime dependencies，并实现 `agent_harness.config` schemas / settings loader，支持 profile YAML、可选 agent YAML、`.env`、environment overrides、explicit overrides 和 structured diagnostics。
- [x] 3.2 更新 service-app `local.yaml`，新增 `service.yaml`，并更新 `.env.example`，保证 local/service profiles 都能通过 typed loader 校验。

## 4. Doctor 与 Boundary Gates

- [x] 4.1 新增 `agent-harness doctor --profile local` CLI seam，并更新 smoke validation，使它在无 provider keys 时可运行。
- [x] 4.2 更新 import boundary scanning，使用声明式 banned vendor list 和 approved adapter / integration paths。

## 5. 文档与验证

- [x] 5.1 更新 README 和 DEV-PLAN Phase 2 状态 / 证据，记录新增公共契约、doctor command、deployment boundary note，以及仍属于后续 Phase 的范围。
- [x] 5.2 运行 `openspec validate core-config-identity-contracts --type change --strict`、`openspec validate --all --strict`、`make quality`、`make test`、`make smoke-local`、`make build`、`make license-check`、`uv run pre-commit run --all-files`，并记录结果供 review 使用。
