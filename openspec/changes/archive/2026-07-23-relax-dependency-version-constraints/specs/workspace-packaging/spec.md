## MODIFIED Requirements

### Requirement: 包依赖方向被强制执行
核心包 MUST NOT 依赖 `templates/*` 或 `examples/*`，并且 service-app template SHALL 通过 workspace source、与当前项目版本精确匹配的 package dependency 或已构建 wheel 依赖 `agent-harness`；外部依赖的本地精确解析 MUST 由 `uv.lock` 提供，同仓库自依赖的 exact declaration 则表达版本身份耦合。

#### Scenario: 核心包没有反向依赖
- **WHEN** 运行依赖元数据和 import boundary checks
- **THEN** `packages/agent-harness` 不引用 `templates/*` 或 `examples/*`

#### Scenario: Template 通过包边界依赖核心包
- **WHEN** service-app template 安装到 workspace 中
- **THEN** 它通过与当前项目版本精确匹配的 package dependency 解析 `agent-harness`，而不是通过相对路径 import 源码或允许跨版本组合的范围
