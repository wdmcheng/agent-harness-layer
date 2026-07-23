# 构建、合规与发布边界

[English](release-process.md) | [简体中文](release-process.zh-CN.md)

适用读者：准备交付构建产物的 scaffold maintainer，以及需要判断复制模板是否达到生产发布条件的 app developer。

导航：[根 README](../README.zh-CN.md) · [架构边界](architecture/README.zh-CN.md) · [安全策略](security-policy.zh-CN.md) · [Eval/Observability](eval-observability-loop.zh-CN.md) · [Redis ADR](adr/0003-redis-runtime-license-policy.zh-CN.md)

## 结论先行

当前 checkout 能人工执行质量、测试、eval、local/service smoke、wheel/sdist 构建、fail-closed license 检查、双 CI contract 和 release dry-run。仓库已提供 GitHub Actions、GitLab CI、版本计算、CHANGELOG/release notes 预览、artifact 交接以及默认 plan-only 的 promotion/private registry seam；Hosted runner、远端 environment protection、真实 provider/tag/release/publish 仍未验证且本轮不执行。

`make build` 使用官方 `uv build` 能力生成本地 wheel/sdist；它不发布。本仓库的 registry、credential、审批与发布门禁只通过默认无副作用的 `make registry-publish-plan` 和受保护的 `make registry-publish-execute` 路径开放；维护者不得绕过 wrapper 直接运行裸 `uv publish`。参考 [uv 官方 package guide](https://docs.astral.sh/uv/guides/package/)。

## 当前人工门禁

从干净 checkout 按顺序执行：

```bash
uv sync
uv lock --check
make quality
make test
make integration
make eval
make smoke-local
# 需要可用的 Docker daemon/Compose；启动真实 PostgreSQL、Redis、migration、API、worker：
make smoke-service
make build
make license-check
make ci-contract-check
make release-dry-run
uv run pre-commit run --all-files
```

| 门禁 | 当前证明 | 不证明 |
|---|---|---|
| `make quality` | format、lint、type、import boundary | runtime integration |
| `make test` | unit/contract/离线 integration | 真实 PostgreSQL/Redis/DBOS 跨进程 |
| `make integration` | 独立 integration suite 与 JUnit/coverage 证据 | service profile 或 hosted runner |
| `make eval` | approved fake-model cases | 生产模型质量或自动 release acceptance |
| `make smoke-local` | SQLite/in-memory/fake model/local JSONL | service profile |
| `make smoke-service` | wheel-only Compose、真实 auth/secret/PostgreSQL/Redis/API/worker/recovery/SSE | 生产部署、容量和高可用 |
| `make build` | `dist/` 中 wheel/sdist 可构建 | 已签名、已上传或可回滚发布 |
| `make license-check` | 根 Apache-2.0 文件、`uv.lock` runtime closure、`compliance/third-party.toml`、`licensecheck` observation、仅补空值/`UNKNOWN` 的版本绑定 PyPI 官方观察快照、NOTICE、vendoring 与 pinned image identity，并写入 `.artifacts/license/license-report.json` | 法律意见、完整 SBOM 或 hosted registry 许可审查 |
| `make ci-contract-check` | 两套 pipeline 的触发、依赖、artifact、权限与共享入口合同 | hosted runner 已执行 |
| `make release-dry-run` | 下一 SemVer、tag、CHANGELOG/release notes preview、wheel/sdist 与 checksum，或显式 `no-release` | commit/tag/release/publish 已发生 |

失败即停止。不要在 service smoke 失败时用 local 结果替代；不要在 license check 通过后声称完成了依赖许可证法律审计。

本地执行必须使用仓库固定的 uv `0.11.29`。若宿主设置了 HTTP 代理，`127.0.0.1`/`localhost` 必须进入 `NO_PROXY` 与 `no_proxy`；否则 service smoke 的宿主 HTTP 请求可能被代理截获并返回 HTML 503，而容器内 API 仍健康。2026-07-22 的归档候选已在 `NO_PROXY=127.0.0.1,localhost` 下完整通过真实 service smoke 并生成 service trace；这证明本地 service gate，不证明 hosted runner 或生产网络配置。

## 版本真相

| 资产 | 权威来源 | 当前表达规则 |
|---|---|---|
| Python dependency declaration | root/package/template `pyproject.toml` | 写声明范围或 exact pin |
| Python dependency resolution | `uv.lock` | 用 `uv lock --check` 与 `uv tree --locked` 复核实际解析版本 |
| Docker runtime | `templates/service-app/docker-compose.yml` 与 `compliance/third-party.toml` | PostgreSQL `18.4` 与 Redis `7.2.14` 使用完整 OCI index digest；实际 server 版本仍须由 service smoke 记录 |
| uv CLI | 根 `pyproject.toml`、GitHub setup action、GitLab image | 精确固定 `0.11.29`；本地、两套 CI、release wrapper 与 `uv publish` 必须使用同一版本，其他版本由 `required-version` fail closed |
| 其他外部 CLI | 当前开发机或 CI runner | Docker、Compose 等宿主工具未由 Python lock 固定；运行证据必须记录实际版本，不能称为项目 pin |
| 实际 server patch | 某次 service smoke 输出 | 只作为该次运行证据，不反向改写 Compose 声明 |

技术栈表与上述受控来源冲突时，修正文档，不偷偷升级 dependency、toolchain 或 image。uv 升级必须同步根约束、两套 CI pin、release 合同和 lock 验证；Docker/Compose 的 hosted runner 能力仍属于未验证边界。

## CI 与发布工具选择

下表记录本轮实际采用的版本或不可变 pin。版本号用于需要稳定 CLI 行为的仓库工具；GitHub Action 使用完整 commit SHA，GitLab 与 service runtime 使用 OCI digest，避免浮动 tag 在未审查时改变执行内容。

| 工具 | 版本或 pin | 选择依据与官方来源 | 与 `uv workspace` 的适配结论 |
|---|---|---|---|
| uv | `0.11.29` | [uv 0.11.29 metadata](https://github.com/astral-sh/uv/blob/0.11.29/pyproject.toml)、[`uv lock --check`](https://docs.astral.sh/uv/concepts/projects/sync/#checking-the-lockfile)、[build/publish guide](https://docs.astral.sh/uv/guides/package/)；根 `required-version`、GitHub setup、GitLab image 与 release wrapper 使用同一版本。 | workspace 开发继续使用根 `tool.uv.sources`；发布兼容性用 `uv build --no-sources`/workspace 外安装验证，确保模板声明和构建 metadata 不依赖本机 workspace path。 |
| python-semantic-release | `10.6.1` | [10.6.1 metadata](https://github.com/python-semantic-release/python-semantic-release/blob/v10.6.1/pyproject.toml) 与 [version command](https://python-semantic-release.readthedocs.io/en/latest/api/commands.html#semantic-release-version)；只复用 Conventional Commits 解析与下一 SemVer 计算，仓库 wrapper 负责无副作用 preview。 | 作为根 `release` dependency group 的 exact pin 进入 `uv.lock`，不进入核心包或模板 runtime dependency；release dry-run 不调用会写 commit/tag/release 的命令路径。 |
| `actions/checkout` | `9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0` | [官方 commit](https://github.com/actions/checkout/commit/9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0)；完整 SHA 固定 action 代码。 | 只提供 checkout；依赖解析仍由固定 uv 和仓库 lock 控制。 |
| `astral-sh/setup-uv` | `08807647e7069bb48b6ef5acd8ec9567f424441b` | [官方 commit](https://github.com/astral-sh/setup-uv/commit/08807647e7069bb48b6ef5acd8ec9567f424441b) 与 [setup-uv 文档](https://github.com/astral-sh/setup-uv)；输入 `version: 0.11.29`。 | 只安装精确 uv；不改变 workspace sources 或 lock。 |
| `actions/upload-artifact` | `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` | [官方 commit](https://github.com/actions/upload-artifact/commit/043fb46d1a93c77aae656e7c1c64a875d1fc6a0a)；归档每个 gate 的 result/log 与产物。 | 只传输 `.artifacts/` 和 `dist/`，不参与依赖解析。 |
| `actions/download-artifact` | `95815c38cf2ff2164869cbab79da8d1f422bc89e` | [官方 commit](https://github.com/actions/download-artifact/commit/95815c38cf2ff2164869cbab79da8d1f422bc89e)；按 job DAG 交接证据。 | 只消费上游 artifact，不允许用缺失或旧 identity 结果继续 release gate。 |
| GitLab uv image | `ghcr.io/astral-sh/uv:0.11.29-python3.12-trixie-slim@sha256:36cdfbf910c8b0f651355c013e7ece9678f4ecbf030a9fd9e6779de421189805` | [uv GitLab integration](https://docs.astral.sh/uv/guides/integration/gitlab/) 与 [GitLab image syntax](https://docs.gitlab.com/ci/yaml/#image)；tag 便于人读，digest 固定实际 OCI index。 | image 内 uv 与根 `required-version` 一致；`uv sync --locked --all-groups` 仍从根 workspace 执行。 |
| act | `0.2.88` | [act v0.2.88 官方 README](https://github.com/nektos/act/blob/v0.2.88/README.md)；用于本地读取 workflow、解析依赖并在 Docker 容器执行 job。 | 只验证 GitHub workflow 的本地容器执行语义；不替代 hosted artifact service、权限或 runner 证明。 |
| gitlab-ci-local | `4.73.0` | [4.73.0 官方 README](https://github.com/firecow/gitlab-ci-local/blob/4.73.0/README.md)；用于本地 Docker executor 与 artifact/needs 路径验证。 | 只验证本地 GitLab job 执行；protected variables、protected environment 和 hosted runner 保持未验证。 |

GitLab 的 artifact 传递与 job 顺序遵循官方 [`needs`](https://docs.gitlab.com/ci/yaml/#needs)、[`artifacts`](https://docs.gitlab.com/ci/yaml/#artifacts) 和 [动态 child pipeline](https://docs.gitlab.com/ci/pipelines/downstream_pipelines/#dynamic-child-pipelines) 语义；父 pipeline 的无凭据 plan 只为 `no-release` 生成无 environment/secret 的回执 job，可发布输入才实例化 protected promotion/publish jobs。真实 promotion 还要求官方 [protected environment/manual job](https://docs.gitlab.com/ci/jobs/job_control/#protect-manual-jobs) 与 protected variable 配置。本轮只验证 YAML 合同和无外部副作用替身，不创建这些远端设置。

### Apple Silicon 本地 runner 边界

本轮宿主为 macOS `arm64`，Docker daemon 为 `linux/arm64`。本地 runner 默认使用原生 `linux/arm64`：act 显式传入 `--container-architecture linux/arm64`，GitLab CI local 让固定 OCI index digest 解析到 `arm64` manifest；service smoke 也核对实际 PostgreSQL/Redis 容器架构。只有在专门验证跨架构兼容性并明确记录 QEMU/emulation 前置条件时才允许拉 `linux/amd64`，不得把 emulated 结果写成 Apple Silicon 原生 PASS。

act 对 GitHub artifact service 的模拟并不等同 hosted service；本轮若因本地 artifact server 协议差异失败，只记录已执行到的 job/命令证据和 runner 限制，不把该 job 写成 PASS。已经在容器中退出 0 的仓库 Make gate 仍是有效本地证据，artifact service 不属于本地 ready-to-archive 的验收依赖。GitLab CI local 同理不能证明 GitLab SaaS runner、protected variables 或 environment reviewer 已配置。

## 构建产物与证据

`make build` 的当前产物是 `packages/agent-harness` 的 wheel、sdist 和 `dist/SHA256SUMS`。`make release-dry-run` 另生成 `release-preview/v1` manifest、CHANGELOG preview、release notes 和隔离预演产物；这些 wheel/sdist 不作为 registry 上传输入。受保护 promotion 在更新版本/CHANGELOG、创建 release commit/tag/release notes 后，从 tag target 重新构建 `release-build/v1` 正式产物，registry 只消费该 manifest 与 `release-promotion/v1`。交付候选至少记录 commit/diff identity、上述命令退出状态、test/eval/smoke 摘要、service runtime 版本、artifact 文件名和 checksum；CI evidence runner 会把这些结果写入独立的 `.artifacts/ci/<gate>/` 目录。

复制的 service-app 不是独立发布证明。它必须通过可信本地 wheel/sdist/source 或组织私有 index bootstrap，并在自己的仓库建立生产配置、license/SBOM、secret、deployment 和 rollback 门禁。

## License 与 NOTICE

本仓库代码按 [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0) 提供，根 `LICENSE` 是许可证正文，`NOTICE` 指向 `compliance/third-party.toml` 的版本化依赖清单和当前无 vendoring 事实。`scripts/license_check.py` 对 `uv.lock` runtime closure、`licensecheck` metadata、依赖 policy、vendored source/ADR 精确匹配和 Compose image identity fail closed。`licensecheck` 的空值或 `UNKNOWN` 只允许由 `compliance/pypi-license-observations.toml` 中绑定 `name`、`version`、PyPI source、原始字段与精确版本官方 JSON 的观察补齐；已有工具观察与快照不等价、快照身份陈旧或依据不精确均失败，报告同时记录快照 checksum。该门禁不是法律意见或完整 SBOM。

Redis server `7.2.14` 按其版本化官方 [`COPYING`](https://raw.githubusercontent.com/redis/redis/7.2.14/COPYING) 记录为 BSD-3-Clause，redis-py client 独立记录为 MIT；二者都不等同于本仓库的 Apache-2.0。Redis 7.4+ 进入不同许可证体系，升级 Redis、改变分发/托管用途或准备生产发布前，必须按 [Redis 官方许可说明](https://redis.io/legal/licenses/) 和 [ADR-0003](adr/0003-redis-runtime-license-policy.zh-CN.md) 重新复核 NOTICE。本文不提供法律意见。

## 当前发布边界

`.github/workflows/ci.yml` 与 `.gitlab-ci.yml` 复用同一组 `make ci-*` 入口，覆盖 lock/install、独立质量项、unit/contract、integration、eval、local/service smoke、build、license 和 release dry-run，并以独立 result/log 与 artifact 归档失败诊断。两套 CI 的独立 `acceptance-validate` clean-runner job 显式下载或继承矩阵所需的全部 producer evidence，其中包括不会自动存在于新 runner 的 `install`、`integration` 与 `build` 结果。`docs/acceptance-matrix.md` 显式选择需要长期保障的 REQ，并将所选 REQ 及其全部 AC（当前共 92 项）逐项映射到仓库内存在的具体生产文件、精确 pytest node、CI job 和实际证据路径；validator 不从开发阶段或优先级标签推断范围，并会拒绝孤立 AC、目录、文件级测试映射、缺失/越界路径、空壳节点及已列明验收的 producer/行为节点错配。经独立审查点名的 import 扫描、fake adapter/eval、默认 tenant、deny audit、MCP allowlist 与 API/worker/tool/model/Event 关联传播均固定到实际执行相应行为的节点，不能退回只检查常量或无关 happy path 的测试。AC-001 的 `uv sync --frozen` 由 `install` 证据证明，AC-002 的 `uv build` 由 `build` 证据证明，AC-003/006 的 workspace 外 wheel 与复制模板运行由 `integration` 和实际集成测试证明；AC-012/068 明确由 `test-aggregate` 的 SQLite 节点与真实 PostgreSQL `smoke-service` 两部分共同证明，不能拿会跳过的 PostgreSQL pytest 冒充完整后端闭环。AC-065 映射到从公开入口完成 single-agent fake run 的正向节点，并由 `smoke-local` 证明总时延 `<5s`。

`make release-dry-run` 使用 Conventional Commits 和固定 `python-semantic-release==10.6.1` 计算下一 SemVer；有 releasable commits 时生成 `release-preview/v1`，无 releasable commits 时成功退出且不创建 tag/release。`make release-promote-plan` 同时生成版本化 plan 与 GitLab child config：GitHub 依据 plan output 在无凭据 `promote-no-release` 和 protected execute 间二选一；GitLab 的动态 child 对 `no-release` 只实例化无 environment/secret 的回执 job，对 `planned` 才实例化四阶段人工门禁。`make registry-publish-plan` 只接受已 promotion 的正式 build；只有 protected job 在 `make release-promote-execute` / `make registry-publish-execute` 注入显式授权、匹配的 preview/promotion receipt、受限 credential 和固定 HTTPS endpoint 后才可能执行副作用；本 checkout 不执行真实 promotion/publish。

Registry upload/check endpoint 只接受不含 userinfo、query 或 fragment 的纯 HTTPS 路由；token 只能通过 protected environment 的专用变量进入 execute job，不能夹带在 URL、argv、plan 或日志中。GitHub release 多路径 artifact 以 `.artifacts` 为 archive root，后续 job 下载到 `.artifacts` 才能恢复 Make target 消费的路径；这只完成静态 handoff 合同，不代表 hosted artifact service 已验证。

本轮 macOS arm64 本地边界为：act `0.2.88` 的 checkout、setup-uv `0.11.29` 和 `make ci-lock` 已执行通过，故 GitHub 仓库 gate 的本地证据为 PASS；整个 job 仍因 act artifact server 不支持 upload-artifact v4 `mime_type` 而失败，该上传不再作为本地收口验收项。GitLab 使用 `gitlab-ci-local 4.73.0`，在隔离副本中按其只同步 tracked files 的官方约束纳入当前 dirty 内容后，固定 Debian trixie arm64 镜像已完成 bootstrap、uv `0.11.29`、`make ci-lock` 与 artifact 导出并退出 0。GitHub/GitLab hosted execution、远端 protected environment、secret/artifact service 和 provider/registry side effect 仍是未验证边界，不能用本地 contract 或静态 YAML 解析宣称通过。

release、license 与双 CI 的三个历史 change 已于 2026-07-22 同步主规格并归档；当时记录的一次性 `owner-waived` 只描述该冻结候选的历史审查边界，不构成后续 reviewer PASS 或默认规则。AC-053/054 继续保持 `hosted-unverified`，只有真实 hosted pipeline 与远端保护证据才能关闭。

## 排障

- `uv lock --check` 失败：先确认 `pyproject.toml` 是否有未锁变更；不要手改 `uv.lock`。
- build 缺包或夹带 workspace 路径：检查 package include、template bootstrap 和 wheel-only smoke。
- service smoke 无法启动：检查 Docker daemon、Compose、端口、secret file 和本轮命名资源；按脚本输出清理，不删除无关 volume。
- license check 失败：补正确来源/许可证/修改说明，或移除不合规 vendoring；不要把目录改名规避检查。
- 需要发布：先确认 protected environment、endpoint identity、凭据范围、审批、版本和回滚合同；本地只运行 dry-run 或 loopback substitute，不要直接执行真实 `uv publish`。
