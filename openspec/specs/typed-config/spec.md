# typed-config Specification

## Purpose
定义 profile YAML、agent YAML、env / `.env` 和 explicit overrides 的 typed merge 行为，以及 structured diagnostics 和公开 schema import seam。该 spec 保证 local/service profiles 在不启动外部服务的情况下可校验，并为 template app 和后续 runtime 提供配置契约。
## Requirements
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

### Requirement: Shared-budget fingerprint key 通过 typed secret 边界注入
Shared-budget tenant-scoped request fingerprint key SHALL 是 `BudgetSettings` 的 typed secret 字段，并由统一配置加载器从 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY` 或 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE` 注入。两者同时存在 MUST 返回 `config.secret_file_conflict`；file 入口 MUST 复用受信 secret root、绝对普通非 symlink 文件、最大 64 KiB、UTF-8、非空及只移除一个结尾换行的 CFG-001 规则。四类 application startup 在 key 缺失或非法时 MUST fail closed；`SharedBudgetRuntime` 与 migration MUST NOT 直接读取环境变量、文件路径或自行执行 whitespace normalization。

该 secret 字段 MUST 从 settings `model_dump`/`to_payload`、tree snapshot、event、trace、audit、error、日志、health/doctor、数据库与 traceback frame locals 的可观察输出中排除。Runtime composition MAY 在启动时把 secret 转成仅供 fingerprint 计算的进程内 bytes，但 MUST NOT 持久化或回显原值；数据库仍只保存 opaque fingerprint 与 key version。

#### Scenario: Direct typed secret 正常注入
- **WHEN** application 只设置非空 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY`
- **THEN** typed settings 在 startup 构造 shared-budget runtime，operation identity 使用该 key 计算 opaque fingerprint，settings payload、snapshot、日志与 evidence 均不含原值

#### Scenario: Docker secret file 保留统一内容语义
- **WHEN** application 只设置合法 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE`，文件位于受信 root 且以一个结尾换行结束
- **THEN** loader 只移除该一个结尾换行并注入 typed secret；runtime 不再次 `.strip()`，其余前后空白属于 secret 内容且必须保留

#### Scenario: 缺失或非法 key 在四类启动入口失败
- **WHEN** key 缺失，或 direct/file 冲突，或 file 为相对路径、目录、symlink、越界、空、非 UTF-8、超限或不可读
- **THEN** API、worker、migration startup 与 doctor/CLI application boundary 在 shared-budget runtime 接受请求前结构化失败，错误、异常链和 traceback frame locals 不含 key 内容或受信 root 外绝对路径

#### Scenario: Runtime 旁路读取被合同拒绝
- **WHEN** contract 静态或运行时检查 shared-budget composition 与 operation identity
- **THEN** fingerprint key 的唯一来源是已验证 `BudgetSettings`，生产代码不通过 `os.environ`、`Path.read_text()` 或自定义 secret-file env 名称读取 key
