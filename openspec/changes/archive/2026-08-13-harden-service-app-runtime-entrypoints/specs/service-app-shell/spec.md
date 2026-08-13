## ADDED Requirements

### Requirement: Template worker 入口显式绑定 profile 与目录
service-app template SHALL 让 `make worker` 把调用方选择的 profile 与 profiles 目录显式传给 runtime worker。`service` profile MUST 保持常驻消费；其他 profile MUST 使用 `--once`。

#### Scenario: Local worker 使用显式配置并单次退出
- **WHEN** 开发者在复制模板中 dry-run 或执行 `make worker PROFILE=local PROFILES_DIR=<profiles>`
- **THEN** worker 命令包含 `--profile local`、`--profiles-dir <profiles>` 与 `--once`

#### Scenario: Service worker 保持常驻消费
- **WHEN** 开发者在复制模板中 dry-run `make worker PROFILE=service PROFILES_DIR=<profiles>`
- **THEN** worker 命令包含 `--profile service` 与 `--profiles-dir <profiles>`，且不包含 `--once`

### Requirement: App 与 worker 复用同一显式 env file
service-app SHALL 允许 app factory、runtime composition 与 worker 选择同一个可选 env file。复制模板内部 `python -m app.workers.runtime_worker` CLI SHALL 向后兼容接受可选 `--env-file`；产品公开 `agent-harness` CLI 及其 schema MUST 保持不变。显式提供时，本次启动的全部配置加载 MUST 使用该值且 MUST NOT 重新发现复制目录、仓库根目录或当前目录中的其他 `.env`；省略时 MUST 保持既有默认 `.env` 发现和优先级语义。

#### Scenario: App factory 的健康摘要与 runtime 使用同一 env file
- **WHEN** 调用方以显式 env file 创建 app，且 app 需要构造真实 runtime components
- **THEN** 健康摘要与 runtime composition 都把同一路径传给既有类型化配置加载器

#### Scenario: 注入组件的 app 不受冲突 ambient env file 污染
- **WHEN** 测试以专属空 env file 创建注入组件的 app，同时复制项目或当前目录存在冲突 `.env`
- **THEN** app 配置结果不读取冲突文件，且不触发真实 provider、credential 或外部服务

#### Scenario: Worker 两种启动模式逐层传递显式 env file
- **WHEN** worker 以 local `--once` 或 service 常驻方式接收 `--env-file <path>`
- **THEN** parse、run-once/run-forever、worker core 与 runtime composition 都保留同一路径

#### Scenario: 省略 env file 保持兼容
- **WHEN** 现有 app 或 worker 调用方不提供 env file
- **THEN** 参数保持 `None` 并由既有配置加载器执行默认发现，不要求调用方迁移

#### Scenario: Make 只在 ENV_FILE 非空时传参
- **WHEN** 开发者分别以非空、空值或省略的 `ENV_FILE` dry-run `make worker`
- **THEN** 非空值原样形成 `--env-file` 参数；空值或省略时不生成该参数

### Requirement: Workspace 外复制合同只证明入口 Bug
根级聚焦合同 SHALL 在 workspace 外复制 `templates/service-app`，只验证上述 Make 参数与显式 env file 隔离。测试 MUST 使用 fake/local 依赖和测试专属配置，不读取真实凭据，不调用真实 provider、外部业务工具或 SaaS，也不得为证明本 Bug 新增生产控制面。

#### Scenario: 复制目录 app 忽略冲突 ambient env file
- **WHEN** 聚焦合同分别在 clean 与含冲突 `.env` 的 workspace 外复制目录中，实际导入复制出的 app factory，并用显式空 env file 构造 app
- **THEN** 两次 app 配置与健康结果逐值一致，且导入路径指向复制目录而非源仓库模板

#### Scenario: 复制目录 runtime composition 使用同一显式 env file
- **WHEN** 同一聚焦合同分别从 clean 与冲突复制目录实际进入复制出的 runtime composition settings 边界
- **THEN** 两次都接收各自复制目录中的同一显式 env file，并在创建存储、provider 或其他运行副作用前证明配置结果一致

#### Scenario: 简单离线合同不成为生产能力
- **WHEN** 维护者检查本 change 的生产文件与测试 helper
- **THEN** 所有新增生产 seam 都可追溯到 profile、profiles-dir、once 或 env-file Requirement；测试专用 fixture/断言不被模板 runtime、service composition 或公开 API 导入
