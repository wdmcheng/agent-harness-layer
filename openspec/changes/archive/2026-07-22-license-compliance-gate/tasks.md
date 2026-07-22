## 1. 先建立合规红证据

- [x] 1.1 为允许、拒绝、未知、版本漂移四类 dependency license fixture 新增公开 CLI 合同，保留现有脚本无法阻断依赖 license 的失败输出
- [x] 1.2 为未声明 vendoring、字段不全、通配伪声明、清单悬空，以及 `adr_ref` 缺失、越出 `docs/adr/`、悬空、非 `Accepted`、泛化 ADR/缺少 `vendoring_approval`、`path`/`source_url`/`source_revision`/`source_sha256`/`license_expression`/`modified`/`modification_summary_sha256` 各字段错配和完整 ADR 批准新增独立临时仓库红合同，保留当前无法执行明确 ADR 门禁的未满足证据

## 2. 建立版本化第三方清单

- [x] 2.1 新增 `compliance/third-party.toml` schema，记录 project policy、允许/拒绝 SPDX expression、逐包版本/source/license 判断、有期限例外，以及 vendoring `adr_ref` / `vendoring_approval` 精确匹配合同
- [x] 2.2 从当前 `uv.lock` 生成核心运行时直接/传递依赖 inventory，逐项复核版本、source、license metadata 与 Redis server/client 边界
- [x] 2.3 更新 `NOTICE` 指向第三方清单和当前无 vendoring 事实，不把自动检查表述为法律意见
- [x] 2.4 将 Compose 默认 image 固定为 PostgreSQL `18.4@sha256:3a82e1f56c8f0f5616a11103ac3d47e632c3938698946a7ad26da0df1334744a` 与 Redis `7.2.14@sha256:f0707c78ea880b293ccdeb410c9c0a8ccae93fe7128799b751333a698b0a39a7`，恢复 Redis 7.2/BSD-3-Clause 决策并更新 ADR-0003/NOTICE 的安全与 server/client license 边界，同时阻止 PostgreSQL 18.3 回退及 Redis 7.4+ 越界

## 3. 实现 fail-closed license gate

- [x] 3.1 扩展 `scripts/license_check.py`，验证 LICENSE/NOTICE、lock inventory、`licensecheck` 观察和仓库判断一致，并生成 `license-report/v1` JSON report
- [x] 3.2 实现 vendored tree 与 manifest 一一对应校验，要求 `source_url`、immutable `source_revision`、`source_sha256`、SPDX `license_expression`、`license_ref`、`notice_ref`、`modified`、`modification_summary`、`modification_summary_sha256` 和 repo-relative `adr_ref` 完整；`source_url` 的 userinfo/query/fragment 不得携带 credential；ADR 必须位于 `docs/adr/`、状态为 `Accepted`，且 `vendoring_approval` 的 `path`、`source_url`、`source_revision`、`source_sha256`、`license_expression`、`modified`、`modification_summary_sha256` 逐字段匹配
- [x] 3.3 对已拒绝、unknown、自定义条款、过期例外、lock/metadata 漂移、未声明 vendoring，以及 `adr_ref` 缺失/越界/悬空、状态非 `Accepted`、泛化 ADR/缺 approval block、任一 approval/checksum 字段错配返回非零，同时保留精确字段诊断和免责声明
- [x] 3.4 校验报告的 service image tag/index digest、实际 server version、许可证依据和 smoke evidence 路径；任一漂移或缺失均 fail closed

## 4. 验证公开门禁

- [x] 4.1 运行全部 license 合同、正常 `scripts/license_check.py` 与 vendoring/ADR 失败 fixture，核对非零状态、精确字段诊断、credential 脱敏和仓库相对路径边界
- [x] 4.2 运行真实 `make smoke-service`，证明 PostgreSQL 18.4、Redis 7.2.14、Streams/XAUTOCLAIM/recovery 兼容，并记录 Compose image identity 与 server version
- [x] 4.3 补齐 editable/virtual 依赖、workspace 根同名碰撞、许可证等价拼写、官方版本观察快照和完整 `license-report/v1` 字段回归合同
- [x] 4.4 运行受影响 quality/test、`make license-check` 与本 change strict，确认本地合规门禁满足归档前要求
