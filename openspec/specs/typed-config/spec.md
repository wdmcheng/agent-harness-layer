# typed-config Specification

## Purpose
定义 profile YAML、agent YAML、env / `.env` 和 explicit overrides 的 typed merge 行为，以及 structured diagnostics 和公开 schema import seam。该 spec 保证 local/service profiles 在不启动外部服务的情况下可校验，并为 template app 和后续 runtime 提供配置契约。
## Requirements
### Requirement: 配置加载器合并 env、profile YAML 和 agent YAML
package SHALL 提供 typed settings loader，把显式 defaults、profile YAML、agent config YAML、`.env` / environment values 合并成一个已校验 settings object。

#### Scenario: Local profile 不需要 provider key
- **WHEN** 调用方加载 `templates/service-app/configs/profiles/local.yaml`
- **THEN** storage、queue、observability、policy、model、budget 和 identity settings 在不需要真实模型或 SaaS provider credentials 的情况下通过校验

#### Scenario: Service profile 校验部署边界
- **WHEN** 调用方加载 `templates/service-app/configs/profiles/service.yaml`
- **THEN** API/worker process settings、shared storage/queue config 和 provider boundary placeholder 都以 typed settings 形式通过校验，且不启动外部服务

#### Scenario: Agent YAML 参与 typed merge
- **WHEN** 调用方提供包含 metadata、budget、tool allowlist、eval dataset 或 delegation edges 的 agent config YAML
- **THEN** 这些值通过 typed schema 校验，并出现在 merged settings object 中

### Requirement: 校验诊断可操作
配置校验失败 SHALL 暴露安全的逻辑 field path 和 remediation hint，而不是直接抛出原始 parser trace 或公开宿主机文件系统路径。

#### Scenario: 缺少必填 profile 字段
- **WHEN** required nested profile field 缺失
- **THEN** loading 失败，并报告缺失字段路径和指向 profile 或 env variable 的修复建议

#### Scenario: 非法 YAML 被安全报告
- **WHEN** profile 或 agent YAML 无法解析为 mapping
- **THEN** loading 以 structured config error 失败，`field_path` 标出 `profile` 或 `agent` 逻辑来源，错误不公开宿主机绝对路径，且不会执行 arbitrary YAML tags

### Requirement: 配置 schemas 可公开复用
Profile、provider、storage、queue、observability、policy、budget、identity 和 agent config schemas SHALL 可从 `agent_harness.config` import，供 template app 和 tests 复用。

#### Scenario: Template app 通过公共包 import config schemas
- **WHEN** `templates/service-app/app/*` 下的代码需要配置类型
- **THEN** 它从 `agent_harness.config` import，而不是直接读取 YAML 或依赖 provider SDK
