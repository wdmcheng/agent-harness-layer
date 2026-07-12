## MODIFIED Requirements

### Requirement: 配置加载器合并 env、profile YAML 和 agent YAML
package SHALL 提供 typed settings loader，把显式 defaults、profile YAML、agent config YAML、`.env`、受控 Docker secret file、environment values 和 explicit overrides 合并成一个已校验 settings object。合并顺序 MUST 为 profile YAML → agent YAML → `.env` → Docker secret file → process env → explicit overrides；所有 secret file 值 MUST 在进入既有 env path parser 后由同一 Pydantic schema 校验。

#### Scenario: Local profile 不需要 provider key
- **WHEN** 调用方加载 `templates/service-app/configs/profiles/local.yaml`
- **THEN** storage、queue、observability、policy、model、budget 和 identity settings 在不需要真实模型或 SaaS provider credentials 的情况下通过校验

#### Scenario: Service profile 校验部署边界
- **WHEN** 调用方加载 `templates/service-app/configs/profiles/service.yaml`
- **THEN** API/worker process settings、shared storage/queue config 和 provider boundary placeholder 都以 typed settings 形式通过校验，且不启动外部服务

#### Scenario: Agent YAML 参与 typed merge
- **WHEN** 调用方提供包含 metadata、budget、tool allowlist、eval dataset 或 delegation edges 的 agent config YAML
- **THEN** 这些值通过 typed schema 校验，并出现在 merged settings object 中

#### Scenario: Docker secret file 映射到既有 typed field
- **WHEN** service process 只设置 `AGENT_HARNESS_STORAGE__DSN_FILE`，其值是默认或显式受信 root 内的合法 secret file 绝对路径
- **THEN** loader 把文件内容映射为 `storage.dsn`，并按与 `AGENT_HARNESS_STORAGE__DSN` 相同的 schema 和 field path 校验

#### Scenario: Direct value 与 file value 冲突
- **WHEN** 同一进程环境同时设置 `<BASE_ENV>` 与 `<BASE_ENV>_FILE`
- **THEN** loader 在读取配置副作用和应用启动前返回结构化冲突错误，不静默选择任一值且不回显 direct value 或文件内容

## ADDED Requirements

### Requirement: Secret file 读取边界 fail-closed
loader SHALL 只读取默认 `/run/secrets` 或测试显式注入的受信 root 内绝对路径所指向的普通、非 symlink 文件。loader MUST 拒绝相对路径、目录、symlink、规范化后越界、不可读、空值、非 UTF-8 和超过 64 KiB 的文件；读取成功时只移除一个结尾换行，其他空白 MUST 保留。

#### Scenario: 合法只读 secret file 被消费
- **WHEN** `_FILE` 指向受信 root 内可读、非空、UTF-8、大小不超过 64 KiB 的普通文件
- **THEN** loader 返回去掉至多一个结尾换行的值，且不在诊断或 evidence 中公开原值

#### Scenario: 路径或文件类型不受信
- **WHEN** `_FILE` 是相对路径、目录、symlink、特殊文件或解析后逃出受信 root
- **THEN** loader 返回 `config.secret_file_invalid`，不读取目标内容，不回显受信 root 外绝对路径

#### Scenario: 内容不满足边界
- **WHEN** secret file 不可读、为空、不是 UTF-8 或超过 64 KiB
- **THEN** loader 返回 `config.secret_file_invalid` 和安全修复提示，错误、日志及公开 evidence 不包含文件内容或 raw exception

### Requirement: Application startup 统一配置失败
CLI、FastAPI、runtime worker 和 migration composition SHALL 在加载缺失、无效或冲突配置时复用同一结构化失败合同，包含稳定 error code、field path 和安全 remediation。配置失败 MUST 在监听端口、连接 storage/queue、运行 migration、创建 run 或发布业务 evidence 前终止；health endpoint MUST NOT 把启动配置失败表示为运行中 `degraded`。

#### Scenario: 四类入口对缺失必填字段一致失败
- **WHEN** 相同 service profile 缺少必填配置并分别启动 CLI composition、FastAPI app、worker 和 migration composition
- **THEN** 四者在外部副作用前失败，并返回相同 code、field path 和不含 secret/绝对路径的修复提示

#### Scenario: Secret fixture 不进入公开观测面
- **WHEN** direct value、secret file 内容或底层异常包含唯一 secret fixture
- **THEN** stdout/stderr、doctor、health、日志、error envelope、trace、eval、audit 和 CanonicalEvent evidence 均不包含该原值
