## ADDED Requirements

### Requirement: Vendor import 边界被声明并扫描
repository SHALL 声明 banned vendor SDK imports，并扫描 business/template/example surfaces，确保 Pydantic AI、DBOS、Logfire、Phoenix、Langfuse 等 provider 只出现在 adapter 或受控 integration boundary 下。

#### Scenario: Template app 不能直接 import vendor SDK
- **WHEN** static import boundary checks 扫描 `templates/service-app/app/*`、`templates/service-app/agents/examples/*` 和 `examples/*`
- **THEN** 直接 import banned vendor SDK 会失败，除非文件位于 approved adapter / integration path

#### Scenario: Core package 不依赖 templates 或 examples
- **WHEN** static import boundary checks 扫描 core package metadata 和 imports
- **THEN** `packages/agent-harness` 不存在指向 template 或 example code 的 dependency / import edge

### Requirement: Doctor 命令报告 profile 加载状态
`agent-harness` package SHALL 暴露 CLI doctor command，通过公开 command seam 校验指定 profile 并报告 configuration load status。

#### Scenario: Local doctor 成功
- **WHEN** developer 运行 `agent-harness doctor --profile local`
- **THEN** command 加载 local profile，报告 profile、storage、queue、observability、policy、identity 和 model 状态，并在无真实 provider key 时成功退出

#### Scenario: Doctor 报告配置错误
- **WHEN** 选定 profile 缺失或非法
- **THEN** command non-zero 退出，并打印 structured field-path diagnostics 和 repair hints

### Requirement: 部署边界文档可见
repository SHALL 文档化哪些 boundary 在 P0 中同进程运行，以及哪些 interface 在未来拆分 API/worker/model/tool/storage/event 时保持稳定。

#### Scenario: Maintainer 能识别未来拆分路径
- **WHEN** maintainer 阅读 README 或 architecture docs
- **THEN** 能识别 Access/API gateway、runtime worker、model gateway、tool gateway、storage service、event/observability pipeline 的当前形态和未来拆分路径，且不会误以为 P0 已实现物理微服务

## MODIFIED Requirements

## REMOVED Requirements

## RENAMED Requirements
