# 为 Agent Harness Layer 做贡献

[English](CONTRIBUTING.md)

本指南是人和 Agent 在本仓库共同遵守的贡献契约，说明如何确认当前事实、保持架构边界、
选择充分验证并交付证据。它不授予 commit、push、publish、release、deploy、使用生产资源
或归档变更的权限。

## 从当前事实开始

实现前先检查 `git status --short` 和相关 diff。已有 tracked/untracked 工作都属于其所有者；
不得改写、删除、暂存或顺手清理分配范围之外的改动。

按以下顺序读取本次适用的事实源：

1. `Product-Spec.md` 定义产品需求和验收标准。
2. `API-Contract.md` 定义公共 HTTP、CLI 和 module contract。改动跨越这些 seam 时读取相关
   章节；只有公共契约确实变化时才更新它。
3. `DEV-PLAN.md` 定义阶段 DAG、所有权、交付物和预期证据。
4. 相关已同步 `openspec/specs/<capability>/spec.md` 定义长期 OpenSpec 行为，active
   `openspec/changes/<change>/` 定义一次增量 delta。先用 `uv run openspec list --json` 确认当前
   状态，再读取适用主规格，以及 active change 中实际存在的 proposal、delta specs、design、
   tasks 和元数据。不能仅因仓库支持 OpenSpec 就自行创建、sync 或 archive change。
5. [工程原则](docs/engineering-principles.zh-CN.md)、相关架构指南和 ADR 解释已确定的边界与
   取舍，只读取会影响本次改动的决策。

架构工作跨 session 时，还要读取并维护[架构演进 living plan](docs/plans/architecture-evolution-plan.md)
及其[变更矩阵](docs/plans/architecture-evolution-change-matrix.md)，并用当前 Git/OpenSpec 状态重新
核对冻结基线；对话历史不是权威 handoff。

随后从受影响的公共 seam 检查当前实现和测试。不能用旧审查、生成图、issue 描述或记忆中的
文件布局替代磁盘上的当前文件。如果两个事实源冲突，停止实现，准确报告冲突及影响，不得
擅自选择一方覆盖另一方。

## 定义可审核的变更

- 编辑前写明目标、验收标准、受影响公共 seam、文件所有权和验证计划。
- 行为改变先更新其控制规格。公共 API、CLI 和 module contract 没变时，不要改
  `API-Contract.md`。
- 实现前先通过公共 seam 增加失败的 contract 或 regression test。有效 seam 包括 HTTP、CLI、
  module protocol、repository/Unit of Work、canonical event 和持久化状态；测试私有 helper
  不能替代公共行为测试。
- 每批改动要小到可以独立审查和回退，不把顺手清理混进功能或修复。
- 有意识地维护兼容性。schema、event、CLI flag、migration 或持久化值变化时，明确迁移和
  回滚行为，不能假设所有环境都是空状态。

## 协调人、Agent 与 worktree

并行不等于没有所有权：

| 需求 | 机制 | 规则 |
|---|---|---|
| 独立分析或 fresh 判断 | Sub-Agent | 用于认知并行，必须提供完整相关契约和范围；独立上下文不等于文件隔离。 |
| 独立实现 | 独立 worktree | 只有在证明无顺序依赖、无共享接口、无共享验收且无文件所有权重叠后才能使用；任一重叠都由一个串行 owner 或协调批次处理。worktree 不能充当独立审查。 |
| 共享 contract、manifest、索引或根文档 | 一个串行 owner | 其他执行者只向 owner 提供发现或 patch，不并发编辑同一个共享文件。 |
| 同一 seam 上的耦合改动 | 一个协调批次 | 让规格、红灯测试、实现和证据保持在一起，避免把中间状态当成交付状态。 |

所有贡献者应用 patch 前都要重新检查当前 diff。Agent 不得回滚其他执行者的改动、扩大文件
范围，也不得把“工具可用”理解成“获准使用”。协调 owner 负责合并发现、处理冲突，并决定
内容何时冻结进入审查。

## 代码与维护说明风格

本项目面向维护者的注释、docstring、测试说明、脚本/配置说明和操作文档以中文为主；
identifier、protocol field、API 名称、schema field、error code、命令及其他稳定技术术语保持
英文。

说明应解释维护意图、责任、数据形状、约束、风险、兼容性和取舍，不逐行复述显然代码。
非显然 API、worker、migration、并发路径、安全边界、测试 fixture 和公共 seam 必须提供足够
上下文，让下一位维护者能安全修改。

把 SRP、OCP、DIP 等原则当作判断工具，不把它们当成验收指标。interface、layer 或设计模式
的数量从来不是质量证据。优先采用能守住所需边界的最小显式设计。禁止隐藏的全局可变
singleton；有状态依赖必须在 application 或 adapter boundary 显式装配。

遵循已有命名和公共数据形状。避免无关格式化或重命名、为假想未来做抽象，以及没有当前
需求的兼容 wrapper。

## 保持架构边界

| 区域 | 必须保持的边界 |
|---|---|
| 核心包 | `packages/agent-harness` 不得依赖模板、示例或具体业务 Agent。 |
| 应用层 | `templates/service-app/app` 是薄 HTTP、CLI、worker 和装配层，负责 protocol DTO 转换和依赖装配，不承载业务 Agent 逻辑。 |
| 业务 Agent | `templates/service-app/agents` 只依赖 harness 公共 seam，不直接 import vendor SDK、storage driver 或 ORM session。 |
| Vendor integration | vendor SDK import 只存在于批准的 adapter/integration 路径，并位于项目自有 protocol/provider boundary 之后；vendor object 不泄漏进 core 或业务 contract。 |
| 持久化 | ORM session 留在 storage adapter、repository、migration 和 Unit of Work 实现内；application 与 Agent 代码只使用 repository/UoW seam。 |
| 跨层数据 | 使用显式 DTO、protocol、facade、provider、repository/UoW 和 `CanonicalEvent` 跨边界；不得把临时 dict、ORM entity 或 vendor response object 变成隐藏契约。 |
| 副作用 | 外部或特权副作用必须走 policy/HITL/audit 路径。policy 决定是否需要人工批准；批准的动作及结果通过 audit 和 canonical event 证据保持可追溯。 |

如果一条架构规则可以机械判定，只写文档不算完成；同一变更必须更新 checker、公共 contract
test 和相关 CI seam。

## 认清机械规则的真相源

不要在文档里复制工具配置：

- `pyproject.toml` 是 uv workspace、受支持 Python/工具版本、Ruff、Pyright strict、pytest
  discovery 和根环境约定的真相源；各 package 的 `pyproject.toml` 持有自己的 metadata 与
  build 配置。
- `.pre-commit-config.yaml` 是 pre-commit 实际执行 hook 的真相源。
- `scripts/import_boundary_check.py` 检查静态依赖方向、批准的 vendor import 位置、workspace
  dependency mapping 和 ORM session boundary；它不替代 runtime sandbox、policy 或 adapter
  contract test。
- `Makefile` 提供稳定的 reviewer/CI 入口；每个 target 背后的脚本和工具配置定义其精确边界。
- `tests/contracts/` 下的 contract test 把公共 seam 和维护规则变成可执行证据。

新规则只要可以机械判定，就应写入对应配置、checker、contract test 和 CI 路径。不能让长期
架构约束只能靠 reviewer 记忆执行。

## 选择充分验证

稳定 Make target 各自证明不同边界：

| 命令 | 覆盖范围 | 不能证明什么 |
|---|---|---|
| `make quality` | Pyright 环境校验、Ruff format/lint、strict Pyright 和 `scripts/import_boundary_check.py`。 | runtime 行为、测试、外部服务、打包或许可证。 |
| `make test` | 使用 release dependency group 运行 `tests/` 下的仓库 pytest suite。 | 被 skip 的环境相关用例，或真实 PostgreSQL/Redis service 行为。 |
| `make eval` | local SQLite migration、仓库内 example eval 流程，以及导出的 score/trace artifact。 | 真实 provider 质量、hosted observability 或 service storage/queue 行为。 |
| `make smoke-local` | 不依赖外部 model、database 或 queue service，验证 offline local profile、core import、template layout、公共 CLI、fake run 和 local event transport。 | PostgreSQL、Redis、DBOS、API/worker recovery 或生产配置。 |
| `make smoke-service` | 构建新的 core wheel，在 workspace 外复制模板，然后运行 Compose-backed service 路径并生成证据；要求可用的 Docker Compose、PostgreSQL 和 Redis。 | 生产部署或使用生产凭据的权限。 |
| `make build` | 在 `dist/` 生成 core package distribution 和 `dist/SHA256SUMS`。 | fresh environment 安装、publish 或 deploy。 |
| `make license-check` | license 文件、lock dependency policy/observation、vendoring 记录和批准的 service image identity，并写入机器可读报告。 | 法律意见或 runtime 正确性。 |

按改动 seam 选择最小充分矩阵：

| 改动类型 | 最小充分验证 |
|---|---|
| 仅文档 | 检查本次链接、中英文事实一致性和文档中的命令；运行相关 documentation contract test；运行 `git diff --check -- <changed-docs>`。默认不跑全量测试。 |
| 不改变行为且边界明确的 Python refactor | `make quality` 加精确受影响测试；共享边界变化或无法证明隔离时再加 `make test`。 |
| 公共 HTTP、CLI、module、runtime、repository、event 或持久化行为 | 新增的红灯 contract/regression test、`make quality` 和 `make test`；再按下列 runtime 边界追加 smoke/eval target。 |
| Local profile、template shell、CLI、fake model 或 event transport | 公共行为行，再加 `make smoke-local`。 |
| Eval dataset、scoring、trace 或 review flow | 公共行为行，再加 `make eval`；涉及 service storage/queue 时再跑 `make smoke-service`。 |
| PostgreSQL、Redis、DBOS、migration、API/worker 协作、持久化恢复或 service-only authentication | 公共行为行，再加 `make smoke-service`；SQLite 或 mock 结果不能替代。 |
| Package metadata 或 build output | `make quality`、`make test`、`make build`，再加受影响 consumer/template smoke；build 不等于 publish。 |
| Dependency、vendored code、license policy 或 runtime image | 受影响测试和 `make license-check`；service image identity/evidence 变化时先跑 `make smoke-service`，package 内容变化时再跑 `make build`。 |

当前可执行的定向文档 contract 示例：

```bash
uv run --group release pytest -q tests/contracts/test_documentation_bilingual_contracts.py
```

准备获准的全仓交付或 commit 时运行 `uv run pre-commit run --all-files`。它复用已配置的机械
hook，不能替代上面的行为验证矩阵。

只报告实际运行的结果。每条命令写明命令、退出状态、简要结果和覆盖 seam。环境问题标为
`BLOCKED`，测试或契约失败标为 `FAIL`，未运行门禁标为 `NOT RUN` 并说明原因。通过只能证明
该命令声明的边界。纯文档改动如果无需全量测试和 service smoke，要明确说明未运行及理由。

## Git 与安全纪律

- 不提交 secret、credential、`.env`、`.agent-harness/`、数据库文件、trace payload、本地状态、
  `.artifacts/` 报告或生成的 release preview。遵守 tracked policy 和 `.gitignore`，不 force-add
  被忽略状态。
- 除非用户明确授权该边界，不使用生产 credential、生产数据或真实 provider。测试与示例使用
  隔离本地状态和 fake provider。
- 未得到对应动作的明确授权，不 commit、amend、push、tag、publish、release、deploy，也不
  archive OpenSpec change。一个动作的权限不蕴含另一个动作。
- 获准 commit 前检查 staged diff，只包含约定文件，并使用 `feat:`、`fix:`、`docs:`、
  `refactor:`、`chore:` 等原子 Conventional Commit message。
- 生成 build 或 release 证据只是验证输出，不代表获准发布。

## 交付检查清单

- 已写明接受的需求和受影响公共 seam。
- 已按顺序读取适用事实源，且报告了所有冲突。
- 用户和其他执行者的无关改动保持不变。
- 行为变化有公共红灯 contract/regression test，并已更新控制规格。
- package、Agent、vendor、persistence、跨层和副作用边界保持显式。
- 可以自动执行的新规则已经进入机械门禁。
- 验证结果明确区分 `PASS`、`FAIL`、`BLOCKED`、`NOT RUN`，并说明未覆盖边界。
- 不包含 secret、本地状态、trace、数据库或 release preview。
- commit、push、tag、publish、release、deploy 和 OpenSpec archive 状态都按事实报告，不作暗示。
