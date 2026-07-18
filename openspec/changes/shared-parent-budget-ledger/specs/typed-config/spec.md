## ADDED Requirements

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
