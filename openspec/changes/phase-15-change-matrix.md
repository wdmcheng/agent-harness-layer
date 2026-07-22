# Phase 15 关联 change 矩阵

本矩阵记录三个 active change 的依赖、共享验收和本地收口边界。三者共享 Phase 15 验收和实现范围，因此不主张独立；默认审查规则保持不变。本次 Phase 15 的 Reviewer 2/3 已由用户明确作出一次性最终审查豁免，该豁免记录为 `owner-waived`，不构成联合 Reviewer PASS，也不适用于后续 Phase。

## 当前收口状态

- 审查确认的 `2 HIGH + 1 MEDIUM + 1 LOW` 已按最小范围修复，fresh Reviewer 1 对四项修复给出 Stage 1/2 PASS。
- 受影响 quality/test/OpenSpec strict 已通过；真实 PostgreSQL 18.4/Redis 7.2.14 `smoke-service` 在 uv `0.11.29` 且 localhost 绕过宿主代理时完整退出 0，用户已裁决该门禁按 PASS 处理。
- 用户明确取消最终 Reviewer 2/3，并对本次 Phase 15 作出一次性 `owner-waived` 裁决。三个 change 的本地任务均完成，可标记 `ready-to-archive`；这不是 Reviewer 2/3 PASS。
- 三个 change 继续保持 active。GitHub/GitLab hosted runner、artifact service、远端 reviewer/protected ref/secret 与真实 provider/registry 执行仍为 `hosted-unverified`，AC-053/054 保持未勾选；不声明已归档或已发布。

## 依赖与允许顺序

```text
release-dry-run-private-registry
              │
              ├──> license-compliance-gate
              │             │
              └─────────────┴──> ci-p0-evidence-closure
```

1. `release-dry-run-private-registry` 先集中锁定 Phase 15 工具依赖与 `uv.lock`，并提供 release preview/artifact 公开 seam。
2. `license-compliance-gate` 复用已锁工具环境，提供 license report 公开 seam；不反向修改 release 文件。
3. `ci-p0-evidence-closure` 最后只编排两个前置 seam，建立双 CI、P0 matrix 和最终状态文档。

## 共享接口、验收与所有权

| Change | 直接依赖 | 共享接口 / artifact | 共享验收 | 文件所有权 |
|---|---|---|---|---|
| `release-dry-run-private-registry` | 无 | `release-preview/v1`：`.artifacts/release-preview/<identity>/manifest.json`、预演 `dist/`、`SHA256SUMS`；`release-promotion-plan/v1` 与 `registry-publish-plan/v1` 动态审批计划；`release-build/v1`：tag 后正式 `.artifacts/release-build/<identity>/manifest.json`、`dist/`、`SHA256SUMS`；`release-promotion/v1`：`.artifacts/release-promotion/<identity>/receipt.json`；registry wrapper receipt/status | AC-055、AC-056；AC-053/054 的 release job 输入与 promotion→publish 身份交接 | 根/核心/模板 `pyproject.toml`、`uv.lock`、`.gitignore` 的统一 `.artifacts/` 规则、release/registry scripts 与 tests、`CHANGELOG.md` |
| `license-compliance-gate` | release change 的工具 pin/lock | `license-report/v1`：`.artifacts/license/license-report.json`、第三方来源清单与 vendoring `adr_ref` 审批证据；固定 PostgreSQL/Redis image identity 与 `make smoke-service` runtime evidence | AC-058；AC-051/053/054 的 license/service jobs | `scripts/license_check.py`、`compliance/third-party.toml`、`NOTICE`、license tests、`templates/service-app/docker-compose.yml`、Redis ADR；未来具体 vendoring ADR 由引入源码的 change 拥有，本 change 只定义并校验引用合同 |
| `ci-p0-evidence-closure` | 前两项全部公开 seam | `ci-result/v1`、Make targets、`promote-plan -> promote-execute -> publish-plan -> publish-execute` CI DAG、完整 Git history/tag 输入合同、eval/smoke 稳定原生产物、统一 `.artifacts/**` 路径、P0 matrix | AC-050、AC-051、AC-053-056、AC-058；hosted 未运行时保持未验证 | `Makefile`、`.github/workflows/**`、`.gitlab-ci.yml`、`compliance/ci-jobs.toml`、CI scripts/tests、P0 matrix、最终完成状态与 release 文档；`DEV-PLAN.md` 顶部事实状态块由 Phase 15 主控在审查转折点同步，不得提前写成完成 |

## 共享 schema 与兼容规则

| Schema | 必填稳定字段 | 状态枚举 | 生产者 / 消费者 |
|---|---|---|---|
| `release-preview/v1` | 始终必填 `schema_version`、`status`、`source.commit_sha`、`source.dirty_diff_sha256`、`source.base_tag`、`current_version`、`decision.bump`、`decision.reason`、`decision.commits[]`、`artifacts[]`；`release` 另必填 `next_version`、`tag`、CHANGELOG/release-notes 与 wheel/sdist/checksum 引用 | `release`、`no-release` | release wrapper 生产；promotion/registry wrapper、双 CI、P0 matrix 消费 |
| `release-promotion-plan/v1` | 始终必填 `schema_version`、`status`、preview checksum 与 source identity；`planned` 另必填去敏 approval payload、`approval_sha256`、protected default branch、push endpoint SHA-256 与 provider endpoint identity；零授权 `no-release` 必填稳定 preview/source identity、`tag: null`，且不得含 approval | `planned`、`no-release` | promotion wrapper 生产；两套 CI 的 promotion consumer 只接受字段完整的 `planned` 或零授权 `no-release`，并在副作用或生成回执前重新校验 |
| `release-build/v1` | `schema_version`、`status`、`version`、`tag`、`tag_target_sha`、`uv_version`、`artifacts[]`（wheel/sdist/checksum 的 path/kind/SHA-256/size） | `built` | promotion 在 tag/release notes 后从 tag target 生产；promotion receipt、registry plan/execute 与双 CI 消费 |
| `release-promotion/v1` | 始终必填 `schema_version`、`status`、`preview_manifest_sha256`、`source.commit_sha`、`source.dirty_diff_sha256`、`version`、`artifacts[]`；`promoted` 另必填 `release_commit_sha`、`tag`、`tag_target_sha`、`release_notes_sha256`、`release_build_manifest_sha256`、`provider`、`provider_release_id`、`provider_release_url` | `promoted`、`no-release`、`failed` | promotion wrapper 生产；registry wrapper、双 CI publish job、P0 matrix 消费 |
| `registry-publish-plan/v1` | `schema_version`、`status`、去敏 approval payload、`approval_sha256`、preview/promotion receipt/release build checksum、release/tag/artifact identity、upload/check endpoint identity、protected tag ref | `planned` | registry wrapper 生产；两套 CI `publish-execute` 只接受 `planned` 并在网络前重新计算 |
| `license-report/v1` | `schema_version`、`status`、`input.uv_lock_sha256`、`input.policy_sha256`、`input.metadata_snapshot_sha256`、`tools`、`packages[]`、`vendored[]`（每项含 `adr_ref` 与审批匹配结果）、`service_images[]`、`findings[]`、`disclaimer` | `pass`、`fail`、`review-required` | license gate 生产；双 CI、P0 matrix 消费 |
| `ci-result/v1` | `schema_version`、`gate`、`status`、`command`、`exit_code`、`input_identity`、`artifacts[]` | `pass`、`fail`、`skipped` | evidence runner 生产；两套 pipeline 与 P0 matrix 消费 |

所有 artifact path 必须是 repo-relative；checksum 使用小写 SHA-256。`release-promotion/v1` 的 `tag_target_sha` 必须等于 `release_commit_sha`，其 `release_build_manifest_sha256` 必须匹配 `release-build/v1`，`artifacts[]` 必须与正式 build manifest 中 wheel/sdist 的 path/kind/SHA-256/size 完全一致；preview artifacts 只用于预演，不得作为上传输入。promotion plan consumer 只接受字段完整的 `planned` 或零授权 `no-release`，registry plan consumer 只接受 `planned`，release build consumer 只接受 `built`，registry execute 只接受 `promoted`；缺失/其他状态、陈旧 preview/build/receipt、身份或 checksum 漂移均在副作用前 fail closed。schema major 不匹配必须 fail closed，新增可选字段只允许向后兼容，状态枚举或必填字段变化必须升级 major 并重跑三 change 联合审查。

eval 与 smoke 的稳定原生产物固定为 `.artifacts/eval/scores.jsonl`、`.artifacts/eval/traces.jsonl`、`.artifacts/smoke/local/trace.jsonl`、`.artifacts/smoke/service/trace.jsonl`。`native_artifacts_pending` 不属于合法接口；每个路径必须同时进入 `compliance/ci-jobs.toml` manifest、对应 `ci-result/v1.artifacts[]` checksum、GitHub upload-artifact 与 GitLab artifacts，任一集合漂移都使联合验收失败。

## 冲突与接力规则

- 根 `pyproject.toml` 与 `uv.lock` 只由 release change 修改；license change 不自行追加依赖。
- `.gitignore` 的 `.artifacts/` 统一规则由最先实现的 release change 创建并独占所有权；license/CI changes 只在该目录下生产受审 artifact 并验证规则存在，不重复修改忽略文件。
- `templates/service-app/docker-compose.yml` 与 Redis ADR 只由 license change 修改，用于关闭发布前 image/security/license 复审；CI change 只消费其已验证的 service seam。
- 当前仓库没有 vendored source，因此 license change 不伪造批准 ADR；未来引入 vendoring 的 change 必须拥有对应 ADR，并按 license change 定义的 `adr_ref` 与 `vendoring_approval` 合同接受门禁校验。
- `Makefile`、两套 CI、P0 matrix 和最终完成状态文档只由最终 change 修改；前置 changes 通过脚本公开 seam 自证，不抢占共享入口。例外仅限 Phase 15 主控按已发生证据同步 `DEV-PLAN.md` 顶部“当前状态/进度/立即下一步”，用于避免计划继续声称“尚未实现”；该事实同步不得宣称 Phase 15 完成、发布、归档或 hosted CI 已验证。
- `docs/release-process.md` 最终只由 `ci-p0-evidence-closure` 一次性同步，避免前置实现状态被提前写成整体完成。
- 若实现需要越过上述所有权，先修改本矩阵和相关 design，重跑三个 strict，并从第 1 名 fresh code-reviewer 重新开始 1+2 联合审查。
