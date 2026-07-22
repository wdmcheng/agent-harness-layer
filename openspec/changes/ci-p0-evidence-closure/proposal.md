## Source Links

- Product-Spec.md: `REQ-019` 的 `AC-050`、`AC-051`，`REQ-020` 的 `AC-053` 至 `AC-056`，`REQ-021` 的 `AC-058`
- DEV-PLAN.md: `Phase 15: CI/CD、Release Automation 与合规收口`
- Design-Brief.md or design artifact: 不适用；本 change 不涉及产品 UI
- CONTEXT.md / ADR: `docs/adr/0003-redis-runtime-license-policy.md` 的 service smoke 与发布证据要求

## Why

GitHub Actions、GitLab CI、`make integration`、CI artifacts 和 P0 acceptance matrix 当前均不存在。最后一个 change 必须只编排已经通过合同验证的 release/license 能力，用同一仓库入口证明两套 pipeline 等价、失败会阻断下游，并把 P0 声明绑定到实际证据而非 YAML 存在性。

## What Changes

- 增加细粒度且稳定的 Make/脚本入口，独立执行 lock、ruff format、ruff lint、pyright、import boundary、unit/contract、integration、eval、local/service smoke、build、license 与 release dry-run。
- 两套 CI 在 release 相关 job 前显式获取完整 Git history/tags：GitHub checkout 使用 `fetch-depth: 0`，GitLab 使用 `GIT_DEPTH: "0"`，并以 shallow-clone 合同防止默认深度掩盖版本基线。
- 新增最小权限的 GitHub Actions 和 GitLab CI：可发布输入使用 `promote-plan -> promote-execute -> publish-plan -> publish-execute` 受保护人工门禁；`no-release` 输入则由无 environment、无 credential 的独立执行节点生成零副作用回执并终止。GitHub 以 plan job output 选择分支，GitLab 以无凭据 plan 生成只包含对应节点的动态 child pipeline；execute job 只从同一 `status: planned` artifact 取得动态审批摘要，并以 `release-build/v1` 与 `release-promotion/v1` 显式交接 tag 后正式构建及 promotion 身份。
- 将 eval 与两类 smoke 的原生 trace 固定为 `.artifacts/eval/scores.jsonl`、`.artifacts/eval/traces.jsonl`、`.artifacts/smoke/local/trace.jsonl`、`.artifacts/smoke/service/trace.jsonl`；禁止用 `native_artifacts_pending` 占位，原生产物必须同时进入 job manifest、`ci-result/v1` checksum 和两套 CI 的 artifact 上传集合。
- 用 pipeline contract tests、`act` 与 `gitlab-ci-local` 固定触发、依赖、artifact、权限和实际 job 执行边界；hosted runner 未运行时明确保持未验证。
- 新增覆盖全部 P0 REQ/AC 的 acceptance matrix，并只按最终证据同步产品/计划/README/release 文档状态。

## Non-Goals

- 不 push 触发 hosted CI，不修改远端 CI、环境、secret 或 protected branch 配置。
- 不真实发布、部署、创建 tag/release，也不将 local runner 结果表述为 GitHub/GitLab hosted PASS。
- 不重做前置 changes 的 release 或 license 核心逻辑。

## Capabilities

### New Capabilities

- `dual-ci-p0-evidence`: GitHub/GitLab 等价 pipeline、可归档证据与 P0 追踪矩阵。

### Modified Capabilities

- `maintainer-documentation`：按当前 checkout 区分已实现的本地 CI/release seam 与仍为 hosted-unverified 的平台执行、远端保护和凭据配置，不再把全部 Phase 15 自动化笼统标为“尚未实现”。

## Impact

- CI 与入口：`Makefile`、`.github/workflows/ci.yml`、`.github/workflows/release.yml`、`.gitlab-ci.yml`、`compliance/ci-jobs.toml`、CI evidence/contract scripts。
- 证据与文档：`docs/p0-acceptance-matrix.md`、`docs/release-process.md`、`README.md`、`Product-Spec.md`、`Product-Spec-CHANGELOG.md`、`DEV-PLAN.md`。
- 依赖 `release-dry-run-private-registry` 和 `license-compliance-gate`；实现顺序固定为两个前置 change 完成后再进入本 change。
