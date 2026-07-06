## Purpose

定义 service-app template 的后端应用壳、目录边界、本地 profile 和文档入口要求。

## Requirements

### Requirement: Service-app template 暴露预留后端布局
service-app template SHALL 预留 Product Spec 要求的后端应用、agent、配置、eval、测试、文档和环境文件布局。

#### Scenario: Template 目录结构存在
- **WHEN** 开发者检查 `templates/service-app`
- **THEN** template 包含 `app/`、`agents/`、`configs/profiles/`、`eval-cases/drafts/`、`eval-cases/approved/`、`tests/`、`docs/`、`.env.example`、`Makefile`、`README.md` 和 `pyproject.toml`

### Requirement: App entry code 与 agent logic 分离
template SHALL 保持应用入口与未来业务 agent 实现目录分离。

#### Scenario: App entry 目录已预留
- **WHEN** 开发者检查 `templates/service-app/app`
- **THEN** 它包含预留的 `api/`、`cli/` 和 `workers/` 入口区域

#### Scenario: Agent 目录已预留
- **WHEN** 开发者检查 `templates/service-app/agents`
- **THEN** agent 实现预留在 agent 专属目录下，而不是放进 `app/*`

### Requirement: Local profile shell 无需外部 provider 凭据即可运行
service-app shell SHALL 提供本地开发 profile 和 smoke entrypoint，可在没有真实模型 key 或外部 observability providers 的情况下运行。

#### Scenario: Local profile 存在
- **WHEN** 开发者检查 `templates/service-app/configs/profiles`
- **THEN** `local.yaml` 存在，并声明适合后续 fake provider 和 local-jsonl integration 的本地默认值

#### Scenario: Local smoke entrypoint 存在
- **WHEN** 开发者运行 template 本地 smoke 命令
- **THEN** 命令只使用 workspace 本地配置即可完成，且不需要真实模型 key 或 SaaS provider 凭据

### Requirement: Template 文档标明 developer 和 maintainer entrypoints
service-app template SHALL 记录 agent 应用开发者如何从 template 启动，以及脚手架维护者如何让 template 与核心包边界保持一致。

#### Scenario: Template README 描述两个受众
- **WHEN** 开发者阅读 `templates/service-app/README.md`
- **THEN** README 标明 app 开发者设置步骤和脚手架维护者边界职责
