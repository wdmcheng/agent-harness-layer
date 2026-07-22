## Context

现有脚本没有依赖 license inventory，也把任意 vendored directory 一律当成错误，无法区分“已声明并审查”和“未声明”。Phase 15 需要一个确定性仓库策略：工具负责收集上游 metadata，仓库清单负责记录维护者已作出的允许/拒绝/例外判断，两者任何漂移都 fail closed。

当前 Compose 的 `redis:8.0.1` 偏离了 P0 原先为许可证风险批准的 Redis 7.2 线；redis-py client 版本与 server 主版本对齐不是有效理由。Redis 官方说明 7.2.x 及更早版本使用 BSD-3-Clause，而 7.4 起进入 RSALv2/SSPLv1 体系；`7.2.14` 于 2026-05-05 发布并包含三项安全修复。本 change 因此选择 Docker Official Image `redis:7.2.14@sha256:f0707c78ea880b293ccdeb410c9c0a8ccae93fe7128799b751333a698b0a39a7`，同时恢复既定 BSD-3-Clause 边界。PostgreSQL 官方安全清单确认多个影响 18 的漏洞在 `18.4` 修复，18.4 release notes 记录无需从 18.x dump/restore；因此把浮动 `postgres:18` 收紧为 Docker Official Image `postgres:18.4@sha256:3a82e1f56c8f0f5616a11103ac3d47e632c3938698946a7ad26da0df1334744a`，不再采用已知受影响的 18.3。两者均以 multi-arch OCI index digest 保留 amd64/arm64 可复现解析。官方依据：https://raw.githubusercontent.com/redis/redis/7.2.14/COPYING、https://github.com/redis/redis/releases/tag/7.2.14、https://redis.io/legal/licenses/、https://www.postgresql.org/support/security/、https://www.postgresql.org/docs/release/18.4/ 与 https://hub.docker.com/_/postgres 。

## Goals / Non-Goals

**Goals:**

- 让核心运行时依赖、第三方源码/片段和 NOTICE 引用都能追到固定来源、license、修改说明与明确接受的 ADR 批准。
- 明确阻断未知、未声明或策略中已拒绝的 license，并生成机器可读报告供 CI 归档。
- 让真实 service smoke 绑定固定 PostgreSQL/Redis image、实际 server version、Redis server/client license 边界和 ADR 复审结论。

**Non-Goals:**

- 不把自动 metadata 判断写成法律意见，不自动允许 copyleft/自定义 license。
- 不扫描 SaaS 条款、容器镜像全部文件或生成完整 SBOM。

## Decisions

### 1. 锁文件 inventory 与人工策略分层

`compliance/third-party.toml` 记录 project license、允许/拒绝 SPDX expression、需要复核的 package/version、来源 URL、依据和有期限例外；每个 runtime transitive dependency 必须与 `uv.lock` 当前版本对应。`licensecheck==2026.0.8` 提供独立 metadata 观察，仓库脚本负责把观察结果与清单对账。对于其空值或 `UNKNOWN`，`compliance/pypi-license-observations.toml` 只冻结官方 PyPI 精确版本 JSON 的原始 `license` 或 `license_expression`，并同时绑定 `name`、`version`、registry source、字段名和 endpoint；这份观察快照与仓库允许/拒绝 policy 分文件维护，不从 policy 反向生成。已有非 unknown 工具观察优先，并在与快照不等价时 fail closed；快照身份陈旧、重复、字段无效或 endpoint 非官方精确版本地址也立即失败。工具和 PyPI 对同一许可证使用不同分隔符或 classifier 文案时，只对明确列出的等价别名做规范化比较；清单 `metadata_license` 与报告仍保留发布物原始观察，独立的 `license_expression` 记录规范 SPDX 判断，`basis` 则绑定版本 tag 对应的不可变官方 LICENSE。未知值和未列别名继续 fail closed。选择这种分层结构，是因为上游 metadata 可能缺失或漂移，不能让一次网络查询静默改变门禁结论，也不能让本地 hook 的真值依赖 13 次实时查询。

### 2. vendoring 按受控根、manifest 与 ADR 精确匹配

高风险目录仍由目录名和清单共同发现。出现第三方源码、片段或素材时，manifest 条目必须至少包含 repo-relative `path`、`source_url`、immutable `source_revision`、`source_sha256`、SPDX `license_expression`、`license_ref`、`notice_ref`、`modified`、`modification_summary`、`modification_summary_sha256` 和 repo-relative `adr_ref`。`source_url` 的 userinfo、query 与 fragment 都是 credential 泄漏面；token、secret、password、credential、signature、API key 等敏感键一律 fail closed，并在 stderr/stdout/report 中用统一占位替代整条 URL。目录中的每个文件必须落入唯一条目；清单指向不存在路径也失败。

`adr_ref` 只能指向 `docs/adr/` 下的 Markdown ADR；ADR 必须存在、状态精确为 `Accepted`，并包含机器可校验的 `vendoring_approval` TOML block。该 block 的 `path`、`source_url`、`source_revision`、`source_sha256`、`license_expression`、`modified`、`modification_summary_sha256` 必须和 manifest 对应条目完全一致。缺少引用、路径越界、悬空引用、状态非 `Accepted`、审批 block 缺字段或任一值错配都返回非零。未来具体 vendoring ADR 由引入源码的 change 创建和拥有；当前仓库无 vendored source，因此本 change 只落 schema、空清单与 fixture，不伪造批准决策。

仅靠 NOTICE 一段自然语言、目录改名或泛化的 vendor adapter ADR 不构成具体 vendoring 批准。

### 3. 未知与拒绝状态都阻断

策略显式拒绝当前已判定不兼容的 license expression；metadata 缺失、非 SPDX 自定义条款、版本不匹配和过期例外进入 `review-required` 并返回非零。报告区分自动观察、仓库判断与人工/法律复核边界。

### 4. 合规报告 schema 固定为 `license-report/v1`

报告必填 `schema_version`、`status`、`input.uv_lock_sha256`、`input.policy_sha256`、`input.metadata_snapshot_sha256`、`tools`、`packages[]`、`vendored[]`、`service_images[]`、`findings[]` 和 `disclaimer`。`status` 只允许 `pass`、`fail`、`review-required`；失败也必须原子写出报告后返回非零。package 条目记录 name/version/source/license expression、metadata observation 与 repository decision；两个可发布 workspace 根本身不作为第三方报告项，根选择必须同时匹配包名与仓库固定的 editable source identity，不能按名称豁免同名第三方。其 runtime 闭包中的 `editable`/`virtual` 依赖与其他 source 一样必须进入策略和报告。vendored 条目记录 `adr_ref`、ADR status 与逐字段审批匹配结果；`path`、`license_ref`、`notice_ref`、`adr_ref` 在复制到失败报告前先规范化为仓库相对路径，非法值统一脱敏。image 条目记录 name/tag/index digest、运行时 server version、许可证选择依据和 smoke evidence 相对路径。禁止绝对路径、credential 和把自动结果称为法律意见。

### 5. Redis/PostgreSQL image 以 tag + OCI digest 进入发布证据

Compose 默认值使用完整 tag 和 OCI index digest，环境覆盖值必须进入本次 `smoke-service` result。Redis 7.2.14 server 按其版本化官方 `COPYING` 记录为 BSD-3-Clause；redis-py client 继续按 lock 中的 MIT metadata 独立判断。Redis 7.4+ 不得作为补丁升级绕过当前 P0 许可证决策。PostgreSQL 必须至少处于官方当前 18.4 安全修复线，禁止回退到 18.3。ADR 与 NOTICE 记录此边界。真实 service smoke 必须输出拉取到的 image identity、`redis-server --version`、PostgreSQL server version，并重跑现有 Streams/XAUTOCLAIM/recovery 合同；只改 Compose 文本不构成关闭复审。

## Affected Surfaces

- `scripts/license_check.py`
- `compliance/third-party.toml`、vendoring ADR 引用合同与生成的 `.artifacts/license/license-report.json`
- `NOTICE` 的清单入口和无 vendoring 当前声明
- `templates/service-app/docker-compose.yml` 与 `docs/adr/0003-redis-runtime-license-policy.md`
- license/service identity contract tests；不改变 Python runtime dependency

## Testing Seams

- CLI：`python scripts/license_check.py --report ...` 的退出码、stderr 和 JSON report。
- 文件系统边界：临时 root 中的 LICENSE、NOTICE、清单、vendored tree 与缺失/悬空/非 Accepted/字段错配/完整批准 ADR。
- dependency boundary：fixture lock/metadata 包含允许、拒绝、未知、版本漂移、工具 `UNKNOWN` 的版本绑定快照回退、已有工具观察与快照冲突，以及同一 Zlib 许可证在 PyPI/`licensecheck` 中的等价拼写。
- traceability：每个 vendored file 恰好映射一个包含来源/license/修改说明/`adr_ref` 的条目，且 ADR 的 `vendoring_approval` 与条目逐字段一致。
- service runtime：Compose tag/digest 合同、真实 PostgreSQL/Redis server identity 与既有 queue/recovery smoke。

## Risks / Trade-offs

- [上游 license metadata 不准确或网络不稳定] → 自动观察不直接放行；官方精确版本快照只补空值/`UNKNOWN` 并记录 checksum，已有观察冲突仍失败；版本化 policy 独立记录人工依据与来源。
- [允许列表过宽] → 使用规范 SPDX expression 和逐包版本记录，拒绝 `*`、unknown 或无期限模糊例外。
- [源码藏在非典型目录] → 扫描声明目录之外再用常见 vendoring 名称/marker 检测；文档明确 code review 仍承担复制片段识别。
- [用泛化 ADR 或悬空链接冒充批准] → 只接受 `docs/adr/` 下状态为 `Accepted` 且含精确 `vendoring_approval` block 的 ADR，逐字段匹配 manifest。
- [Redis server 与 client 混判] → server runtime 的 7.2.14/BSD-3-Clause 与 Python `redis` package 的 MIT metadata 分开记录；真实 service identity 和 lock inventory 各自留证。

## Migration Plan

先生成当前 lock 对应的 dependency inventory 并人工复核，再启用 fail-closed；随后固定 PostgreSQL/Redis OCI identity、更新 ADR/NOTICE，并通过真实 service smoke。当前仓库无 vendored source，NOTICE 指向空但有效的 vendoring 清单。回滚不涉及数据迁移，但发布不得在移除门禁或退回已知受影响 Redis 版本后继续。

## Open Questions

无阻塞问题。完整 SBOM 工具仍是组织发布前单独决策；本 change 只证明 Redis 7.2.14 的版本化 BSD-3-Clause 依据、安全公告、运行兼容和 server/client 边界可追踪，不能代替法律意见。升级到 Redis 7.4+ 必须另开决策。
