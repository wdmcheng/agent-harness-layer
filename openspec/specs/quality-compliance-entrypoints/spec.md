## Purpose

定义仓库根目录质量命令、测试与 smoke 入口、文档边界、Apache-2.0 许可证基线，以及 pre-commit 本地质量门禁要求。

## Requirements

### Requirement: 根目录质量命令表面存在
仓库 SHALL 提供根目录命令，覆盖依赖设置、质量检查、测试、本地 smoke 验证、包构建和许可证检查。

#### Scenario: Quality command 可执行
- **WHEN** 开发者运行 `make quality`
- **THEN** 命令针对当前受支持 code surface 运行 linting、格式检查、类型检查和 import boundary checks

#### Scenario: Test command 可执行
- **WHEN** 开发者运行 `make test`
- **THEN** 命令运行当前 unit 和 contract test suite

#### Scenario: Local smoke command 可执行
- **WHEN** 开发者运行 `make smoke-local`
- **THEN** 命令在没有外部服务依赖的情况下验证 local workspace 和 template shell

#### Scenario: Build command 可执行
- **WHEN** 开发者运行 `make build`
- **THEN** 命令通过 uv 构建核心包 artifacts

#### Scenario: License check command 可执行
- **WHEN** 开发者运行 `make license-check`
- **THEN** 命令验证预期 license 和 NOTICE baseline

### Requirement: 文档说明仓库目的和边界
根 README SHALL 说明 scaffold 是什么、如何本地启动、项目结构，以及禁止的跨边界依赖。

#### Scenario: README 解释项目结构
- **WHEN** 新开发者阅读根 README
- **THEN** 他们能识别 `packages/agent-harness`、`templates/service-app`、`examples`、`docs` 和 `scripts` 的目的

#### Scenario: README 解释依赖边界
- **WHEN** scaffold maintainer 阅读根 README
- **THEN** 他们能识别核心包不依赖 templates 或 examples，并且 vendor SDKs 应位于 adapters 或未来受控集成模块之后

### Requirement: License 和 NOTICE baseline 存在
仓库 MUST 在功能开发开始前包含 Apache-2.0 license file 和 NOTICE file。

#### Scenario: License file 存在
- **WHEN** 开发者检查仓库根目录
- **THEN** `LICENSE` 存在并声明 Apache-2.0

#### Scenario: NOTICE file 存在
- **WHEN** 开发者检查仓库根目录
- **THEN** `NOTICE` 存在，作为必需第三方声明和源码归因的记录位置

### Requirement: Pre-commit entrypoint 存在
仓库 SHALL 提供 pre-commit configuration，指向仓库 quality checks。

#### Scenario: Pre-commit configuration 存在
- **WHEN** 开发者检查仓库根目录
- **THEN** `.pre-commit-config.yaml` 存在，并可安装用于 local quality checks
