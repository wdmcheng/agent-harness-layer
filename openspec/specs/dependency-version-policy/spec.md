# dependency-version-policy Specification

## Purpose

定义 Python 依赖声明、精确 lock、本地 uv 兼容范围与发布构建 backend 身份之间的分层维护契约。

## Requirements

### Requirement: Python 依赖声明使用有界兼容范围
系统 SHALL 在根 workspace、核心包、optional extra、service-app 模板及其 dev/license/release/build-system metadata 中，为可放宽的外部 Python 依赖声明已验证下界和兼容上界；稳定版本不得跨下一主版本，`0.x` 版本不得跨下一次版本，已知上游组合约束 MUST 保留更窄上界。同仓库 `agent-harness` 自依赖 MUST 精确匹配当前项目版本。

#### Scenario: 三份 package metadata 不含无说明 exact pin
- **WHEN** 维护者检查三份 `pyproject.toml` 的全部 dependency 和 build-system requirement
- **THEN** 每项可放宽的外部依赖都同时含 `>=` 与 `<`，根 workspace 与模板的 `agent-harness` 自依赖精确等于当前项目版本，且不存在其他无说明的 `==` pin

#### Scenario: 已知 optional extra 组合保留窄上界
- **WHEN** 维护者检查 observability extra
- **THEN** OpenTelemetry 声明保留与当前 Logfire 组合相容的 `<1.43` 上界，而不是仅按下一主版本放宽

### Requirement: 精确 lock 与声明范围分层
系统 MUST 以 `uv.lock` 保存当前完整精确解析；只放宽且仍包含当前版本的声明时，系统 MUST NOT 改变任何已锁 package 的 `(name, version, source)`，依赖升级 MUST 由显式 `uv lock --upgrade` 或等价受审动作发起。

#### Scenario: 只刷新声明 metadata 不升级 package
- **WHEN** 维护者在不使用 `--upgrade` 的情况下刷新由兼容范围覆盖的 lock
- **THEN** 变更前后全部 package identity 相同，`uv lock --check` 与 frozen sync 通过

#### Scenario: 普通同步不选择范围内新版本
- **WHEN** 范围内存在比当前 lock 更新的可用版本且维护者运行普通 locked/frozen sync
- **THEN** uv 继续安装 lock 中的精确版本，不自动升级

#### Scenario: 互斥依赖组分别执行 frozen sync
- **WHEN** 根配置声明 `release` 与 `license` dependency groups 冲突
- **THEN** 仓库分别以 `--group release --no-group license` 和 `--group license --no-group release` 执行 frozen sync，并拒绝用无排除条件的 `--all-groups` 作为通过门禁

### Requirement: 本地 uv 兼容范围与发布基线分离
根 `[tool.uv].required-version` SHALL 接受 `>=0.11.19,<0.12`；GitHub、GitLab、release wrapper 和发布证据 MUST 继续精确使用 `0.11.29`。

#### Scenario: 已验证旧 patch 可读取当前 lock
- **WHEN** 本地使用 uv `0.11.19` 执行 lock check
- **THEN** 命令通过 `required-version` 门禁并能读取当前 lock

#### Scenario: 跨 minor 或发布工具漂移被拒绝
- **WHEN** 本地 uv 达到 `0.12`，或 release wrapper 解析到非 `0.11.29` 的 uv
- **THEN** 对应入口在执行 lock/build/publish 语义前 fail closed

### Requirement: 仓库发布构建绑定精确 build backend
build-system metadata SHALL 向 workspace 外消费者声明有界兼容范围，但仓库 release preview 与正式 tag build MUST 先按 frozen lock 准备并核对精确 build backend，再关闭默认 build isolation 使用该 backend；对应 manifest MUST 记录 backend 名称和版本，缺失或漂移 MUST 在产物授权前 fail closed。

#### Scenario: Preview 与正式构建使用 lock 内 Hatchling
- **WHEN** release preview 或正式 tag build 对 `hatchling>=1.30.1,<2` 的 package metadata 执行构建
- **THEN** 构建只使用当前 lock 内精确 `hatchling 1.30.1`，manifest 记录该 identity，默认隔离环境不得另行选择范围内版本

#### Scenario: Build backend identity 漂移被拒绝
- **WHEN** frozen lock、构建环境或 manifest 中的 Hatchling 名称/版本缺失或不一致
- **THEN** preview、promotion 或 publish consumer 在授权 wheel/sdist 前 fail closed
