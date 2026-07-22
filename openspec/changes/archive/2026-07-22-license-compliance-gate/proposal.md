## Source Links

- Product-Spec.md: `REQ-021`、`AC-058`，以及 `REQ-020` 不得绕过 license check 的规则
- DEV-PLAN.md: `Phase 15: CI/CD、Release Automation 与合规收口`
- Design-Brief.md or design artifact: 不适用；本 change 不涉及产品 UI
- CONTEXT.md / ADR: `docs/adr/0003-redis-runtime-license-policy.md`

## Why

现有 `make license-check` 只验证根 LICENSE、NOTICE 非空和高风险目录名，不能识别已判定不兼容的依赖许可证，也没有让第三方来源、license 与修改说明形成可机器校验的追踪记录。发布门禁若停在这层，只是有个脚本，不是合规收口。

## What Changes

- 建立受版本控制的第三方来源/license/修改说明清单和依赖许可证策略；vendoring 条目必须引用 `docs/adr/` 下状态为 `Accepted` 的明确 ADR，且 `vendoring_approval` 的 `path`、`source_url`、`source_revision`、`source_sha256`、`license_expression`、`modified` 与 `modification_summary_sha256` 必须逐字段匹配。
- 扩展 `make license-check`：阻止未声明 vendoring、缺字段/失效来源、`adr_ref` 越界或悬空、非 `Accepted`、缺少/泛化 approval block、任一审批字段错配、未知或明确拒绝的 license，并保留可归档报告。
- 使用固定版本的 `licensecheck` 辅助解析依赖许可证；对其空值或 `UNKNOWN` 只使用按 `name`、`version`、PyPI source 和精确版本官方 JSON 绑定的版本化观察快照补齐，已有工具观察与快照冲突时 fail closed。仓库策略仍独立负责允许/拒绝结论和例外追踪；输出明确声明不构成法律意见。
- 关闭 ADR-0003 的发布前复审：恢复 P0 已批准的 Redis 7.2/BSD-3-Clause 边界，将 service profile 固定到含官方安全修复的 Redis `7.2.14` 与 PostgreSQL `18.4` OCI identity，复核 server/client license 边界并用真实 service smoke 留证。
- 用正常、未声明 vendoring、`adr_ref` 越界/悬空/非 `Accepted`、缺失或泛化 approval、各审批字段错配、拒绝 license、已声明且经 ADR 完整批准的合同测试固定行为。

## Non-Goals

- 不提供法律意见，不把 runtime server license 与 Python client license 混为一谈；只记录本 Phase 实际运行的 PostgreSQL/Redis image identity 与安全/许可证复审依据，Redis 7.4+ 的许可证选择不在本 change 范围内。
- 不生成完整 SBOM，不上传扫描结果到外部服务。
- 不为通过检查而改名隐藏 vendored source，也不允许无到期日、无依据的通配例外。

## Capabilities

### New Capabilities

- `license-compliance-gate`: 依赖 license 策略、第三方来源追踪与未声明 vendoring 阻断。

### Modified Capabilities

- 无。

## Impact

- 合规实现与记录：`scripts/license_check.py`、`LICENSE`、`NOTICE`、`compliance/third-party.toml`、`compliance/pypi-license-observations.toml`、vendoring ADR 引用合同和报告目录。
- service runtime：`templates/service-app/docker-compose.yml`、`docs/adr/0003-redis-runtime-license-policy.md` 与真实 smoke evidence。
- 测试：license contract tests。
- 工具 pin 由前置 `release-dry-run-private-registry` 集中维护，避免两个 change 同时改根依赖与 `uv.lock`。
- 下游 `ci-p0-evidence-closure` 将把本门禁作为独立 CI job 并归档报告。
