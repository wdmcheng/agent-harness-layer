## Purpose

定义 Agent Harness Layer 的 monorepo 结构、核心包构建方式、template 依赖关系，以及防止核心包反向依赖示例或模板代码的包边界要求。

## Requirements

### Requirement: Workspace 能解析核心包和 template 包
仓库 SHALL 定义一个 `uv workspace`，把核心包和 service-app template 作为 workspace members。

#### Scenario: Workspace sync 能解析 members
- **WHEN** 开发者在仓库根目录运行 `uv sync`
- **THEN** uv 能解析 workspace，且 `packages/agent-harness` 和 `templates/service-app` 没有依赖错误

#### Scenario: Workspace 结构暴露预期顶层边界
- **WHEN** 开发者检查仓库根目录
- **THEN** 仓库把 `packages/`、`templates/`、`examples/`、`docs/` 和 `scripts/` 暴露为独立顶层区域

### Requirement: 核心包可独立构建
`agent-harness` 核心包 SHALL 可构建为 wheel 和 sdist，且不 import template 或 example code。

#### Scenario: 核心包构建成功
- **WHEN** 开发者运行 `uv build --package agent-harness`
- **THEN** 构建为核心包产出 wheel 和 sdist artifacts

#### Scenario: 核心包 import 成功
- **WHEN** 开发者从已安装构建产物或 workspace 环境中 import `agent_harness`
- **THEN** import 成功，并暴露包版本值

### Requirement: 包依赖方向被强制执行
核心包 MUST NOT 依赖 `templates/*` 或 `examples/*`，并且 service-app template SHALL 通过 workspace source、与当前项目版本精确匹配的 package dependency 或已构建 wheel 依赖 `agent-harness`；外部依赖的本地精确解析 MUST 由 `uv.lock` 提供，同仓库自依赖的 exact declaration 则表达版本身份耦合。

#### Scenario: 核心包没有反向依赖
- **WHEN** 运行依赖元数据和 import boundary checks
- **THEN** `packages/agent-harness` 不引用 `templates/*` 或 `examples/*`

#### Scenario: Template 通过包边界依赖核心包
- **WHEN** service-app template 安装到 workspace 中
- **THEN** 它通过与当前项目版本精确匹配的 package dependency 解析 `agent-harness`，而不是通过相对路径 import 源码或允许跨版本组合的范围
