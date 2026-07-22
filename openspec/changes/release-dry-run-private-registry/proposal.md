## Source Links

- Product-Spec.md: `FLOW-005`、`REQ-020`、`AC-055`、`AC-056`、`ASM-006`、`Q-002`，以及 10.2 节“已从待确认列表移除的既定事项”中的 Python Semantic Release 决策
- Product-Spec-CHANGELOG.md: `v1.12` 对原 `Q-001` 决策的可追踪记录
- DEV-PLAN.md: `Phase 15: CI/CD、Release Automation 与合规收口`
- Design-Brief.md or design artifact: 不适用；本 change 不涉及产品 UI
- CONTEXT.md / ADR: `docs/adr/0003-redis-runtime-license-policy.md` 的发布前复审边界

## Why

当前仓库只能构建 `0.1.0` wheel/sdist，没有无副作用的版本、tag、CHANGELOG、release notes 与 checksum 预演，也没有受审批保护的私有 registry 路径。Phase 15 必须先把发布计算和外部副作用分离，避免维护者为验证流程而修改历史或误发包。

## What Changes

- 固定 `python-semantic-release`、`uv` 和 release wrapper 的职责边界，以 Conventional Commits 产生可解释的下一 SemVer；首版本显式启用 PSR 10.6.1 的 `allow_zero_version = true`，避免默认配置把本仓库 `0.1.0` 错算为 `1.0.0`。
- release wrapper 在版本计算前要求完整且非 shallow 的 Git history/tag 输入；它不自行联网补历史，避免不完整 checkout 被误判成首发或 `no-release`。
- 为有 releasable commits 与无 releasable commits 两条路径生成机器可读 preview；只有前者在临时构建副本中生成下一版本 wheel/sdist、release notes 与 SHA-256 checksum。
- 将 Product-Spec 的 `ReleaseRecord` 落为 `release-preview/v1` JSON CI artifact；它不创建数据库表，也不连接运行时 storage。
- 将模板对核心包的依赖改为可发布兼容范围，并验证 wheel/sdist metadata 不含 workspace path dependency。
- 实现受保护的 promotion 路径：只在显式审批与远端保护成立时更新版本/CHANGELOG、创建 release commit/tag/release notes，再从 tag target 的隔离 checkout 重新构建正式 wheel/sdist 与 `release-build/v1` checksum；本轮只在隔离临时仓库和 provider 替身验证，不对当前仓库或远端执行。
- 生成 `release-promotion/v1` 回执，把受审 preview checksum 与新 release commit、tag target、provider release、`release-build/v1` manifest 和正式构建 checksum 绑定；dry-run 产物只作为预演证据，registry publish 只消费完整且状态为 `promoted` 的回执及其正式构建产物。
- 定义私有 registry 的 credential、protected ref、人工审批、失败诊断和只允许同 checksum 重试的发布合同；execute 必须通过固定 `uv publish` 生成标准 Python package index multipart 请求，并以 `UV_PUBLISH_CHECK_URL` 对相同文件做 SHA-256 查重确认。为阻断固定 uv 对 30x 的隐式跟随，上传和查重请求只能经过进程内受限 loopback relay 转发到受审 endpoint，relay 拒绝所有重定向且不改变 distribution bytes。本 change 只验证本地 package-index 替身，不执行真实发布。
- 为 promotion 与 registry publish 各生成版本化 plan artifact，先在无写权限、无 credential 的 job 中冻结 endpoint、protected ref、source/artifact identity 和动态 `approval_sha256`，再由独立 protected/manual execute job 消费同一 artifact；execute 仍重新计算身份并 fail closed，不能使用 YAML 常量或人工抄写摘要代替本轮计划。

## Non-Goals

- 本轮不对当前仓库或任何远端 commit、push、创建 tag/release、修改历史或访问真实 package registry；这不是删除产品的受保护 promotion 能力。
- 不决定公开 PyPI 发布，也不创建远端环境、secret 或保护规则。
- 不创建 GitHub/GitLab pipeline；双 CI 编排由关联 change `ci-p0-evidence-closure` 负责。

## Capabilities

### New Capabilities

- `release-dry-run-private-registry`: Conventional Commits 版本预演、隔离构建、checksum 与受控私有 registry 发布边界。

### Modified Capabilities

- 无。

## Impact

- 依赖与版本配置：根 `pyproject.toml`、核心包与模板 `pyproject.toml`、`uv.lock`。
- 发布实现与测试：`.gitignore` 的统一 `.artifacts/` 规则、`scripts/release_dry_run.py`、`scripts/release_promote.py`、`release-preview/v1` / `release-promotion-plan/v1` / `release-build/v1` / `release-promotion/v1` / `registry-publish-plan/v1` schema、私有 registry wrapper、release contract tests、`CHANGELOG.md`。
- 明确不修改 runtime ORM、migration、repository 或 UnitOfWork；`ReleaseRecord` 只保留在 CI artifact 层。
- 下游关系：`ci-p0-evidence-closure` 依赖本 change 的稳定 preview、promotion receipt 与 artifact 接口；`license-compliance-gate` 复用本 change 集中锁定的 Phase 15 工具依赖，但不共享实现文件。
