## Context

两个前置 changes 将提供 release preview 和 license report，但仓库仍缺 `make integration`、独立 CI gate、artifact 目录和两套真实 pipeline。设计重点不是复制两份 shell，而是让 GitHub/GitLab 只编排同一组仓库入口，并能用本地 runner 区分“YAML 合同正确”“job 在容器中真实执行”“hosted 环境未验证”三种证据层级。

官方依据与 pin：

| 组件 | Pin | 选择依据 | 官方来源 |
|---|---|---|---|
| `actions/checkout` | `9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0` (`v7.0.0`) | uv 官方当前集成示例使用 v7；完整 SHA 防 tag 漂移，只需 `contents: read`；官方说明默认只取单一 commit，release jobs 必须 `fetch-depth: 0` 获取全部 history/tags | https://github.com/actions/checkout/releases/tag/v7.0.0 与 https://github.com/actions/checkout |
| `astral-sh/setup-uv` | `08807647e7069bb48b6ef5acd8ec9567f424441b` (`v8.1.0`) + uv `0.11.29` | uv 官方推荐 action 与 exact uv pin，保留 workspace/lock 语义 | https://docs.astral.sh/uv/guides/integration/github/ |
| `actions/upload-artifact` | `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` (`v7.0.1`) | 完整 SHA 与明确 retention；归档每个独立 gate 的证据 | https://github.com/actions/upload-artifact/releases/tag/v7.0.1 |
| GitLab CI runtime | `ghcr.io/astral-sh/uv:0.11.29-python3.12-trixie-slim@sha256:36cdfbf910c8b0f651355c013e7ece9678f4ecbf030a9fd9e6779de421189805` | 固定 uv、Python 3.12、发行版和 multi-arch OCI index digest；使用 `needs`、artifacts、rules、manual/protected environment | https://docs.astral.sh/uv/guides/integration/gitlab/ 与 https://docs.gitlab.com/ci/yaml/ |
| 本地 runner | `act 0.2.88`、`gitlab-ci-local 4.73.0` | 真正启动 workflow/job 容器与依赖，不把静态解析当运行 | https://nektosact.com/ 与 https://github.com/firecow/gitlab-ci-local |

## Goals / Non-Goals

**Goals:**

- 两套 CI 的 job/DAG/触发与 artifact 语义等价，所有业务命令来自同一组 Make targets。
- quality 子项、测试层级、service smoke、build、license、release preview 均有独立结果和稳定 artifact。
- 本地/容器 runner 实际执行能证明仓库 gate 的成功、失败与阻断；artifact 路径和上传配置由静态合同证明，本地 backend 支持时再补充传递证据，同时如实保留 hosted runner 未验证边界。

**Non-Goals:**

- 不用本地 runner 假冒 GitHub/GitLab SaaS 的计费、权限后端、environment reviewer 或 artifact service。
- 不 push、不创建远端 pipeline，不将 publish job 默认设为自动。

## Decisions

### 1. Make/脚本是唯一业务入口，CI YAML 只编排

根 Makefile 增加 lock、独立 quality、unit-contract、integration、evidence、release 与 CI contract targets；两套 pipeline 都必须有独立 `quality-aggregate` 与 `test-aggregate` job，分别真实执行 `make quality` 与 `make test`。ruff format/lint、pyright、import boundary、unit/contract 等细粒度 jobs 仍独立运行并产出结果；聚合 job不能替代这些子项，子项也不能替代 AC-051 明文要求的两个聚合命令。通用 evidence runner 捕获 stdout/stderr、exit code、开始/结束时间、命令 identity 与相对 artifact 路径，失败仍写 result 后返回原状态；同名 gate 重跑时先撤下旧 `result.json`，使运行中或中断状态不能被上一轮终态冒充。

YAML 内重复 pytest/ruff 参数被拒绝：它会让两套 pipeline 逐渐漂移，也无法在本地复用。

### 2. 等价 DAG 以 job contract manifest 校验

仓库维护一份 job contract，声明 trigger、job、Make target、needs、artifact glob、权限和 failure policy。合同测试解析 GitHub/GitLab 配置并要求它们映射到同一语义集合；随后 local runners 启动真实 job 容器并执行仓库 Make gate。静态合同发现结构漂移，runner 证明命令和容器环境能执行仓库入口，两层缺一不可；本地 artifact backend 不兼容只限制上传服务证据，不推翻已取得的仓库 gate 退出状态。

job contract 还必须声明 release 输入为完整 history/tags。GitHub 所有可能执行 release dry-run/promotion 的 checkout 固定 `fetch-depth: 0`；GitLab 对应 jobs/pipeline 固定 `GIT_DEPTH: "0"`，覆盖新项目默认 depth 20。进入 release wrapper 前再断言 `git rev-parse --is-shallow-repository=false` 且预期 release tag 可见；配置与运行时门禁任一缺失都阻断。官方依据：https://github.com/actions/checkout 与 https://docs.gitlab.com/ci/pipelines/settings/ 。

固定的 uv slim 镜像不自带 `make` 与 `git`，因此 GitLab `default.before_script` 按 `compliance/ci-jobs.toml` 声明安装 `ca-certificates`、`git`、`make`，并在 `smoke-service` job 额外安装 `docker.io`、`docker-compose` 及执行版本探针。该 bootstrap 只提供 CLI；真实 Docker daemon/Compose socket 仍是受保护 runner 的明确前置条件，不能由 SQLite 替代。合同测试会拒绝删除或漂移这些工具前置，防止容器内以 127 失败才发现配置缺口。

### 3. evidence 分层且不跨 job 猜成功

每个已终结 gate 写 `.artifacts/ci/<gate>/result.json` 和 log；同名 gate 从启动到终结之间旧 `result.json` 必须不可见，若 runner 中断则保持无结果而不是回退到陈旧终态。`AC-050` 固定映射独立终态 `p0-validate`；该 producer 启动时由 evidence runner 注入当前 gate 身份，validator 仍先校验 AC-050 的路径和 producer，只对本次尚未原子落盘的自身 result 内容做一次受控自举，其他缺失 evidence 一律失败。tests 另有 JUnit/coverage，eval 固定写 `.artifacts/eval/scores.jsonl` 与 `.artifacts/eval/traces.jsonl`，local/service smoke 分别固定写 `.artifacts/smoke/local/trace.jsonl` 与 `.artifacts/smoke/service/trace.jsonl`，build/license/release 保留各自原生 artifact。`compliance/ci-jobs.toml` 不允许 `native_artifacts_pending`；validator 要求这些原生产物同时进入 job manifest、`ci-result/v1` checksum、GitHub upload 和 GitLab artifacts。release dry-run job `needs` 所有 required gates；任一失败时 runner 不调度 release。artifact upload 使用 `if: always()`/`when: always` 保存失败诊断，但这不改变 job 的失败状态。

通用 result 使用 `ci-result/v1`：必填 `schema_version`、`gate`、`status`、`command`、`exit_code`、`input_identity.commit_sha`、`input_identity.dirty_diff_sha256` 和 `artifacts[]`；producer 与 matrix validator 必须调用 `scripts/ci_identity.py` 的同一公开算法，禁止各自复制摘要逻辑。`dirty_diff_sha256` 同时覆盖 tracked binary diff 与所有未忽略的 untracked 文件路径、mode 和 bytes，确保未提交 change 也能绑定 frozen evidence，而 `.artifacts/` 不污染身份。状态只允许 `pass`、`fail`、`skipped`。artifact 条目只含 repo-relative path、kind、SHA-256、size 与 producer gate。release 原生 artifact 必须是 `release-preview/v1`、`release-promotion-plan/v1`、`release-build/v1`、`release-promotion/v1`、`registry-publish-plan/v1`，license artifact 必须是 `license-report/v1`；major、必填 status 或 checksum 不匹配时 CI contract fail closed。

### 4. service smoke 使用真实 Docker service 边界

GitHub job 使用具备 Docker 的 runner；GitLab job使用受控 Docker daemon/service 或明确要求的 runner capability，运行现有 `make smoke-service`，不能用 SQLite/local 替代。local runner 绑定隔离 Compose project 与临时 volume，完成后按既有脚本定向清理。

### 5. planned release 使用四阶段分权门禁，no-release 使用无凭据终止分支

CI/merge request/push 默认只运行 dry-run。可发布输入的 release workflow 使用 `promote-plan -> promote-execute -> publish-plan -> publish-execute`：plan jobs 使用只读、非持久化 checkout，不读取 push/provider/registry credential，分别原子写出状态为 `planned` 的 `release-promotion-plan/v1` 与 `registry-publish-plan/v1`；execute jobs 绑定 manual/protected environment/ref，下载同一 plan artifact，只从 artifact 取得动态 `approval_sha256`，并在副作用前按实际 checkout、preview/build/receipt、endpoint 与 protected ref 重新计算摘要。`promote-execute` 在 tag/release notes 后从 tag target 原子生成 `release-build/v1` 正式构建，后续 publish jobs 明确拒绝 preview wheel/sdist。

GitHub plan job 暴露由同一 plan artifact 计算的 `release_required`：`false` 只调度 read-only、无 environment/secret 的 `promote-no-release`，生成零授权回执后成功终止；`true` 才允许 `promote-execute` 及 registry jobs。GitLab 父 pipeline 的无凭据 plan job 生成动态 child config：`no-release` child 只实例化无 environment/credential 的回执 job；`planned` child 才实例化 release/private-registry protected environment 下的人工 execute 与 publish 链。这样 no-release 全路径不会提前绑定发布凭据，也不会因缺正式 build 而误入 registry plan。

GitHub `promote-execute` 在 `persist-credentials: false` 下只在受保护 environment 内获得 `contents: write`、provider credential 与绑定冻结 HTTPS host 的短期 push credential；临时 credential helper 仅存活于 execute，结束后删除，且该 job 看不到 registry secret。`publish-execute` 恢复 `contents: read`，只读取 registry credential。GitLab planned child 的两个 execute jobs 使用 protected default branch/environment 和受保护、最小 scoped credential；promotion 不把 `CI_JOB_TOKEN` 的 Git push 能力当作默认事实。两套 CI 都必须拒绝缺 credential、`failed`、陈旧 plan/receipt 或 preview/release/artifact/endpoint identity 漂移；防御性 registry consumer 收到 `no-release` 仍须拒绝，但正常 no-release DAG 根本不调度 registry job。仓库文档列出必须在平台 UI 配置的 reviewer/secret/protected ref，合同测试与本地替身只能验证配置、门禁和命令顺序，不能宣称远端已配置或已执行。

### 6. ready-to-archive 与 hosted PASS 分开记录

本 change 的 `ready-to-archive` 只表示仓库实现、合同测试、容器化 local runner 中的仓库 Make gates 和本地验证证据已完成；默认仍要求 frozen review。用户已对本次 Phase 15 明确作出一次性最终审查豁免，因此本轮以 `owner-waived` 记录本地 ready-to-archive，不把未执行的 Reviewer 2/3 写成 PASS，也不改变后续 Phase 的默认审查规则。artifact service 不属于本地 ready-to-archive 的验收依赖。由于本轮禁止 push 或修改远端 CI，GitHub/GitLab hosted runner、artifact service、environment reviewer、protected ref 与 secret 配置保持 `hosted-unverified`。`AC-053`/`AC-054` 在 Product-Spec 中不得因本地结果勾选，P0 matrix 必须把本地可证部分与 hosted 未验证部分拆开。该限制不阻止 change 达到本地授权范围内的 ready-to-archive，但严禁写成 hosted PASS、已发布或已归档。

本 change 同时修改 `maintainer-documentation` 的事实边界：文档以当前代码、测试、配置和锁文件为准，可把已落地并通过本地验证的 CI/release seam 描述为当前能力；hosted runner、artifact service、environment reviewer、protected ref、secret 和真实 provider/registry 执行在缺少远端证据时仍必须标为未来或未验证。该修改只纠正“全部 Phase 15 尚未实现”的过期主规格，不扩大本 change 的实现范围。

## Affected Surfaces

- `Makefile`、CI evidence/contract scripts
- `.github/workflows/ci.yml`、`.github/workflows/release.yml`、`.gitlab-ci.yml`、`.gitlab/release-child.yml`
- `tests/contracts` 中 pipeline、artifact、P0 matrix 合同
- `.artifacts/**`（复用前置 release change 已建立的忽略规则；本 change 不修改 `.gitignore`）
- `docs/p0-acceptance-matrix.md` 与最终状态文档

## Testing Seams

- Make public targets：每个 gate 的成功/失败 exit 与 artifact。
- Pipeline contract：trigger、jobs、needs、Make targets、artifact、aggregate + 细粒度结果、GitHub `fetch-depth: 0` / GitLab `GIT_DEPTH: "0"`、非 shallow/tag 可见性、四阶段 release DAG、plan `status: planned` 与动态摘要、tag 后 `release-build/v1` 正式构建、execute credential/权限、GitHub 短期 push 认证、`release-promotion/v1` 身份交接与 manual/protected gates。
- 原生产物合同：拒绝 `native_artifacts_pending`，并逐项比较 eval/local smoke/service smoke 的稳定路径是否同时存在于 manifest、`ci-result/v1` checksum 和两套 CI artifact 上传集合。
- Local runner：`act` 与 `gitlab-ci-local` 实际启动 job 并执行仓库 Make gate，失败 fixture 证明阻断；本地 backend 支持时补充 artifact 传递。若 artifact server 协议不兼容或 runner 只复制 Git 已跟踪文件，必须标明具体 local-runner 限制，不能写成整个 job PASS，也不能用它否定已经真实返回的仓库 gate 状态。
- shallow fixture：从本地 bare remote 建立 depth-1 clone，证明两套 checkout 合同补全 history/tags；若仍 shallow 或历史 tag 不可见，release job 在 dry-run 前失败。
- P0 matrix validator：Product-Spec 中全部 P0 REQ/AC 必须唯一映射 production、CI job、test 和 evidence path；命令型和环境型验收还必须显式约束真实 producer，例如 AC-001 绑定执行 `uv sync --frozen` 的 `install`、AC-002 绑定执行 `uv build` 的 `build`、AC-003 绑定 workspace 外安装 wheel 的 `integration`，AC-004/061 绑定真实 import 扫描，AC-019/023/026/029/052/062 绑定各自实际 tenant、deny audit、MCP、fake eval 和跨边界关联行为，AC-011/012/068 绑定真实 PostgreSQL 服务的 `smoke-service`，不能由只运行 pytest、只声明禁止集合或会在 clean runner 跳过服务测试的聚合 gate 冒充。独立 `p0-validate` 在 clean runner 必须显式下载或继承矩阵所需的全部 producer evidence，包括 `install`、`integration` 与 `build`。
- 真实服务：现有 PostgreSQL/Redis Compose smoke，不引入替代实现。

## Risks / Trade-offs

- [很多独立 jobs 增加 CI 时间] → 共享 uv cache，但不合并质量结果；正确归因优先于少量时长。
- [local runner 与 hosted runner 差异] → 最终报告分开列证据，hosted 未 push 时始终标记未验证。
- [artifact 上传在失败时掩盖状态] → upload 使用 always 但 gate job 保留原退出码；release `needs` 只接受 required jobs 成功。
- [GitLab Docker-in-Docker 权限依赖 runner] → 文档列出 privileged/daemon 前置条件，本地用 Docker socket 只作为无远端副作用验证。

## Migration Plan

先实现前置 changes，再增加 Make/evidence 入口和 pipeline contract tests，最后写两套 YAML、跑 local runners、生成 P0 matrix 并同步状态。回滚删除新增 CI/入口即可；不得删除前置 release/license 能力来掩盖 pipeline 失败。

## Open Questions

hosted runner、GitHub environment reviewer、GitLab protected environment 和真实 registry 均因禁止 push/远端修改保持未验证；最终必须作为边界列出，而不是在本 change 中猜配置。
