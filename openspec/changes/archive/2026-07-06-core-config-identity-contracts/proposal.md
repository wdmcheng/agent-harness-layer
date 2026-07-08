## Source Links

- Product-Spec.md：`REQ-002` 核心包与上游隔离；`REQ-004` 配置系统；`REQ-009` 租户、身份与认证；`REQ-010` PolicyEngine、权限拦截、InputGuardrail 与 HITL；`REQ-012` 模型、预算、上下文组装与 embedding；`REQ-022` 部署边界与未来微服务拆分基础。
- DEV-PLAN.md：`Phase 2: 核心契约、配置系统与身份上下文`；风险表中 Pydantic AI vendor 漂移、未来拆分边界、prompt/tool output injection 三项。
- 设计稿 / 架构图：`docs/architecture/pydantic-ai-agent-architecture.drawio`，重点对应 Access 层的 Auth/Tenant/Session、Input Guardrails、Request & Response Schema，Engine 层的 Context Assembly / Budget Policy，Tools 层信任边界，以及 P0 部署边界。
- CONTEXT.md / ADR：当前仓库无。

## Why

Phase 1 只建立了 workspace 和可安装包壳。Phase 2 必须先把后续 runtime、storage、policy、model、tool、retrieval、observability、eval 都要复用的公共契约立住；否则后续阶段会直接耦合 vendor SDK、散落读取配置文件，并把身份、信任标记和错误模型补成一堆不兼容的局部实现。

## What Changes

- 新增 `agent_harness` 公共 DTO、错误模型、trust/source/context ref、guardrail / policy decision 契约。
- 新增 typed settings schema 和 loader，合并 `.env`、profile YAML、agent config YAML，并输出字段路径和修复建议。
- 新增 identity / permission context model，覆盖默认单用户 tenant、本地 session、roles、permissions 和 auth method。
- 扩展 import boundary 扫描，阻止 template app、示例 agent 和 examples 直接 import Pydantic AI、DBOS、Logfire、Phoenix、Langfuse 等 vendor SDK。
- 新增 `agent-harness doctor --profile local` 公开 CLI seam，用来证明 profile 可加载且不需要真实 provider key。
- 补齐 local / service profile 样例和 public contract tests。

## Non-Goals

- 不实现 Phase 3 storage、migration、repository / Unit of Work、PostgreSQL 或 Redis service smoke。
- 不实现完整 runtime、checkpoint / resume、模型 provider 调用、tool execution、retrieval、observability adapter 或 eval gate。
- 不实现真实认证后端、OIDC/OAuth2、数据库策略 provider、approval 持久化或 HITL resume flow。
- 不自动 archive 本 OpenSpec change。

## Capabilities

### New Capabilities

- `core-contracts`：公共 DTO base、error envelope、trust marker、source/context ref、context payload、guardrail / policy decision value object。
- `typed-config`：profile / agent settings schema，以及 `.env`、profile YAML、agent config YAML 的确定性加载和校验。
- `identity-context`：tenant/user/session identity 和 permission context，包含默认 local identity 注入语义。
- `vendor-boundary-doctor`：vendor SDK import boundary 声明、静态扫描，以及 profile diagnostics 的 doctor CLI seam。

### Modified Capabilities

- 无。

## Impact

- 受影响代码：`packages/agent-harness/src/agent_harness/**`、`scripts/import_boundary_check.py`、根目录 smoke / quality 脚本、`templates/service-app/configs/**`。
- 受影响包元数据：`packages/agent-harness/pyproject.toml`，以及新增依赖后可能更新的 `uv.lock`。
- 受影响文档：根 `README.md` 和 `DEV-PLAN.md` 的 Phase 2 状态与证据区。
- 受影响测试：`tests/contracts/` 下新增 public seam contract tests；CLI/smoke 验证通过公开命令执行。
- 受影响数据 / UI：无数据库 schema、migration 或产品 UI 变更。
