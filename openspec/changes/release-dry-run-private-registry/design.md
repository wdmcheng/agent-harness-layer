## Context

当前核心包和模板均为 `0.1.0`，根 workspace 通过 `tool.uv.sources` 把模板绑定到本地核心包；`make build` 只构建当前版本，仓库没有 tag 基线、CHANGELOG 自动生成、checksum 或 registry gate。设计必须同时满足 monorepo 本地验证与“发布物不携带 workspace path”的分发边界，并把所有可能修改 git/registry 的动作隔离在 dry-run 之外。

官方依据与 pin：

| 工具 | Pin | 选择依据与边界 | 官方来源 |
|---|---|---|---|
| Python Semantic Release | `10.6.1` | 同一 CLI 支持 GitHub/GitLab remote 与 Conventional Commits；每次预演先用 `semantic-release --version` 核验实际 executable 恰为 `10.6.1`，再只使用全局 `--noop` 的 `--config <temp> --noop version --print` / `--print-tag`，不调用会 commit/tag/push 的正常 `version` 路径 | https://python-semantic-release.readthedocs.io/en/stable/ 与 https://python-semantic-release.readthedocs.io/en/stable/api/commands.html |
| uv | `0.11.29` | 与现有 uv workspace、lock、`uv build`、`uv publish` 直接兼容；CI 固定 patch，避免把本机观察版本误当项目 pin | https://docs.astral.sh/uv/guides/package/ 与 https://docs.astral.sh/uv/guides/integration/github/ |
| licensecheck | `2026.0.8` | 由关联 license change 使用；与发布工具集中进入同一 lock，避免两个 change 争用根依赖 | https://pypi.org/project/licensecheck/ |

## Goals / Non-Goals

**Goals:**

- 用 PSR 的 noop 结果作为下一版本真相，并附提交分类与 bump 理由。
- 在临时副本中写下一版本、构建 wheel/sdist、生成 changelog/release notes/checksum，原工作树和 git refs 前后完全一致。
- 生成稳定的 `release-preview/v1` manifest 作为 Product-Spec `ReleaseRecord`；不创建运行时数据库表。
- 提供受保护的真实 promotion seam，使未来 hosted 流程能更新 version/CHANGELOG 并创建 release commit/tag/release notes；本轮只做临时仓库与 provider 替身验证。
- 生成 `release-promotion/v1` 回执，显式桥接 preview 的 source identity 与 promotion 后的新 release identity，避免 publish 只依赖 job 顺序猜测目标。
- 把发布计划与执行分成独立 CI jobs；版本化 plan artifact 冻结动态审批摘要，执行路径必须同时满足 protected ref、人工批准、凭据、HTTPS registry 和 artifact checksum 门禁。

**Non-Goals:**

- 不创建 `release_records` 数据库表；Phase 15 的发布证据是 CI artifact，不引入运行时持久化。
- 不支持公开 PyPI，不写真实 token，不自动创建远端 environment 或审批规则；当前任务不对真实仓库/远端执行 promotion 或 publish。

## Decisions

### 1. PSR 只负责版本判定，仓库 wrapper 负责无副作用预演

`scripts/release_dry_run.py` 在临时配置中写入不含 credential 的 `https://example.invalid/agent-harness/repository.git` remote seam，并调用固定环境中的 `semantic-release --config <temp> --noop version --print` 与 `--print-tag`。临时配置显式设置 `allow_zero_version = true`；PSR 10.6.1 的默认值为 `false`，会把无 tag 仓库的首个可发布版本提升到 `1.0.0`，与本仓库既定 `0.1.0` 首版本冲突。PSR 10.6.1 的 URL parser 要求 namespace 与 repository name 均非空，因此 `.invalid` seam 保留两段路径；单段 `/agent-harness.git` 会在 noop 前被判为 `Bad url`，不能作为无网络证据。`--noop` 是顶层参数，放在 `version` 子命令之后属于无效调用。测试通过拒绝连接的代理证明版本计算不访问网络；当前 checkout 没有 `origin` 也必须可运行。wrapper 随后用 `git log` 生成包含 commit SHA、类型、scope、breaking 标记和 bump 理由的 preview。选择 wrapper 而不是直接运行 PSR Action，是因为 PSR 正常 version 命令的职责包含写版本、CHANGELOG、commit、tag 和 release，不适合作为必须反复执行的验收命令。

wrapper 在读取 tag 或提交前必须以 `git rev-parse --is-shallow-repository` 证明 checkout 非 shallow；shallow 输入直接 fail closed，并提示 CI 先获取完整 history/tags，wrapper 本身不得隐式联网 unshallow。只有完整 history 中确实不存在 release tag 时才能进入 first-release bootstrap。该前置门禁防止 GitHub/GitLab 默认浅克隆把历史 releasable commit 或已有 tag 隐藏成 `no-release`/首发。

版本基线只有两种合法形态：已有 release tag 时，package current version 必须等于最新 tag 版本，PSR 从该 tag 计算下一版本；没有任何 release tag 时进入 first-release bootstrap，PSR 在显式 `allow_zero_version = true` 下以 `0.0.0` 为基线计算的首版本必须等于当前 package version。本仓库当前无 tag，预期首版本为 `0.1.0`、tag 为 `agent-harness-v0.1.0`；不相等就 fail closed，不把当前 package version再额外 bump 一次。合同测试还必须证明移除该显式配置时 PSR 10.6.1 会得到 `1.0.0`，从而锁住上游默认值变化的回归边界。

替代方案 release-please 被拒绝：它偏 GitHub PR 模型，无法自然复用到 GitLab；两套 CI 会得到不同底层真相。

### 2. 构建发生在受控临时副本

releasable 路径把核心包所需文件复制到临时目录，仅在副本中把 `project.version` 改为下一版本，然后用固定 uv 构建到 repo 内已忽略的 `.artifacts/release-preview/<identity>/dist/`。wrapper 记录运行前后的 `HEAD`、refs、tracked diff 与 `git status --porcelain --untracked-files=no`，任何 tracked source 或 ref 变化均使 dry-run 失败；允许新增约定的 ignored `.artifacts/**` 输出。wheel METADATA 和 sdist `pyproject.toml` 都必须验证只含正常版本范围，不含 `workspace = true`、`file://`、绝对路径或仓库相对 path source。

该构建只证明 AC-055 的预演结果，不是 registry 上传输入。真实 promotion 按 Product-Spec `FLOW-005` 先更新版本/CHANGELOG、创建 release commit、annotated tag 与 provider release notes，再从 tag target 的隔离无 credential checkout 运行相同固定 uv，原子生成 `.artifacts/release-build/<identity>/manifest.json`、`dist/` 与 `SHA256SUMS`。`release-build/v1` 必填 `status: built`，并绑定 tag target、版本、工具版本和正式 artifact checksum；promotion receipt、registry 与 CI consumer 对缺失/其他状态 fail closed。publish 只消费该 manifest，避免把 tag 前临时副本误当成正式来源。

直接临时修改再恢复工作树被拒绝：中断时可能留下脏状态，也无法证明无历史副作用。

### 3. no-release 是成功的显式状态

无 `feat`、`fix`、breaking 等 releasable commit 时，preview 仍生成 `manifest.json`，状态为 `no-release`，说明扫描范围与原因；不生成 tag、wheel/sdist 或 publish plan，并以成功退出让 CI 区分“无需发布”和“工具故障”。

### 4. `ReleaseRecord` 是版本化 CI artifact

`manifest.json` 的 `schema_version` 固定为 `release-preview/v1`，必填字段为 `status`、`source.commit_sha`、`source.dirty_diff_sha256`、`source.base_tag`、`current_version`、`decision.bump`、`decision.reason`、`decision.commits[]` 和 `artifacts[]`。`release` 状态还要求非空 `next_version`、`tag`、CHANGELOG/release-notes 引用以及 wheel/sdist/checksum 条目；`no-release` 时这些发布字段为 `null` 且 `artifacts` 只允许 manifest 自身以外的诊断引用。每个 artifact 记录 repo-relative path、kind、SHA-256 与 size，不写绝对路径、credential 或运行时 tenant。

manifest 是 Product-Spec `ReleaseRecord` 的唯一落地，不创建 `release_records` 表，不新增 migration/repository/UoW，也不让 dry-run 连接 SQLite/PostgreSQL。schema major 不匹配时 registry/CI consumer 必须 fail closed。

### 5. promotion 与 registry publish 分权

`scripts/release_promote.py` 消费受审 `release-preview/v1`，默认只输出去敏 promotion plan。真实执行必须同时具备 `--execute`、`RELEASE_PROMOTION_APPROVED=true`、protected default branch、clean tracked checkout、与 manifest 完全一致的 commit/diff/checksum identity 和短期 provider credential。执行顺序固定为：在受控 release checkout 更新 package/template version 与 `CHANGELOG.md`，创建 release commit，创建 manifest 指定的 annotated tag，再用同一 release notes 创建 provider release；任一步不确定即停止，不自动删除或重写已经公开的 ref。

promotion 始终在 `.artifacts/release-promotion/<identity>/receipt.json` 写 `release-promotion/v1` 回执。公共字段为 `status`、preview manifest 的 SHA-256、原 `source.commit_sha`/`source.dirty_diff_sha256`、version 与 artifacts；成功状态 `promoted` 还必须记录 release commit SHA、tag、tag target SHA、release notes SHA-256、provider、去敏且非空的 provider release ID 与合法 HTTPS（test mode 可为 loopback HTTP）URL，以及 `release-build/v1` manifest SHA-256。tag target 必须等于 release commit，artifacts 必须逐项复用正式 build manifest 中 wheel/sdist 的 repo-relative path、kind、SHA-256 与 size。`no-release` 和 `failed` 用于可诊断终态，但都不是 publish 授权；partial provider/build 结果、缺失/非法 identity 以及 2xx headers 后响应体截断等未知结果都写 `failed`，并保留已确认 release commit/tag 身份供人工复核，禁止将其伪装为可重试成功。

GitHub `promote` job 单独授予 `contents: write` 且不读取 registry credential；后续 `publish` job 恢复 `contents: read`，只读取 registry credential。GitLab 使用 protected environment 下的最小 scoped project access/deploy credential 完成 repository/tag/release promotion，package publish 使用受保护、短期或最小 scoped credential；只有 registry 明确支持且项目已显式开启并验证 job token 发布权限时，才允许使用 `CI_JOB_TOKEN`。普通 pipeline 两类 credential 均不可见。本轮禁止真实远端副作用，因此只在一次性临时 git/bare remote 与 provider HTTP 替身执行完整 promotion，并在测试后删除；hosted promotion 保持未验证。

promotion 在 CI 中拆为 `plan -> protected/manual execute`。plan job 使用只读、非持久化 credential 的 checkout，运行 wrapper plan-only 并原子写出 `release-promotion-plan/v1`。可发布输入生成 `status: planned`，并包含去敏 approval payload、`approval_sha256`、preview checksum、source identity、protected default branch、push endpoint SHA-256 和 provider endpoint identity；无版本变化输入生成零授权 `status: no-release`，保留稳定 preview/source identity 与 `tag: null`，且不得包含 approval。provider URL 必须是无 userinfo 的 HTTPS endpoint，username-only 与 username/password 形式都在计划生成前 fail closed。promotion consumer 拒绝字段不完整或其他状态：`planned` execute 只能从该 artifact 读取本轮摘要，不能在 YAML 中固定或由操作者抄写；`no-release` 节点只生成零副作用回执，不读取发布凭据。wrapper 会根据下载的 preview、当前 checkout、provider URL、default branch 与实际 origin push endpoint 重新计算 planned 摘要。GitHub 在 `persist-credentials: false` 下由 protected environment 提供短期 push token，并仅在 execute 期间通过临时 credential helper 绑定冻结 HTTPS host，结束后删除 helper；provider token 与 push token 分权。GitLab execute job 同样要求受保护、最小 scoped 的 push credential，不能把 `CI_JOB_TOKEN` 可写能力当作未经验证的默认事实。

### 6. 私有 registry 采用双重显式授权

registry wrapper 默认只输出去敏计划。执行必须同时具备 CLI `--execute`、`REGISTRY_PUBLISH_APPROVED=true`、protected ref 证明、HTTPS `UV_PUBLISH_URL`、同 registry 的 HTTPS `UV_PUBLISH_CHECK_URL`、受支持身份、匹配的 `release-preview/v1`、`release-build/v1` 与状态为 `promoted` 的 `release-promotion/v1`。两个 endpoint 必须是不含 userinfo、query 或 fragment 的纯 HTTPS 路由；其摘要都进入审批身份，防止查重与上传在审批后漂移。wrapper 在网络前验证 preview/build manifest SHA-256、source identity、version、release commit/tag target 和待上传正式 artifact checksum 闭合；`no-release`、`failed`、陈旧 receipt 或任一漂移均 fail closed。凭据只从进程环境读取，禁止命令行、URL、配置文件和日志回显。GitHub 使用 protected environment secret；GitLab 使用受保护、短期或最小 scoped 的 registry credential，只有项目显式开启并验证对应发布权限时才允许使用 `CI_JOB_TOKEN`。本地替身只在显式 test mode 允许 loopback HTTP 和假 token。

实际上传只调用固定版本 `uv publish`，使用标准 Python package index multipart 协议；token 只通过 `UV_PUBLISH_TOKEN` 环境变量传入，不出现在 argv。固定 uv 会跟随 30x 并重复发送 distribution，因此 wrapper 在同一进程内创建只监听 loopback 的短命 relay：uv 的 upload/check URL 只指向 relay，relay 只向审批时冻结的 HTTPS upload/check endpoint 转发对应方法、认证和原始 body，使用禁止重定向的 HTTP client；任一 30x 都不转发 `Location`、不重放 body，并立即归为人工复核失败。relay 不实现自定义上传协议，只隔离 transport；外部 registry 收到的仍是 uv 生成的标准 multipart metadata 与原 distribution bytes。

wrapper 把 uv 内部 HTTP retry 设为零并在外层最多执行三次；但 `--check-url` 在 upload 回包未知后仍可能于同一 uv 进程内再次 POST，甚至在空 check 后以零退出，因此 relay 还必须在连接未知、2xx headers 后响应体截断、202 或 upload 30x 后锁死后续外部 POST。relay 自行解析 PEP 503 HTML 或 PEP 691 JSON，只有冻结 check endpoint 明确给出同名同 SHA-256 且 uv 未重发，才能确认为已上传；不能只信任 uv 退出码。check 为空、连接失败或无法确认时立即停止并输出人工复核步骤。对确定的 429/5xx，只有失败后的 check 明确成功且仍未找到同名同 checksum，外层才可有界重试相同冻结 bytes。认证失败、400/409、30x、checksum 漂移或查重结果不能确认时不删除或覆盖 registry 资产。此边界与 uv 官方 `--check-url` 对中途失败、并行重复上传和相同文件 SHA-256 查重的说明一致。

registry CI 同样拆为 `plan -> protected/manual execute`。plan job 不读取 token，原子写出 `registry-publish-plan/v1`，包含 `status: planned`、preview/promotion receipt/正式 build manifest checksum、release/tag/artifact identity、upload/check endpoint identity、protected tag ref 和动态 `approval_sha256`。execute job 只从 protected environment 读取 token，拒绝缺失或非 `planned` 状态，从 plan artifact 读取本轮摘要，再由 wrapper 重新计算；plan artifact 的 schema、checksum 或任何输入漂移都在启动 relay/uv 前失败。

## Affected Surfaces

- `pyproject.toml`、`packages/agent-harness/pyproject.toml`、`templates/service-app/pyproject.toml`、`uv.lock`
- `.gitignore` 的统一 `.artifacts/` 规则；后续 license/CI changes 只消费该目录，不再争用文件所有权
- `scripts/release_dry_run.py`、registry publish wrapper 与共用 release models
- `scripts/release_promote.py` 与 provider promotion adapter
- `CHANGELOG.md`、`release-preview/v1`、`release-promotion-plan/v1`、`release-build/v1`、`release-promotion/v1` 与 `registry-publish-plan/v1` artifact 结构
- release contract tests；不修改 runtime API、schema 或数据库 migration

## Testing Seams

- CLI：`python scripts/release_dry_run.py --output-dir ...`，输入为真实或临时 git repo。
- artifact contract：`manifest.json`、`CHANGELOG.preview.md`、`release-notes.md`、`dist/*`、`SHA256SUMS`。
- no-network/version bootstrap：无 `origin`、拒绝连接代理、无 tag 首版本和已有 tag 增量版本；对比显式 `allow_zero_version = true` 的 `0.1.0` 与 PSR 10.6.1 默认配置的 `1.0.0`，防止遗漏首版本配置。
- Git history：从带历史 tag 的本地 bare remote 创建真实 depth-1 clone，证明 wrapper 在 shallow 状态拒绝；unshallow 且 tag 可见后才按正确基线计算。
- package boundary：wheel METADATA、sdist 内容和 workspace 外安装。
- registry CLI：plan、缺 gate、错误 credential、批准后 upload/check endpoint 分别漂移、transient retry、30x、preview/receipt identity 或 checksum drift、非 `promoted` 状态，以及由真实 `uv publish` 经受限 no-redirect relay 驱动的 loopback package-index multipart/upload/check receipt。
- promotion CLI：缺 gate fail closed；一次性临时仓库/bare remote 与 provider 替身验证 version/CHANGELOG/release commit/tag/release notes/tag target 正式构建的顺序、identity、`release-build/v1` 和 `release-promotion/v1` 回执，本轮当前仓库/真实远端零副作用。
- CI 审批交接：plan artifact 的动态摘要、schema/checksum、protected/manual consumer、credential 分权和短期 push 认证；任何静态摘要、缺 secret 或 plan 漂移都在副作用前失败。
- dry-run side-effect boundary：运行前后当前仓库 HEAD、refs、tracked diff 和 tracked status 相同；只允许新增约定的 ignored `.artifacts/**`。

## Risks / Trade-offs

- [历史没有已发布 tag 时版本基线可能歧义] → 临时配置必须显式设置 `allow_zero_version = true`，并只接受 PSR 从 `0.0.0` 计算出的首版本等于当前 package version；本仓库为 `0.1.0`，不相等即 fail closed。
- [PSR 输出格式升级] → exact pin、结构化 parser contract 和错误输出保留；升级必须改 pin并重跑两路径。
- [registry 中途只上传部分文件] → 只允许相同 checksum 重试；不自动回滚或覆盖，输出已确认/未确认文件清单。
- [promotion 完成后 publish 仍引用旧 source 或 preview 产物] → 以 `release-promotion/v1` 把 preview checksum、release commit、tag target、provider release、正式 build manifest 和待上传 artifacts 绑定，publish 不从 job 顺序或 preview 路径推断身份。
- [临时构建遗漏源文件] → wheel/sdist workspace 外安装和 smoke contract 同时验证。

## Migration Plan

先加入 `.gitignore` 的统一 `.artifacts/` 规则、工具 pin 和 dry-run/promotion/registry wrappers，再把模板依赖从 workspace 验证用 exact 表达调整为发布兼容范围；本地 workspace 继续由根 `tool.uv.sources` 覆盖，不改变开发体验。真实 hosted 顺序固定为 required gates → dry-run → manual protected promotion → private registry publish。回滚只需删除新脚本/配置并恢复依赖声明；本轮无数据库或真实远端资产迁移。

## Open Questions

无阻塞问题。实际 registry 厂商、hosted environment reviewer 和组织许可选择留给未来运营配置；当前合同通过通用私有 PyPI seam 与本地替身验证。
