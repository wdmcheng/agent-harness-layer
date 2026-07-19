# ADR-0002：Vendor SDK 隔离在 Adapter/Integration 边界

- 状态：Accepted
- 日期：2026-07-20
- 关联：[ADR-0001](0001-p0-service-boundaries.md) · [Adapter 合同](../adapter-contracts.md) · [扩展指南](../extension-guide.md)

## 背景

Agent runtime 同时接触 model、embedding、MCP、durable execution 和 observability provider。若业务 agent、template app 或核心 DTO 直接依赖 Pydantic AI、DBOS、Logfire、Phoenix、Langfuse 等 SDK，上游升级会穿透整个仓库，跨进程消息也会夹带不可序列化或带 secret 的厂商对象。未来物理拆分 tool/model gateway 和 event pipeline 时，这种耦合会成为迁移阻力。

## 决策

1. 核心调用方只依赖 provider-neutral Pydantic DTO、Protocol、facade、repository/UoW 和 `CanonicalEvent`。
2. Vendor SDK import 只允许出现在 `packages/agent-harness/src/agent_harness/adapters/` 或经 `contracts/boundaries.py` 明确批准的受控 integration path。
3. `agents/*`、`templates/service-app/app/*`、eval runner 和核心业务 service 不直接 import vendor SDK；composition root 负责把具体 adapter 注入公开 seam。
4. Adapter 将 SDK request/response/exception 转换为公共 DTO、封闭错误和脱敏 evidence。SDK object、raw response、credential、本机绝对路径和不可控 payload 不越过边界。
5. local/fake adapter 保持可用，使核心合同、eval 和 smoke 不依赖真实 model key 或 SaaS provider。
6. 新增 vendor 必须先补 dependency pin/extra、adapter contract、import boundary 规则、redaction/degradation 测试和 release/license 复核；不能只加一个直接 import。

## 替代方案

- 业务代码直接调用 SDK：样板少，但升级、测试、redaction、policy 和未来拆分成本扩散，拒绝。
- 建立一套“万能 vendor DTO”：表面统一，实际把各 SDK 私有概念提升成核心合同，拒绝。
- P0 立即把每个 provider 物理拆成服务：超出当前范围且在合同稳定前增加网络/部署复杂度，拒绝；先保持逻辑隔离。

## 后果

- 新 provider 需要显式 adapter 和合同测试，前期代码更多，但调用方和跨进程 DTO 保持稳定。
- Provider 特性只有在能映射到公共合同或经受控 capability seam 引入时才能使用，不能无边界透传。
- Provider failure 可以按合同降级；local durable evidence failure 仍是主失败，不能伪装成 provider degradation。
- 未来拆分 model/tool gateway 与 event pipeline 时，可沿现有 seam 搬移 adapter，而不重写业务 agent。

## 证据

```bash
make quality
uv run python scripts/import_boundary_check.py
make test
```

实现位置包括 `adapters/models/`、`adapters/mcp/`、`adapters/runtime/`、`adapters/observability/`；合同证据包括 `tests/contracts/test_agent_registry_router_model_contracts.py`、`tests/contracts/test_observability_provider_adapters_contracts.py` 和 `tests/contracts/test_tool_registry_public_seam_contracts.py`。

## 复审触发条件

- 新 vendor SDK 或 capability library 进入依赖。
- 现有 SDK 发生破坏性版本升级、许可证变化或无法再映射公共 DTO。
- tool/model gateway 或 event pipeline 开始物理拆分。
- import boundary 出现例外需求，或 provider raw object 必须跨进程。

触发后先形成新的行为/架构变更契约；不得直接放宽 import checker。
