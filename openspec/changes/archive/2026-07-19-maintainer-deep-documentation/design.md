## Context

Phase 1-13.9 已形成可运行的核心包、service-app 模板、API/worker 分进程 service profile、provider-neutral adapter、policy/HITL、retrieval、observability、eval 与 SSE 证据，但维护知识分散在代码、测试、归档 change、README 预留说明和少量专题文档中。Phase 14 的读者是使用模板的 app developer 与维护边界的 scaffold maintainer；两类读者需要同一组事实，却需要不同入口和操作深度。

本变更只整理与验证当前事实，不改变运行时。上游范围由 Product-Spec `REQ-018/AC-049` 和 DEV-PLAN Phase 14 决定；代码、公开 DTO/protocol、测试、`pyproject.toml`、`uv.lock` 与可执行脚本证明当前实现。既有 `docs/adr/0001-p0-service-boundaries.md` 约束当前部署形态和未来拆分顺序。

## Goals / Non-Goals

**Goals:**

- 建立从根 README 到架构、扩展、adapter、安全、context/trust、eval/observability、release 和 ADR 的双受众导航。
- 让每个能力块同时给出维护边界、公开 seam、命令、证据位置和故障排查。
- 让文档中的版本、链接和现状声明可由当前 checkout 复核。
- 明确 release 文档只描述当前人工验证/构建边界与未来自动化合同，不把 Phase 15 写成已实现。

**Non-Goals:**

- 不新增代码、API、schema、migration、dependency 或发布基础设施。
- 不重新设计既有架构，也不把逻辑边界提前物理拆分。
- 不实现 CI/CD、SemVer 自动计算、tag、CHANGELOG 生成或 registry publish。

## Decisions

### 1. 使用双受众入口，而不是复制两套文档

根 README 保留 app developer 与 scaffold maintainer 两个入口，深度文档按能力边界组织。每份文档在开头声明适用读者，并用相对链接回到导航和相邻合同，避免两套说明随着实现演进而分叉。

替代方案是分别维护开发者手册和维护者手册；它会重复 Quick Start、目录边界、命令和安全约束，因此不采用。

### 2. 当前事实与未来合同显式分栏

所有涉及部署、provider、release 的文档使用“当前已实现”“扩展时必须保持”“尚未实现”三个语义层次。Phase 15 相关内容只能作为未来门禁和非目标出现，不能出现暗示 workflow、tag 或 publish path 已可用的操作步骤。

替代方案是只描述最终目标形态；它会让维护者执行不存在的入口，也违反现状真实性，因此不采用。

### 3. 以公开 seam 和证据入口组织维护说明

adapter 文档围绕 protocol/facade/repository/UoW/DTO 等公开 seam；扩展指南围绕 agent、tool、model、retrieval、observability、eval 的注册与验证路径；安全与 context/trust 文档围绕身份、权限、policy、approval、workspace、secret redaction、source/trust refs 和事件可见性。每个能力块链接生产文件和至少一种测试/eval/smoke 证据，不复制大段实现细节。

替代方案是按源码目录逐文件解释；它能导航但不能表达跨目录合同和失败边界，因此不采用。

### 4. 文档验证复用现有稳定命令，不新增 Phase 15 工具

本 change 使用现有 `make quality/test/eval/smoke-local/smoke-service/build/license-check`、OpenSpec strict validation 和一次性只读链接/版本检查。service profile 证据必须来自真实 Docker Compose、PostgreSQL 与 Redis；不以 SQLite 或静态说明替代。

替代方案是新增长期 docs lint/release 脚本；这会扩大到 Phase 15 或引入新的维护面，因此不采用。

### 5. ADR 分别记录 vendor 隔离与 Redis runtime/license 决策

`0002` 记录 vendor SDK 只能进入 adapter/integration boundary、核心合同保持 provider-neutral；`0003` 记录 Redis 8.0.1 当前 runtime pin、用途边界以及升级前必须重新做 license/NOTICE review。二者都引用既有 `0001`，但不改写其中的服务拆分决策。

替代方案是把两项塞进架构 README；这样无法保留决策背景、替代方案和后续复审触发条件，因此不采用。

### 6. 按资产类型裁决版本真相，不制造虚假统一 pin

Python dependency 的声明版本以对应 `pyproject.toml` 为准、解析版本以 `uv.lock` 为准，两者必须一致。Docker runtime 以 `docker-compose.yml` 中实际 image reference 为声明真相：例如 `postgres:18` 只固定 major tag，不能写成已 pin `18.4`；service smoke 观察到的 server patch version 作为一次运行证据单列。`uv`、Docker、Compose 等仓库未锁定的外部 CLI 只能记录本次验证版本和“未锁定”状态，不得声称与 `uv.lock` 一致。若 DEV-PLAN 技术栈表与当前受控配置冲突，本 change 纠正表述，不修改依赖或 image。

替代方案是把当前开发机版本直接写成仓库 pin；它无法跨机器复现，也会掩盖 Phase 15 尚未建立 CI toolchain pin 的事实，因此不采用。

## Affected Surfaces

- 导航和受众入口：`README.md`、`docs/architecture/README.md`。
- 模板下游入口：`templates/service-app/README.md`、`templates/service-app/docs/README.md`，只修正 Phase 14 交付状态并链接仓库级合同。
- 能力合同：`docs/extension-guide.md`、`docs/adapter-contracts.md`、`docs/context-and-trust-boundary.md`、`docs/security-policy.md`。
- 证据闭环与发布边界：`docs/eval-observability-loop.md`、`docs/release-process.md`。
- 决策记录：`docs/adr/0002-vendor-adapter-isolation.md`、`docs/adr/0003-redis-runtime-license-policy.md`。
- 版本与状态真相源：按版本裁决规则纠正 `DEV-PLAN.md` 的 toolchain/runtime 表述；全部命令、链接和版本验证通过后更新 `Product-Spec.md`、`DEV-PLAN.md` 状态，再冻结最终 diff 进入 fresh review。
- 不影响 API、数据模型、migration、UI、配置 schema、依赖或 runtime code。

## Testing Seams

- README/深度文档导航：从根 README 出发能到达全部必需文档、ADR 和返回入口；相对路径与锚点存在。
- 命令 seam：文档列出的仓库命令能在当前 checkout 执行；service 命令在真实 service profile 下执行。
- 版本 seam：Python dependencies 与 `pyproject.toml`/`uv.lock` 一致；Compose runtime 与 image reference 一致；未锁定外部 CLI 明确标记未锁定并记录本次验证版本，不能冒充 lock 内容。
- 合同 seam：adapter/extension/security/context 文档引用的公开类型、命令、路径和测试文件真实存在。
- 状态 seam：release 文档和 README 不声称 `.github/workflows`、`.gitlab-ci.yml`、release dry-run、tag 或 publish automation 已存在。
- OpenSpec seam：strict validation 通过，artifact review 与最终实现 review 都绑定各自冻结内容摘要。

## Risks / Trade-offs

- [文档复制实现细节后快速过期] → 只记录稳定合同、公开 seam 与证据入口；具体算法留在代码和测试。
- [链接存在但证据与声明不匹配] → 人工逐项建立声明到生产/测试/命令的核验矩阵，reviewer 检查语义而非只跑链接检查。
- [release 文档被误读为可发布流程] → 明确当前只能 build/dry verification，Phase 15 自动化入口标记为不存在且禁止执行。
- [外部版本或 license 页面发生变化] → 文档以锁文件为当前版本真相源，ADR 记录复审触发条件并链接官方来源。
- [真实 service smoke 耗时且依赖 Docker] → 在最终状态同步前运行一次完整 service profile 验证；失败则保持 Phase 14 未完成。

## Migration Plan

无数据或运行时迁移。实施顺序为：先建立文档合同与导航，再核验命令/链接/版本；验证通过后同步版本说明和 Phase 状态，随后冻结包含这些状态变更的最终 diff 并从 Stage 1 开始 fresh review。review 后若发生实质变化则重新冻结和复审。回滚只需还原本 change 的文档和状态更新，不影响运行数据。

## Open Questions

无。Phase 15 的 CI provider、semantic-release 工具和 registry 配置由后续 change 决定，本变更不预选。
