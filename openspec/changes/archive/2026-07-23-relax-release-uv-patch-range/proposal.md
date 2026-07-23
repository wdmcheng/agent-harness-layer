## Source Links

- Product-Spec.md: `REQ-023 依赖兼容范围与可复现解析`、`AC-071`；用户本轮明确把 uv 支持下界调整为 `0.11.29`
- DEV-PLAN.md: `Phase 16: 依赖兼容范围与可复现锁定` 的技术栈、交付内容和验收状态
- Design-Brief.md or design artifact: 不适用，本变更不涉及 UI 或交互
- CONTEXT.md / ADR: 不适用，本变更不改变领域语言或既有架构决策

## Why

当前仓库已让本地 uv 接受 patch 范围，但 release wrapper、manifest consumer 和合同仍把一次可复现执行基线误作唯一兼容版本，导致范围内新 patch 被无条件拒绝。用户确认以包含归档与 build backend 安全加固的 `0.11.29` 为下界，同时允许 `<0.12` 的后续非破坏性 patch，并要求发布证据记录实际执行版本。

## What Changes

- 将根 `[tool.uv].required-version` 与 release wrapper 的支持范围统一为 `>=0.11.29,<0.12`。
- `release` preview、正式 build 和 registry publish 使用范围内实际解析到的 uv，并在 manifest/plan 中记录真实版本，而不是固定写入 `0.11.29`；`no-release` preview 明确写入 `uv_version: null`，且不得仅为填充证据而启动 uv 或 build。
- consumer 校验 uv 版本位于支持范围，并继续通过 manifest/plan checksum 绑定本次实际工具身份；低于下界或达到 `0.12` 时在构建或网络副作用前 fail closed。
- GitHub setup、GitLab OCI image及其 digest 继续选择具体 `0.11.29`，作为当前可复现 CI 环境，不再被描述为唯一兼容 patch。
- 更新 Product Spec、DEV-PLAN、双语维护文档和合同测试，明确区分“支持范围”“CI 具体环境”和“单次证据实际版本”。

## Non-Goals

- 不升级 `uv.lock` 中任何 Python package identity，不运行 `uv lock --upgrade`。
- 不改变 `agent-harness` 精确自依赖、Hatchling backend identity、OCI digest、GitHub Action commit 或 Python Semantic Release 版本策略。
- 不执行真实 promotion、tag、provider release 或 registry publish。
- 不放宽到 uv `0.12` 或更高 minor。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `dependency-version-policy`: 将本地及发布工具的 uv 支持下界统一为 `0.11.29`，发布入口接受同一 minor 的后续 patch。
- `release-dry-run-private-registry`: producer 记录实际 uv 版本，consumer 校验支持范围和冻结证据身份，不再要求固定字符串 `0.11.29`。
- `maintainer-documentation`: 文档区分 uv 支持范围、当前 CI 具体版本和单次发布证据实际版本。

## Impact

- 配置：`pyproject.toml`。
- 发布代码：`scripts/release_contract_support.py`、preview/formal build、manifest consumer 与 registry publish 入口。
- 合同：依赖策略、release build/publish、双版本验证及 CI 配置边界。
- 文档：Product Spec、DEV-PLAN、双语 README/release process 和 acceptance mapping 中的 uv 版本表述。
- 无 API、数据库、migration、运行时服务、UI 或真实外部发布副作用。
