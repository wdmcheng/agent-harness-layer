## 1. Secret File Loader 契约与实现

- [x] 1.1 先在 `test_typed_config_contracts.py` 增加 `_FILE` 成功映射和 direct/file 冲突 red tests，逐值验证既有 typed field path、稳定 error code 与无 secret 回显。
- [x] 1.2 在公共 config loader 实现 `_FILE` 收集、冲突优先检查和 profile → agent → `.env` → secret file → process env → overrides 合并顺序，不增加 `SecretProvider` 抽象。
- [x] 1.3 增加受信 root、绝对普通文件、非 symlink、不可读/越界、UTF-8、非空、64 KiB 和单个结尾换行合同；用文件替换/逃逸夹具证明失败路径不读取或泄漏目标内容。
- [x] 1.4 把所有拒绝路径映射为稳定 `config.secret_file_invalid` 或既有 structured config error，验证 field path/remediation 可操作且不含文件内容、raw exception 或受信 root 外绝对路径。

## 2. Application Startup Fail-Closed

- [x] 2.1 为 CLI、FastAPI app factory、runtime worker 和 migration composition 增加相同缺失/无效/冲突配置 red tests，断言监听、连接、migration、run/event 等副作用计数为零。
- [x] 2.2 让四类入口复用公共 loader/启动错误映射，逐值验证相同 code、field path 和安全 remediation；配置失败时不得创建可请求的 health endpoint或伪装为 `degraded`。
- [x] 2.3 使用唯一 secret fixture 扫描 stdout/stderr、doctor、health、日志、error envelope、trace、eval、audit 和 CanonicalEvent evidence，补齐所有泄漏回归测试。

## 3. Service Profile 装配

- [x] 3.1 更新 `docker-compose.yml`、service profile 与 `.env.example`，让 API、worker 和 migration composition 使用一致的只读 mount/`_FILE` 引用；示例只记录路径和生成/清理方法，不保存真实 secret。
- [x] 3.2 扩展 wheel-only template contract 和 service smoke：从隔离临时文件启动三类应用进程，验证 typed value 生效、公开 evidence 无原值，成功/失败/中断均清理临时 secret。
- [x] 3.3 增加无效 mount、缺失、不可读、空值、symlink、越界与 direct/file 冲突的 Compose/readiness 测试，证明依赖进程不会进入可用状态且诊断安全。
- [x] 3.4 让 PostgreSQL 官方镜像通过独立 `POSTGRES_PASSWORD_FILE` secret mount 启动，并把 `docker compose config` 纳入 secret-value 扫描，证明规范化配置不展开 storage DSN 或数据库密码。

## 4. 验证与收口

- [x] 4.1 运行 config/startup 定向 contract/integration tests、wheel-only template tests、`make smoke-local` 和真实 PostgreSQL/Redis `make smoke-service`，分别记录离线与 service 证据。
- [x] 4.2 运行 import/secret scans、OpenAPI drift、`make quality`、`make test`、`make build`、`make license-check`、pre-commit、`git diff --check` 和 `openspec validate config-secret-file-loading --type change --strict`，保持全部 task 未完成前不声明 ready-to-archive。
