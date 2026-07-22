## ADDED Requirements

### Requirement: 依赖许可证清单与 lock 保持一致
系统 SHALL 对两个可发布 runtime package 的普通依赖、全部 `optional-dependencies` 及其传递闭包维护版本化 license inventory；lock package 必须以 `name`、`version` 和 `source` 的组合身份唯一表示，并将自动 metadata 观察与仓库允许、拒绝或复核判断分开记录。`registry`、`git`、`url`、`path`、`editable` 和 `virtual` source 均必须形成稳定、可区分的 identity，不得把受支持的非 registry source 坍缩为 `unknown`。

#### Scenario: 当前依赖全部有可追踪判断
- **WHEN** `make license-check` 针对当前 `uv.lock` 运行
- **THEN** 每个核心运行时依赖的名称、版本、source、license expression 和仓库策略 `basis` 均出现在对应 package 报告项中；缺少非空 `basis` 时门禁至少标记 `review-required` 并非零失败

#### Scenario: 同名 lock package 不互相覆盖
- **WHEN** 核心运行时闭包包含名称相同但版本或 source 不同的多个 lock package
- **THEN** inventory、策略匹配与报告按 `name`、`version`、`source` 组合身份逐项处理，每个直接或传递依赖均保留独立结果

#### Scenario: workspace 根名称不能豁免同名第三方身份
- **WHEN** direct 或 transitive runtime 依赖与 `agent-harness` 或 `agent-harness-service-app` 同名，但版本或 source 不等于仓库实际 workspace root identity
- **THEN** 只有名称和 workspace editable source 同时匹配的实际根身份可从第三方报告排除；同名 registry、git、url、path、editable 或 virtual 依赖仍须逐项观察、执行策略并进入报告

#### Scenario: 可发布包的 optional runtime 依赖进入闭包
- **WHEN** `agent-harness` 或 `agent-harness-service-app` 在 lock 中声明一个或多个 `optional-dependencies` group
- **THEN** 每个 group 的直接依赖及其传递依赖均进入 inventory、策略匹配和报告，不能只遍历普通 `dependencies`

#### Scenario: 非 registry source 保持稳定身份
- **WHEN** lock 的运行时闭包中出现 `git`、`url`、`path`、`editable` 或 `virtual` package
- **THEN** 除两个可发布 workspace 根本身外，每个依赖均生成独立且非 `unknown` 的 identity，并分别接受 metadata 观察、策略判断和报告；不得因为 source 是 `editable` 或 `virtual` 而跳过

#### Scenario: 等价 metadata 拼写归一到同一许可证
- **WHEN** PyPI 与 `licensecheck` 对同一版本分别返回 `zlib/libpng`、`zlib_libpng`、`zlib/libpng License` 等已知等价拼写
- **THEN** 门禁使用同一规范许可证身份与仓库策略比较，同时在报告中保留原始 metadata 观察和不可变官方许可证依据；未识别或缺失 metadata 仍必须进入复核，不能被归一规则静默允许

#### Scenario: 工具 metadata 缺口使用版本绑定的官方观察快照
- **WHEN** `licensecheck` 对当前 PyPI runtime identity 返回空值或 `UNKNOWN`
- **THEN** 门禁只允许 `compliance/pypi-license-observations.toml` 中同时精确匹配 `name`、`version`、`registry:https://pypi.org/simple` source 和官方精确版本 JSON endpoint 的原始 `license` 或 `license_expression` 补齐；快照缺失、身份陈旧、字段非法、依据非官方精确版本端点，或已有非 unknown 工具观察与快照不等价时均非零失败，不得从仓库 policy 期望值反向生成观察

#### Scenario: lock 或 metadata 漂移触发复核
- **WHEN** 依赖版本变化、license metadata 缺失或与 inventory 不一致
- **THEN** 门禁返回非零并标记 `review-required`，不得沿用旧版本结论

### Requirement: 已拒绝或未知 license 阻断发布门禁
系统 MUST 拒绝策略中已判定不兼容的 license、非 SPDX 未知条款和无有效依据的例外，并在报告中说明是自动发现还是仓库判断。

#### Scenario: 明确拒绝的 license 失败
- **WHEN** dependency fixture 声明策略拒绝的 license expression
- **THEN** license check 返回非零并列出 package、version、license 和拒绝依据

#### Scenario: 未知 license 不被静默允许
- **WHEN** dependency 缺少 license、使用未知自定义条款或例外已过期
- **THEN** license check 返回非零并要求人工复核

### Requirement: vendored source 必须完整声明
系统 MUST 阻止未声明 vendoring；允许的第三方源码、片段或素材必须逐项记录 repo-relative `path`、`source_url`、immutable `source_revision`、`source_sha256`、SPDX `license_expression`、`license_ref`、`notice_ref`、`modified`、`modification_summary`、`modification_summary_sha256` 和 repo-relative `adr_ref`。引用的 ADR MUST 位于 `docs/adr/`、状态为 `Accepted`，并以机器可校验的 `vendoring_approval` 对 `path`、`source_url`、`source_revision`、`source_sha256`、`license_expression`、`modified`、`modification_summary_sha256` 逐字段批准。

#### Scenario: 未声明 vendored directory 失败
- **WHEN** 受检查根目录出现没有 manifest 条目的第三方源码目录或文件
- **THEN** license check 返回非零并报告精确 repo-relative path

#### Scenario: 已声明 vendoring 可追踪
- **WHEN** 每个 vendored file 都唯一匹配字段完整且路径存在的 manifest 条目，且 `adr_ref` 指向状态为 `Accepted`、审批字段与该条目完全一致的具体 ADR
- **THEN** 报告逐项保留来源、license、修改状态、NOTICE 与 ADR 审批引用和逐字段匹配结果，并通过 vendoring 检查；字段齐全但值非法的条目也必须进入失败报告，不能因提前拒绝而从 `vendored[]` 消失

#### Scenario: 伪声明不能绕过门禁
- **WHEN** manifest 缺字段、`source_url` 不是无 userinfo 且带主机的绝对网络 URL或在 query/fragment 中携带 token、secret、password、credential、signature、API key 等凭据字段，`modified` 不是布尔值、`license_expression` 命中 deny 或未命中 allow、使用通配/空/越界路径、指向不存在文件、NOTICE 没有对应入口，或 `adr_ref` 缺失/越界/悬空/状态非 `Accepted`
- **THEN** license check 返回非零而不是把目录视为已声明

#### Scenario: 泛化或错配 ADR 不能批准具体 vendoring
- **WHEN** ADR 没有 `vendoring_approval`，或其中 `path`、`source_url`、`source_revision`、`source_sha256`、`license_expression`、`modified`、`modification_summary_sha256` 任一项与 manifest 不一致
- **THEN** license check 返回非零并报告具体错配字段，不把 vendor adapter 架构决策或自然语言 NOTICE 当作源码批准

### Requirement: 合规报告不冒充法律意见
系统 SHALL 生成 `license-report/v1` JSON 与人类可读摘要，明确区分工程门禁、待人工复核和组织法律判断。

#### Scenario: 失败报告不得泄露来源 credential
- **WHEN** vendoring `source_url` 非法携带 username/password userinfo，或在 query/fragment 中携带 token、secret、password、credential、signature、API key 等凭据字段
- **THEN** 门禁非零失败，且 stderr、stdout 与 `license-report/v1` 均不得回显该 credential

#### Scenario: 非法 vendored 路径不得泄露本机目录
- **WHEN** vendoring 的 `path`、`license_ref`、`notice_ref` 或 `adr_ref` 携带绝对路径或越界路径
- **THEN** 门禁非零失败，失败条目仍保留在 `vendored[]`，但 stdout、stderr 与 `license-report/v1` 只记录稳定的脱敏标记，不得回显本机绝对路径

#### Scenario: 正常检查生成归档报告
- **WHEN** LICENSE、NOTICE、dependency inventory 和 vendoring 声明全部一致
- **THEN** report 记录 lock/policy/metadata snapshot checksum、工具版本、package/vendored/service image 逐项结果、findings 和免责声明，且不包含 credential 或本机绝对路径

### Requirement: service runtime image 在发布前固定并复审
系统 MUST 将 service profile 的 PostgreSQL 与 Redis 默认 image 固定到明确 tag 和 OCI index digest，并把实际 server version、PostgreSQL/Redis 安全依据与 server/client license 边界写入 ADR、NOTICE 和归档 evidence；license gate 必须逐项校验 NOTICE 内容，不能只检查文件非空。

#### Scenario: 固定 image 通过真实 service smoke
- **WHEN** 使用仓库默认 Compose 配置运行 `make smoke-service`
- **THEN** 实际拉取 PostgreSQL `18.4@sha256:3a82e1f56c8f0f5616a11103ac3d47e632c3938698946a7ad26da0df1334744a` 与 Redis `7.2.14@sha256:f0707c78ea880b293ccdeb410c9c0a8ccae93fe7128799b751333a698b0a39a7`，既有 Streams/XAUTOCLAIM/recovery 合同通过，并归档 image identity 与 server version

#### Scenario: 浮动、旧补丁或越过许可证边界的 Redis 被阻断
- **WHEN** Compose 使用浮动 tag、digest 漂移、回退到缺少后续安全修复的 7.2.4，或升级到采用不同许可证体系的 7.4+
- **THEN** license/service identity gate 返回非零，release dry-run 下游不得表述为可发布

#### Scenario: 已知受影响 PostgreSQL patch 被阻断
- **WHEN** Compose 使用浮动 PostgreSQL tag、digest 漂移，或回退到官方安全清单中被 18.4 修复的 18.3 及更早 18.x
- **THEN** license/service identity gate 返回非零并引用官方修复线，release dry-run 下游不得表述为可发布

#### Scenario: Redis server 与 client 分开裁决
- **WHEN** 生成 license report
- **THEN** Redis 7.2.14 server 按版本化官方 `COPYING` 记录安全依据与 BSD-3-Clause，redis-py client 按 lock metadata 独立记录为 MIT，不把 server/client 或项目 Apache-2.0 结论混为一谈；NOTICE 与归档报告均保留这些独立字段
