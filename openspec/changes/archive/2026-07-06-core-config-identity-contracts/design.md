## 背景

Phase 1 之后，`agent_harness` 只是可安装但基本空的包。Phase 2 要补上后续 runtime、storage、model、tool、retrieval、observability 和 eval 都要复用的公共契约。架构图把 auth/tenant/session、input guardrails、request/response schemas、context assembly、budget policy 和 P0 deployment boundary 放在更深层 runtime/provider 工作之前，这决定了本 change 先做 value contracts 和验证 seam，不做真实 provider 或持久化。

2026-07-06 已核验的外部资料：
- Pydantic docs 当前 stable 文档为 `v2.13.4`，示例使用 `BaseModel`、`model_dump()` 和 validation errors：https://pydantic.dev/docs/validation/latest/get-started/
- Pydantic Settings docs 说明通过 `pydantic-settings` 管理 settings：https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/
- PyPI 当前 `pydantic-settings 2.14.2`，发布日期 2026-06-19：https://pypi.org/project/pydantic-settings/
- PyPI 当前 `typer 0.26.8`，发布日期 2026-06-26：https://pypi.org/project/typer/
- PyPI 当前 `PyYAML 6.0.3`，发布日期 2025-09-25：https://pypi.org/project/PyYAML/
- PyYAML 文档要求用 `yaml.safe_load` 安全解析简单 YAML 数据：https://pyyaml.org/wiki/PyYAMLDocumentation

## 目标 / 非目标

**目标：**
- 增加稳定、vendor-neutral 的 Pydantic DTO、error、trust、identity、config contracts。
- 支持 profile YAML、可选 agent YAML、`.env` 和 environment overrides 的 typed config loading。
- 提供最小 `agent-harness doctor` CLI seam，证明 profile diagnostics 可通过安装包命令执行。
- 强化 import boundary checks，防止后续业务代码绕过 adapter seam。

**非目标：**
- 不实现 Phase 3 storage / migration / repository。
- 不实现真实 Pydantic AI、DBOS、Logfire、Phoenix、Langfuse、model、tool、retrieval 或 observability adapter 行为。
- 不实现真实 API authentication backend、database-backed policy provider、HITL persistence 或 checkpoint / resume。
- 不自动 OpenSpec archive。

## 决策

1. 公共 DTO 和 settings schemas 使用 Pydantic v2，并在未知字段应失败的公共 contract 上使用 `ConfigDict(extra="forbid")`。
   - 理由：Product-Spec 把 Pydantic schema 定为跨边界契约；当前文档确认 v2 是稳定线。
   - 备选方案：dataclasses + 手写校验。拒绝原因是会重复实现 validation 和 error path 处理。

2. YAML 使用 `PyYAML.safe_load` 解析，再交给 Pydantic model 校验。
   - 理由：profile 和 agent config 虽然是本地文件，但仍是配置输入；safe parsing 避免任意 YAML object construction。
   - 备选方案：完全依赖 `pydantic-settings[yaml]`。暂不采用，因为本阶段需要明确 merge order 和 remediation hints。

3. Settings merge order 定为 profile YAML -> agent YAML -> `.env` / environment overrides -> explicit overrides。
   - 理由：profile defaults 应该可读，local env 负责覆盖部署敏感值；tests 可以用 explicit overrides 避免污染 process-global state。
   - 备选方案：只用原生 `BaseSettings`。拒绝原因是 agent YAML 不是常规 env source，且需要确定性 merge tests。

4. Trust、context refs、identity、permission、policy / guardrail decisions 只做 value contracts。
   - 理由：Phase 2 定义后续阶段的公开词汇，不实现 storage、runtime 或 approval persistence。
   - 备选方案：现在实现完整 `PolicyEngine`。拒绝原因是这属于 Phase 7；本 change 只需要 decision DTO 和 defaults。

5. CLI entrypoint 放在 `agent_harness.cli`，用 Typer 实现；`doctor` 保持无副作用。
   - 理由：DEV-PLAN 已指定 Typer。无副作用 doctor 可以在 smoke tests 中运行，不需要 database 或 provider keys。
   - 备选方案：只做脚本级 doctor。拒绝原因是验收标准明确要求 `agent-harness doctor --profile local`。

## 受影响表面

- `packages/agent-harness/pyproject.toml`：新增 runtime dependencies 和 console script。
- `packages/agent-harness/src/agent_harness/config/`：settings loader 和 schemas。
- `packages/agent-harness/src/agent_harness/contracts/`：DTO base、errors、trust/context/policy contracts、boundary declarations。
- `packages/agent-harness/src/agent_harness/identity/`：identity 和 permission context。
- `packages/agent-harness/src/agent_harness/cli/`：doctor command。
- `templates/service-app/configs/profiles/`：local 和 service profiles。
- `templates/service-app/.env.example`：env override keys。
- `scripts/import_boundary_check.py`、smoke tests：公开验证门禁。
- README / DEV-PLAN：只补必要状态和证据。

## 测试 seam

- Module interface seam：contract tests import 公共 `agent_harness.config`、`agent_harness.contracts`、`agent_harness.identity`。
- Config seam：tests 加载临时 profile / agent YAML / env file，断言 typed merge 和 field-path diagnostics。
- CLI seam：tests 和 smoke 运行 `agent-harness doctor --profile local`，必要时用 `python -m agent_harness.cli doctor --profile local` 验证模块入口。
- Static analysis seam：`scripts/import_boundary_check.py` 扫描配置的 code surfaces，并报告 banned vendor imports。
- Build seam：`uv build --package agent-harness` 证明新 modules 和 entrypoint metadata 被打包。

## 风险 / 取舍

- [Risk] YAML merge precedence 可能让用户误解。-> Mitigation：用 tests 和 README 固化 precedence。
- [Risk] `pydantic-settings` 当前 PyPI 版本比 DEV-PLAN 技术栈表更新。-> Mitigation：以 `uv.lock` 解析结果为准，记录资料核验，不把本 change 扩成全栈升级。
- [Risk] `agent-harness doctor` 可能只有 workspace install 后可调用。-> Mitigation：同时支持 `python -m agent_harness.cli` 作为 tests seam，并打包 console script。
- [Risk] Import boundary scan 可能误伤未来 adapter 文件。-> Mitigation：把 approved adapter / integration path logic 集中在 `contracts.boundaries`。

## 迁移计划

这是 additive Phase 2 change。现有 Phase 1 commands 应继续通过。无需数据迁移。回滚方式是删除新增 package modules、回滚 package dependencies / entrypoint，并在 archive 前删除本 OpenSpec change。

## 待确认问题

- 无阻塞问题。数据库策略 provider、audit persistence、service profile API/worker smoke 留给后续 Phase。
