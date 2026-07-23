## Source Links

- Product-Spec.md: REQ-001、REQ-019、REQ-020、REQ-023 与 AC-069/070/071/072
- DEV-PLAN.md: Phase 16“依赖兼容范围与可复现锁定”
- Design-Brief.md or design artifact: 不涉及 UI 或交互设计
- CONTEXT.md / ADR: 无适用领域上下文或既有 ADR；Redis server、容器 digest 等不可变运行时输入不在本次放宽范围

## Why

当前三份 `pyproject.toml` 把多数外部 Python 依赖和本地 uv 都写成 exact pin，混淆了“项目支持范围”和“当前可复现解析”；旧 promotion 又让根 exact、模板通配，无法保证同仓库版本身份一致。用户要求在不自动升级、不削弱 CI/发布证据的前提下放宽所有可兼容的外部声明，同时让根与模板的 `agent-harness` 自依赖精确匹配当前项目版本。

## What Changes

- 为外部 runtime、optional、dev、license、release 与 build-system 依赖声明已验证下界和兼容上界；稳定版本默认约束到下一主版本，`0.x` 默认约束到下一次版本，已知上游组合约束保留更窄上界。根与模板对同仓库 `agent-harness` 保持当前项目版本的精确自依赖。
- 将根 `[tool.uv].required-version` 改为 `>=0.11.19,<0.12`；GitHub、GitLab、release wrapper、OCI digest 与发布证据仍精确固定 `0.11.29`。
- 保持 `uv.lock` 的精确版本和 source identity，不使用 `--upgrade`；只刷新与声明范围相关的 lock metadata。
- build-system metadata 向消费者声明兼容范围，但仓库 preview 与正式 tag build 先 frozen sync，再关闭默认 build isolation 使用 lock 内精确 Hatchling，并在 manifest 记录、核对 backend identity。
- 修改 release promotion，使根 workspace 与模板在升版后都生成 `agent-harness==<new-version>`，不再产生范围或通配 pin。
- 增加依赖策略、旧版 uv 兼容、promotion 和 lock identity 合同，并同步双语 README、release 文档和 acceptance matrix。

## Non-Goals

- 不升级、降级或重新选择任何已锁 Python package。
- 不放宽 CI action/image、release wrapper、容器 image digest、license snapshot 等不可变证据边界。
- 不修改运行时 API、数据库 schema、HTTP/CLI 行为、部署拓扑或 provider 集成。
- 不执行 commit、push、tag、release、真实 registry publish、部署或 OpenSpec archive。

## Capabilities

### New Capabilities

- `dependency-version-policy`: 定义兼容声明、精确 lock、显式升级和本地/CI uv 版本分层合同。

### Modified Capabilities

- `workspace-packaging`: 外部 package dependency 改为有界兼容范围，根与模板的同仓库自依赖精确匹配项目版本，并由 lock 保持完整解析。
- `release-dry-run-private-registry`: promotion 升版后同步根与模板的精确自依赖，发布工具精确基线不变。
- `maintainer-documentation`: 双语文档明确区分支持范围、当前 lock 解析和 CI/发布精确工具基线。

## Impact

- 配置与 lock：`pyproject.toml`、`packages/agent-harness/pyproject.toml`、`templates/service-app/pyproject.toml`、`uv.lock`。
- 发布代码：`scripts/release_workspace_contract.py`、preview/formal build seam、manifest 及相关 promotion/preview 合同。
- 测试与证据：新增 dependency policy contracts，更新 workspace、template、release promotion/preview、文档与 acceptance matrix 合同。
- 文档：Product Spec/CHANGELOG、DEV-PLAN、根与模板双语 README、双语 release process、acceptance matrix。
- 无 API、数据、UI 或外部系统副作用。
