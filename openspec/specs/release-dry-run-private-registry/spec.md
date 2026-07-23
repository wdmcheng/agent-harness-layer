# release-dry-run-private-registry Specification

## Purpose
定义 Conventional Commits 版本预演、受保护 promotion、正式构建与私有 registry 发布的身份交接、授权边界和零副作用失败语义。
## Requirements
### Requirement: Conventional Commits 产生可解释版本预演
系统 SHALL 使用固定版本的 Python Semantic Release noop 路径读取 Conventional Commits，输出当前版本、下一 SemVer、tag 名称、扫描基线、提交分类和 bump 理由；无 tag 首版本计算 MUST 显式设置 `allow_zero_version = true`，不得依赖 PSR 10.6.1 默认的 `false`。

#### Scenario: releasable commits 计算下一版本
- **WHEN** 已发布基线后存在 `feat`、`fix` 或 breaking commit
- **THEN** preview 输出与 PSR 一致的下一版本和 tag，并逐条解释影响 bump 的提交

#### Scenario: 无 tag 仓库使用唯一 first-release 规则
- **WHEN** 仓库没有 release tag，且 PSR 在显式 `allow_zero_version = true` 下从 `0.0.0` 计算出的首版本等于当前 package version
- **THEN** preview 将当前 package version 作为首个发布候选，不再额外 bump；本仓库输出 `0.1.0` 与 `agent-harness-v0.1.0`

#### Scenario: 首版本配置默认值回归被阻断
- **WHEN** 合同测试移除 `allow_zero_version = true` 并使用 PSR 10.6.1 默认配置计算相同的无 tag `feat` 历史
- **THEN** 测试证明默认结果为 `1.0.0` 而受控配置结果为 `0.1.0`，wrapper 只接受后者作为本仓库首版本真相

#### Scenario: PSR executable 版本漂移在预演前被阻断
- **WHEN** PATH 或显式环境入口解析到的 `semantic-release` 不是精确 `10.6.1`
- **THEN** wrapper 在调用 noop、生成 preview 或修改隔离副本前 fail closed，不接受碰巧符合本地算法的伪输出

#### Scenario: 无 origin 的 noop 不访问网络
- **WHEN** 当前 checkout 没有 `origin`，且外部网络通过拒绝连接代理被封闭
- **THEN** wrapper 使用临时无 credential remote 配置执行 `semantic-release --config <temp> --noop version --print` 并完成版本计算

#### Scenario: 版本基线不一致时拒绝猜测
- **WHEN** package version、最新 release tag 与 PSR 计算无法形成可解释的单一版本链
- **THEN** dry-run 以非零状态停止，且不写 git ref、package version 或 release artifact

#### Scenario: shallow checkout 不得猜测版本基线
- **WHEN** `git rev-parse --is-shallow-repository` 返回 `true`，使历史 commit 或 release tag 可能不可见
- **THEN** wrapper 在扫描 commit、判定 first release 或调用 PSR 前非零失败，提示先获取完整 history/tags，且不得自行联网 unshallow

### Requirement: release artifact 在隔离副本中生成
系统 MUST 在不修改原工作树、git 历史或 refs 的临时副本中生成下一版本的 CHANGELOG preview、release notes、wheel、sdist 和 SHA-256 checksum，并输出机器可读 manifest。

#### Scenario: releasable dry-run 生成完整 artifacts
- **WHEN** 维护者对包含 releasable commits 的仓库执行 dry-run
- **THEN** artifact 目录包含版本/tag/提交解释、CHANGELOG preview、release notes、wheel、sdist、`SHA256SUMS` 和每个文件的 checksum

#### Scenario: dry-run 前后仓库身份不变
- **WHEN** dry-run 成功或在构建中失败
- **THEN** 原仓库的 HEAD、refs、tag、tracked source/diff/status 和外部 registry 均未被修改；只允许生成约定的 ignored `.artifacts/**`

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

### Requirement: 无 releasable commits 不产生 release
系统 SHALL 把没有 releasable commit 视为明确的 `no-release` 结果，不创建 tag、release、wheel/sdist 或 publish plan。

#### Scenario: 文档提交不触发发布
- **WHEN** 基线后只有 `docs`、`chore`、`test` 或不匹配 Conventional Commits 的提交
- **THEN** manifest 记录 `no-release` 和原因，tag/release/artifact 数量保持不变

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

### Requirement: 受保护 promotion 完成版本与 git release 生命周期
系统 SHALL 提供默认 plan-only 的 promotion seam；真实 execute 必须在全部质量门禁后，经显式人工批准、protected ref、匹配的 release manifest/checksum 和受限 provider credential，依次更新 package/template version 与 `CHANGELOG.md`、创建 release commit、annotated tag 和 provider release notes。

promotion plan MUST 以 `release-promotion-plan/v1` artifact 持久化。可发布路径必填 `status: planned`、去敏 provider/push endpoint 摘要、protected default branch、source/artifact identity 和动态 `approval_sha256`；`no-release` 路径必填 `status: no-release`、`tag: null` 和同一受审 source/preview identity，且不得包含 approval 或授权任何副作用。plan producer MUST 使用只读 checkout 且不读取 provider 或 git push credential，provider endpoint 也不得通过 URL userinfo 嵌入用户名或密码；protected/manual execute consumer MUST 下载同一 artifact，可发布路径拒绝缺失或非 `planned` 状态、从受限环境读取短期 provider 与 push credential，并把 artifact 中的动态摘要注入 wrapper；`no-release` consumer 只能无 credential 地写出不可发布回执。GitHub 使用 `persist-credentials: false` 时 MUST 显式安装只对冻结 push endpoint 生效的短期认证，执行结束后清理；GitLab MUST 明确短期或最小 scoped push credential 前置条件。wrapper MUST 在副作用前重新计算并核对计划身份。

#### Scenario: promotion 计划与执行分成两个 job
- **WHEN** 双 CI 准备执行真实 promotion
- **THEN** 只读无 credential 的 plan job 先生成状态为 `planned` 的可归档 `release-promotion-plan/v1`，protected/manual execute job 后续消费同一 artifact；execute 只有在 plan 状态、动态摘要、protected ref、provider credential 和冻结 push endpoint 的短期认证全部闭合时才允许写入

#### Scenario: provider endpoint 不得夹带 URL credential
- **WHEN** plan producer 收到 username-only 或 username/password userinfo 的 provider endpoint
- **THEN** 系统在生成计划、读取受限 credential 或执行任何副作用前拒绝，错误输出不得回显该 userinfo

#### Scenario: 缺少可用 push 认证时零副作用
- **WHEN** checkout 不持久化平台 credential，且 execute job 未建立绑定冻结 endpoint 的短期 push 认证
- **THEN** promotion 在修改版本、commit、tag、push 或 provider API 前失败，且日志与 artifact 不包含 credential

#### Scenario: 缺任一 promotion 门禁时零副作用
- **WHEN** execute 缺少批准、protected ref、clean tracked checkout、identity/checksum 或 provider credential
- **THEN** 系统在修改文件、commit、tag、push 或 provider API 前失败，日志不回显 credential

#### Scenario: 隔离替身验证完整 promotion
- **WHEN** 在一次性临时 git/bare remote 与 provider HTTP 替身中对匹配的 `release` manifest 执行 promotion
- **THEN** version/CHANGELOG、release commit、annotated tag、release notes 和 tag target 正式构建按固定顺序关联同一版本与 commit，生成 `release-build/v1` 及 `release-promotion/v1` `promoted` 回执；当前仓库和真实远端不受影响

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

### Requirement: promotion 回执绑定发布前后身份
系统 SHALL 以 `release-promotion/v1` 回执把 preview manifest SHA-256、原 source commit/diff、version、release commit、tag target、provider release、`release-build/v1` manifest SHA-256 与待上传正式 wheel/sdist checksum 绑定；registry 与 CI publish consumer MUST NOT 只根据 job 依赖、preview artifact 或 tag 名推断身份。

#### Scenario: promoted 回执字段闭合
- **WHEN** promotion 的 git 与 provider 步骤全部成功
- **THEN** receipt 状态为 `promoted`，tag target 等于 release commit，provider release 提供非空 ID 与合法 URL 并引用同一 tag/release notes，`release_build_manifest_sha256` 匹配正式 manifest，artifacts 与 `release-build/v1` 的 path/kind/SHA-256/size 完全一致

#### Scenario: 部分 promotion 不产生可发布授权
- **WHEN** commit、tag、push 或 provider release 任一步失败或结果不确定
- **THEN** receipt 状态为 `failed`；provider 返回 2xx headers 后截断响应体也属于结果不确定，回执必须保留已确认 commit/tag 身份，且 provider release 已确认后即使后续正式 build 失败也必须保留 provider ID/URL 供人工复核；registry/CI publish 拒绝执行且系统不自动重写公开 ref
