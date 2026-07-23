## MODIFIED Requirements

### Requirement: 发布物不携带 workspace path dependency
核心 wheel/sdist 的外部依赖 MUST 使用可发布的有界兼容版本元数据；根 workspace 与模板对 `agent-harness` 的自依赖 MUST 精确匹配当前项目版本。本地 workspace source 只能用于 checkout 内解析，不能进入分发产物。promotion 更新版本时 MUST 把根与模板的 `agent-harness` 自依赖都更新为完整新版本的 exact pin，不得放宽为范围或通配形式。

#### Scenario: 分发元数据可在 workspace 外解析
- **WHEN** 从 dry-run 产物检查 wheel METADATA、sdist 与模板依赖
- **THEN** `agent-harness` 精确匹配当前项目版本，且不存在 `workspace = true`、`file://`、绝对路径或相对 path source

#### Scenario: Workspace 外默认隔离构建可解析兼容 backend
- **WHEN** 维护者把核心 package 复制到 workspace 外或解包 sdist，移除 workspace source，并在未预装 build backend 且不使用 `--no-build-isolation` 的环境执行标准构建
- **THEN** 构建器能从 `hatchling>=1.30.1,<2` metadata 创建隔离环境并成功生成产物，且不读取源 workspace 或其 lock 作为隐藏依赖

#### Scenario: Promotion 同步根与模板的精确自依赖
- **WHEN** promotion 把 package version 从 `0.1.x` 更新为 `0.2.0`
- **THEN** 根 workspace 与模板都声明 `agent-harness==0.2.0`，无关 dependency metadata 保持不变

### Requirement: 正式发布物从 tag target 重新构建
系统 SHALL 在 release commit、annotated tag 与 provider release notes 创建后，从 tag target 的隔离、无 credential checkout 先按 frozen lock 准备并核对精确 build backend，再使用固定版本 `uv build --no-build-isolation`，原子生成 `release-build/v1` manifest、wheel、sdist 与 `SHA256SUMS`。正式 manifest MUST 写入 `status: built`，并绑定 tag、tag target、package version、uv 版本、build backend 名称/版本和每个 artifact 的 repo-relative path/kind/SHA-256/size；preview artifact 不得替代该正式构建。任何 consumer MUST 拒绝缺失 `status`、状态不为 `built` 或 backend identity 与 frozen lock 不一致的 manifest。

#### Scenario: tag 后构建形成正式发布输入
- **WHEN** promotion 已创建指向 release commit 的 annotated tag 和同版本 release notes
- **THEN** builder 从该 tag target 以 `--group release --no-group license` frozen sync，核对并使用 lock 内精确 build backend 重新构建正式 wheel/sdist，分别读取 wheel `METADATA` 及 sdist `PKG-INFO`/`pyproject.toml`，验证两种包的版本均等于 tag/version 且均无 workspace/path dependency，并原子写出状态为 `built`、含 backend identity 的 `release-build/v1`

#### Scenario: 非 built 正式构建不得授权下游
- **WHEN** `release-build/v1` 缺少 `status`、状态不是 `built`、缺少 backend identity 或该 identity 与 frozen lock 不一致
- **THEN** promotion receipt、registry plan/execute 与双 CI consumer 均在发布副作用前 fail closed，不得把该 manifest 或其 artifact 作为正式发布输入

#### Scenario: tag 后构建失败不授权 publish
- **WHEN** tag checkout、固定 uv、frozen build backend、wheel/sdist、checksum、版本或 workspace boundary 任一验证失败
- **THEN** promotion 写入 `failed` 回执并保留已确认 commit/tag/provider 身份供人工复核，不生成可供 registry plan 消费的 `promoted` 授权

#### Scenario: no-release 禁止 promotion
- **WHEN** manifest 状态为 `no-release`
- **THEN** plan job 原子生成 `status: no-release` 且无 approval 的 `release-promotion-plan/v1`，execute 消费同一 plan 后在任何 git/provider 副作用及 credential 要求前成功生成不可用于 publish 的 `no-release` 回执，不创建 commit、tag、release 或正式 build
