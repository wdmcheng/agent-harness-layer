## Context

现有 `load_settings` 按 profile YAML、agent YAML、`.env`、进程环境变量、explicit overrides 合并并交给 Pydantic 校验；它尚不识别 `_FILE`，也没有针对 secret file 的路径、类型、大小和内容边界。service-app 的 API、worker、CLI 与 migration composition 各自调用配置/环境入口，如果失败形状不统一，部署会出现某个进程继续启动、公开错误泄漏路径或 secret 的风险。

## Goals / Non-Goals

**Goals:**

- 在同一个 typed settings 边界把 `<BASE_ENV>_FILE` 解析为既有 env path，保持 schema 为唯一字段真相源。
- 对 direct/file 冲突、路径逃逸、文件类型、权限/可读性、编码、空值和大小执行 fail-closed 校验。
- 让所有进程入口复用稳定、脱敏的 `SettingsLoadError`/error code，并让 Docker Compose 装配可验证。

**Non-Goals:**

- 不设计 `SecretProvider`、Vault/KMS adapter、rotation 或远程 fetch；P0 只有 env 与本地只读 secret file 消费。
- 不把 secret 保存到 profile、数据库、artifact 或公开 health/doctor/evidence，也不新增配置热重载。
- 不把 loader 扩张为抵御可并发改写受信 root 或其宿主父目录的本地敌手沙箱；P0 前提是部署方控制该目录，并以只读 mount 提供给应用。Agent 网络之间的信任与隔离由 identity、tenant、policy 和 runtime 边界负责，不由本地 secret loader 承担。

## Decisions

1. **`_FILE` 是现有 env 映射的输入形式，不是新 schema。** Loader 先收集同一来源中的 `<BASE_ENV>`/`<BASE_ENV>_FILE` 并检查冲突，再读取文件并把值交给既有 `_env_values_to_nested`。这保持 field path、Pydantic validation 和 overrides 语义一致。替代方案是为每个 secret 字段增加 `SecretRef`，但会扩张所有 settings DTO 并制造单一实现抽象，因此拒绝。
2. **受信 root 与文件验证在打开前后都 fail-closed。** 生产默认 root 是部署方控制的只读 `/run/secrets`，测试可以显式注入临时 root。输入必须是绝对路径；规范化父路径和目标都必须位于 root 内，目标不得是 symlink、目录或其他特殊文件。打开时采用避免跟随最终 symlink 的受控方式，并在读取后复核文件 identity，降低检查与读取之间的目标替换风险。能够并发替换受信 root 或宿主父目录的本地进程不属于本 change 的威胁模型；该隔离由容器只读 mount 和宿主权限保证。
3. **内容合同固定且有界。** 最大读取 64 KiB，必须为 UTF-8、去掉至多一个结尾 `\n`（同时接受 Docker 常见的 `\r\n` 作为一个行结束符），结果不得为空。保留其他前后空白，避免静默改变密码。读取、解码、大小或空值失败统一映射 `config.secret_file_invalid`。
4. **合并顺序与冲突独立。** 顺序为 profile YAML → agent YAML → `.env` → Docker secret file → process env → explicit overrides。`_FILE` 只来自进程环境，不从 `.env` 读取；若进程环境同时给出 direct 与 `_FILE`，即使后续 override 存在也先失败。这样部署错误不会被更高优先级输入掩盖。
5. **入口组合失败使用同一安全诊断。** API/worker/CLI/migration composition 都调用公共 loader 或公共 startup 映射，保留稳定 code、field path、remediation；公开输出不得包含读取内容、DSN/token、root 外绝对路径或 raw exception。health 只在配置装配成功后存在，不能把启动失败伪装成 `degraded`。
6. **部署装配只挂载引用。** Compose 为 API、worker 和 migration 使用同一只读 storage DSN secret mount 与 `_FILE` env；PostgreSQL 官方镜像通过独立 `POSTGRES_PASSWORD_FILE` secret mount 取得初始化密码，不把值插入 Compose environment。`.env.example` 只给 source path 和生成说明，真实文件由调用方在隔离环境创建并清理。`docker compose config` 必须纳入 secret-value 扫描；规范化输出显示 operator 提供的 source path 属于部署元数据，不等同 secret 值，但该宿主路径仍不得进入应用日志、health、event、数据库或 artifact。

## Affected Surfaces

- `agent_harness.config.settings`、公开 config exports 与结构化错误映射。
- service-app API app factory、worker startup、CLI composition、migration composition。
- `docker-compose.yml`、service profile、`.env.example` 和 smoke 的临时 secret fixture。
- 不增加数据库表或 migration，不新增 HTTP endpoint，不改变 settings DTO 字段。

## Testing Seams

- 通过公共 `load_settings` 验证成功映射、direct/file 冲突和所有文件拒绝路径。
- 通过 CLI、FastAPI app factory、worker 与 migration composition 验证相同 code/field path/remediation，且进程在副作用前退出。
- wheel-only 复制模板与 `make smoke-service` 验证同一 storage DSN mount 被 API/worker/migration 消费，并验证 PostgreSQL 只使用独立 password file。
- 使用唯一 secret fixture 扫描 `docker compose config`、stdout/stderr、error envelope、doctor、health、日志、trace、eval、audit 与 event evidence，断言原值不存在；除 Compose source metadata 外，宿主路径也不得进入应用观测面。

## Risks / Trade-offs

- [Risk] 受信 root 内目标在路径预检与读取之间发生替换 → 使用最终组件非跟随打开、普通文件/identity 复核；平台不支持时拒绝。受信 root 与宿主父目录由部署方只读隔离，不承诺对可改写该目录的本地敌手提供文件系统沙箱。
- [Risk] 限制 64 KiB 会拒绝超大证书链 → P0 选择可审计上限；需要大材料时另行设计 artifact/certificate contract。
- [Risk] direct/file 冲突让旧部署从静默覆盖变为启动失败 → 错误提供字段级修复提示，部署前 contract/smoke 明确验证。
- [Risk] 错误对象携带原始 `OSError` 信息泄漏绝对路径 → 只映射允许的 field path 和固定 remediation，不序列化 raw exception。

## Migration Plan

先发布兼容的 loader 与入口测试，再更新 Compose/profile 示例使用 `_FILE`。现有 direct env 仍有效；部署方必须二选一，不能同时设置 direct 与 `_FILE`。回滚时可恢复 direct env 并移除 `_FILE`/mount；本 change 无数据 migration。完成后保持 change 为 ready-to-archive，不自动归档。

## Open Questions

无。`SecretProvider` 与 Vault/KMS 已由 Product Spec 明确放入 P1，不阻塞本 change。
