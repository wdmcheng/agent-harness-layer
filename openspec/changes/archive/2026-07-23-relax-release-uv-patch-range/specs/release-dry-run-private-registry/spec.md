## MODIFIED Requirements

### Requirement: ReleaseRecord 只作为版本化 CI artifact
系统 SHALL 用 `release-preview/v1` JSON manifest 表达 Product-Spec `ReleaseRecord`，字段至少覆盖 source identity、版本决策、提交解释、tag 计划、CHANGELOG/release notes 引用、带 SHA-256 的 artifacts，以及状态化 `uv_version`：`status: release` 时该字段 MUST 是实际执行且满足 `>=0.11.29,<0.12` 的三段 uv 版本，`status: no-release` 时该字段 MUST 为 JSON `null`；系统 MUST NOT 创建 `release_records` 数据库表或让 dry-run 连接应用数据库。

#### Scenario: releasable manifest 字段完整
- **WHEN** releasable dry-run 成功
- **THEN** manifest 状态为 `release`，包含 current/next version、tag、commit/diff identity、bump 理由、实际执行的范围内 uv 三段版本和每个构建产物的相对路径、kind、checksum、size

#### Scenario: no-release manifest 不伪造发布物
- **WHEN** dry-run 判定没有 releasable commit
- **THEN** manifest 状态为 `no-release`，next version、tag 与发布 artifacts 为空，记录扫描基线和原因，`uv_version` 为 JSON `null`；producer 不调用版本解析器、不启动任何 uv 子进程也不进入 build，consumer 只接受 `null`

#### Scenario: 发布预演不依赖运行时持久化
- **WHEN** 在没有应用数据库 DSN、migration 或 runtime storage 的隔离环境执行 dry-run
- **THEN** preview 正常生成，仓库不存在 `release_records` migration/model/repository/UoW seam

### Requirement: 私有 registry 执行需要受限授权
系统 MUST 默认只生成去敏 publish plan；真实执行必须同时通过 protected ref、人工批准、受限 credential、HTTPS upload/check endpoint、匹配的 `release-preview/v1`、`release-build/v1` 和状态为 `promoted` 的 `release-promotion/v1` 门禁，并验证 release identity 与正式 artifact checksum 闭合。dry-run wheel/sdist 只作为预演证据，不得直接上传。上传 MUST 使用满足 `>=0.11.29,<0.12` 的实际 uv 版本执行 `uv publish` Python package index 协议，publish plan MUST 记录并绑定该实际版本，upload/check endpoint 摘要都必须绑定审批身份。uv 的网络只能经过进程内受限 loopback relay 到达两个冻结 endpoint；relay MUST 拒绝 30x，且不得把 credential 或 distribution body 转发到重定向目标。

publish plan MUST 以 `registry-publish-plan/v1` artifact 持久化必填 `status: planned`、去敏输入、实际 publish uv 版本、动态 `approval_sha256`、`release-build/v1` manifest checksum 和待上传正式 artifact identity。plan producer MUST NOT 读取 registry credential；protected/manual execute consumer MUST 下载同一 plan artifact、拒绝缺失或非 `planned` 状态、从受限环境读取 credential，并把 artifact 中的动态摘要注入 wrapper。wrapper MUST 重新计算当前输入与实际 uv 版本摘要并与批准值比较，不得信任静态 YAML 常量、job 顺序或人工复制值。

#### Scenario: publish 计划与执行分成两个 job
- **WHEN** 双 CI 准备执行私有 registry publish
- **THEN** 无 credential 的 plan job 先生成可归档 `registry-publish-plan/v1`，protected/manual execute job 后续消费同一 artifact；任一 plan 缺失、schema/checksum/实际 uv 版本漂移或动态摘要不匹配都在网络前失败

#### Scenario: 缺少任一门禁时 fail closed
- **WHEN** execute 请求缺少批准、protected ref、credential、HTTPS upload/check endpoint、匹配 preview/build/receipt，plan 状态不是 `planned`，或 receipt 为 `no-release`/`failed`
- **THEN** wrapper 在启动网络上传前失败，日志不包含 token 原值

#### Scenario: 陈旧 promotion 回执不能授权上传
- **WHEN** receipt 的 preview/build manifest checksum、source identity、version、release commit/tag target 或任一正式 artifact checksum 与当前受审输入不一致
- **THEN** wrapper 在网络上传前 fail closed，并指出需要重新 preview/promotion 或人工复核的具体身份字段

#### Scenario: 审批后 endpoint 漂移不能启动网络
- **WHEN** 批准后 upload endpoint 或 check endpoint 任一发生变化
- **THEN** wrapper 在启动 uv 或 relay 网络请求前因审批摘要不匹配而 fail closed

#### Scenario: registry endpoint 不得夹带 URL credential
- **WHEN** plan producer 收到含 userinfo、query 或 fragment 的 upload/check endpoint
- **THEN** 系统在生成计划、读取受限 credential 或启动网络前拒绝，错误输出不得回显 URL 中的敏感内容

#### Scenario: 本地 registry 替身验证上传合同
- **WHEN** 测试模式对 loopback package-index 替身使用假 credential和范围内 uv 上传固定 checksum artifacts
- **THEN** 替身收到 `uv publish` 的标准 multipart metadata、Basic token username与原 distribution bytes，并通过 simple index hash 提供安全查重；仓库历史和任何外部 registry 不受影响

#### Scenario: 失败只允许安全重试
- **WHEN** registry 返回 transient failure 或部分结果不确定
- **THEN** 系统通过 check endpoint 的受支持 hash 只确认或有界重试完全相同 checksum；upload 回包未知、2xx headers 后响应体截断、202 或 30x 后 relay 阻止同一 uv 进程再次向外部 registry POST，且仅以 relay 从 PEP 503/691 响应解析出的同名同 SHA 作为正向确认，不以 uv 零退出代替；认证、冲突、checksum 漂移或查重不能确认的不确定部分上传要求人工复核

#### Scenario: registry 重定向不会越过受审 endpoint
- **WHEN** upload 或 check endpoint 返回任意 30x 与 `Location`
- **THEN** relay 不跟随、不向目标重复发送认证或 distribution body，并使本次 artifact 进入人工复核清单

#### Scenario: publish uv 版本边界与审批身份
- **WHEN** plan 或 execute 使用低于 `0.11.29`、达到 `0.12`、无法解析或与已批准 plan 不同的 uv 版本
- **THEN** wrapper 在启动 relay、读取 credential 或发送网络请求前 fail closed

### Requirement: 正式发布物从 tag target 重新构建
系统 SHALL 在 release commit、annotated tag 与 provider release notes 创建后，从 tag target 的隔离、无 credential checkout 先按 frozen lock 准备并核对精确 build backend，再使用满足 `>=0.11.29,<0.12` 的实际 uv 执行 `uv build --no-build-isolation`，原子生成 `release-build/v1` manifest、wheel、sdist 与 `SHA256SUMS`。正式 manifest MUST 写入 `status: built`，并绑定 tag、tag target、package version、实际 uv 版本、build backend 名称/版本和每个 artifact 的 repo-relative path/kind/SHA-256/size；preview artifact 不得替代该正式构建。任何 consumer MUST 拒绝缺失 `status`、状态不为 `built`、uv 版本不在支持范围或 backend identity 与 frozen lock 不一致的 manifest。

#### Scenario: tag 后构建形成正式发布输入
- **WHEN** promotion 已创建指向 release commit 的 annotated tag 和同版本 release notes
- **THEN** builder 从该 tag target 以 `--group release --no-group license` frozen sync，核对并使用 lock 内精确 build backend 重新构建正式 wheel/sdist，分别读取 wheel `METADATA` 及 sdist `PKG-INFO`/`pyproject.toml`，验证两种包的版本均等于 tag/version 且均无 workspace/path dependency，并原子写出状态为 `built`、含实际 uv 与 backend identity 的 `release-build/v1`

#### Scenario: 非 built 正式构建不得授权下游
- **WHEN** `release-build/v1` 缺少 `status`、状态不是 `built`、uv 版本不在支持范围、缺少 backend identity 或该 identity 与 frozen lock 不一致
- **THEN** promotion receipt、registry plan/execute 与双 CI consumer 均在发布副作用前 fail closed，不得把该 manifest 或其 artifact 作为正式发布输入

#### Scenario: tag 后构建失败不授权 publish
- **WHEN** tag checkout、uv 版本范围、frozen build backend、wheel/sdist、checksum、版本或 workspace boundary 任一验证失败
- **THEN** promotion 写入 `failed` 回执并保留已确认 commit/tag/provider 身份供人工复核，不生成可供 registry plan 消费的 `promoted` 授权

#### Scenario: no-release 禁止 promotion
- **WHEN** manifest 状态为 `no-release`
- **THEN** plan job 原子生成 `status: no-release` 且无 approval 的 `release-promotion-plan/v1`，execute 消费同一 plan 后在任何 git/provider 副作用及 credential 要求前成功生成不可用于 publish 的 `no-release` 回执，不创建 commit、tag、release 或正式 build
