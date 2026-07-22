# maintainer-documentation Specification

## Purpose

定义脚手架深度文档的长期维护契约，使 app developer 与 scaffold maintainer 能从稳定入口找到当前能力、扩展边界、验证证据、安全约束和未来能力说明。

## Requirements

### Requirement: 深度文档提供双受众可达导航
系统 SHALL 从根 README 分别为 app developer 与 scaffold maintainer 提供清晰入口，并 SHALL 互链架构、扩展、adapter、context/trust、安全、eval/observability、release 和 ADR 文档。每份深度文档 MUST 能返回主导航或到达直接相关的相邻合同。

#### Scenario: 应用开发者从 README 找到操作入口
- **WHEN** app developer 从根 README 开始了解或复制 service-app
- **THEN** 其能找到 local/service profile、agent 扩展、验证命令和安全边界说明，而无需阅读归档 change 或搜索源码

#### Scenario: 脚手架维护者从 README 找到维护合同
- **WHEN** scaffold maintainer 从根 README 检查架构或扩展边界
- **THEN** 其能到达 adapter contract、context/trust boundary、安全策略、release process、eval/observability loop 和全部现行 ADR

### Requirement: 文档区分当前事实、扩展约束和未来能力
系统 SHALL 以当前代码、测试、配置和锁文件为依据描述已实现行为。已经落地并通过本地验证的 CI/CD 与 release automation seam MUST 作为当前仓库能力描述，同时 MUST 明确其本地证据和零外部副作用边界。尚未取得 hosted 证据的 runner 执行、artifact service、environment reviewer、protected ref、secret、真实 provider/registry 集成，以及尚未实现的物理服务拆分 MUST 明确标记为未来能力或 `hosted-unverified`，不得作为已部署、已发布或远端保护已生效的事实呈现。

#### Scenario: 维护者阅读 release process
- **WHEN** 维护者阅读 release process 或 README 的发布章节
- **THEN** 文档把当前可执行的质量门禁、版本计算、release preview、promotion/publish plan、零副作用替身与 wheel/sdist build 描述为本地现状
- **AND** 把 hosted runner、artifact service、远端 reviewer/protected ref/secret 与真实 provider/registry 执行明确标为未验证，不声明已发布或已归档

#### Scenario: 维护者阅读部署和 adapter 边界
- **WHEN** 维护者阅读架构、extension 或 adapter 文档
- **THEN** 文档准确区分当前 API/runtime worker 分进程、当前进程内 provider/repository seam 与未来 model/tool gateway、event pipeline、storage service

### Requirement: 每个能力块提供可执行维护证据
系统 SHALL 为 agent、tool、model、retrieval、observability、eval、guardrail/context/trust 和 release/compliance 能力块记录公开 seam、当前可执行命令、证据位置与常见故障排查。需要 PostgreSQL、Redis 或 Docker Compose 的命令 MUST 标注 service profile 前置条件，并 MUST 不以 local/SQLite 结果替代 service 证据。

#### Scenario: 开发者按扩展指南增加能力
- **WHEN** 开发者按 extension guide 扩展任一受支持能力块
- **THEN** 其能识别允许修改的边界、禁止跨越的依赖、对应验证命令和失败时优先检查的证据

#### Scenario: 维护者核验 service profile 命令
- **WHEN** 文档列出需要 PostgreSQL、Redis 或 Docker Compose 的操作
- **THEN** 文档明确标注前置条件和证据来源，且最终验证记录来自真实 service profile

### Requirement: 文档版本、链接与决策可复核
系统 SHALL 使文档中的内部路径、锚点、外部引用和技术版本可从当前 checkout 复核。Python dependency 的声明与解析版本 MUST 分别与对应 `pyproject.toml` 和 `uv.lock` 一致；Docker runtime MUST 按 Compose image reference 描述 pin 粒度；仓库未锁定的外部 CLI MUST 标明未锁定并记录本次验证版本，不得冒充 lock 内容。vendor isolation 与 Redis runtime/license policy MUST 由独立 ADR 记录背景、决策、替代方案、后果和复审触发条件。

#### Scenario: 当前 checkout 执行文档核验
- **WHEN** 维护者对 Phase 14 文档运行链接、路径、命令与版本核验
- **THEN** 所有内部目标存在、文档命令可执行或明确标注 service 前置，dependency/Compose/外部 CLI 分别按其权威来源核验，已知上游表述冲突被纠正，外部事实有官方来源

#### Scenario: 外部 provider 或 Redis 版本变化
- **WHEN** 维护者计划升级 vendor SDK、Redis runtime 或改变部署用途
- **THEN** 其能从 ADR 找到必须保持的隔离边界、license/NOTICE 复审条件和需要重新验证的证据

### Requirement: 完成状态只由冻结证据驱动
系统 MUST 在文档交付、命令/链接/版本核验和 OpenSpec strict validation 通过后，先把 `AC-049` 与 Phase 14 状态更新进候选最终 diff，再冻结该 diff 交 fresh reviewer。只有包含状态更新的同一冻结 diff 达到 Stage 1/2 PASS，Phase 14 才可声明完成。任何审查后实质变更 MUST 使旧 review 结论失效并触发重新审查。

#### Scenario: 验证通过后形成候选最终状态
- **WHEN** Phase 14 文档交付、命令/链接/版本核验和 OpenSpec strict validation 已通过
- **THEN** Product-Spec 与 DEV-PLAN 在 fresh review 前更新进候选最终 diff，标记文档交付完成且 Phase 15 仍未开始

#### Scenario: 状态更新包含在最终审查基线
- **WHEN** 包含 Product-Spec 与 DEV-PLAN 状态更新的候选最终 diff 被 fresh reviewer 审查
- **THEN** 只有该同一冻结内容达到 Stage 1/2 PASS 才能用于 Phase 14 完成声明

#### Scenario: 审查后文档发生实质变更
- **WHEN** fresh review 后任一受审文档、契约、测试或配置发生实质变化
- **THEN** 旧 PASS 不得用于完成声明，维护者必须重新冻结内容并从 Stage 1 复审
