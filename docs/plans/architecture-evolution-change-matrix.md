# 架构演进 Change Matrix

> 首次冻结：2026-07-27
>
> 上位计划：[`architecture-evolution-plan.md`](architecture-evolution-plan.md)
>
> 状态依据：当前 Git、OpenSpec 和磁盘文件；聊天摘要与历史关系图不能单独更新状态

本矩阵同时记录 change 的依赖、共享接口、共享验收、预计文件所有权、并行级别和 worktree 建议。矩阵中的未来名称是计划 identity，不表示目录已经创建；只有 `openspec list --json` 能证明 active change 状态。

## 1. 状态定义

| 状态 | 含义 |
|---|---|
| `文档中` | 仅治理/计划文档正在落盘；不代表行为实现 |
| `计划` | 只有长期计划，尚未获授权创建 OpenSpec change |
| `契约中` | 已创建 change，proposal/spec/design/tasks 尚未全部 strict + review PASS |
| `可实现` | strict validation 与要求的 fresh 契约审查已 PASS，依赖和 owner 已冻结 |
| `实现中` | 已有实现 diff，但尚未完成全部验证和 review |
| `验收中` | 实现冻结，正在完成 fresh review、证据和生命周期收口 |
| `已归档` | 用户已授权 sync/archive，active list、归档目录和侧车引用均已核对 |
| `外部阻塞` | 仅凭据、网络、配额或外部服务阻塞；不得写成 PASS |

状态迁移必须有证据。`openspec validate`、测试或文档勾选都不能单独把 change 推进到“可实现”或“已归档”。

## 2. 默认依赖 DAG

```text
Phase 17 governance baseline (non-OpenSpec documentation batch)
  -> acceptance-criteria-identity-uniqueness (governance correction)
    -> controlled-real-model-runtime
      -> controlled-model-streaming
        -> provider-neutral-structured-output
          -> provider-neutral-tool-call-contract
            -> policy-gated-tool-loop
              -> durable-tool-loop-resume
                -> Phase 21 implementation changes

Phase 17 governance baseline
  -> architecture-hotspot-rebaseline (read-only inventory)
    -> Phase 21 implementation changes
```

Phase 21 的只读 inventory 可以提早进行；写入型 change 默认等 Phase 20 稳定。任何例外都必须先更新本矩阵，给出无顺序依赖、无共享接口、无共享验收、无文件所有权冲突的证据。

## 3. Change 关系与所有权

| 阶段 | Change / 批次 | 目标 | 直接依赖 | 共享接口 | 共享验收 | 预计文件所有权 | 并行级别 | Worktree 建议 | Codex 时间估计 | 状态 |
|---|---|---|---|---|---|---|---|---|---|---|
| 17 | `architecture-governance-baseline`（非 OpenSpec 文档批次） | 冻结工程原则、贡献规则、living plan、handoff 与矩阵；同一 v1.20 修订补充 Phase 18.1 规划 | 无 | 工程原则、贡献流程、OpenSpec/Agent Pack 门禁语言 | 文档互链、事实优先级、状态语义、handoff 可恢复 | `docs/plans/architecture-evolution-*.md`；`docs/engineering-principles*.md`；`CONTRIBUTING*.md` 按本轮独立 owner 分配；Product Spec/DEV/API Contract 由主控整合 | 可做认知并行；写入必须按文件独占 | 当前工作树即可；无需为纯文档另开实现 worktree | 4-8 小时；Phase 18.1 规划补充另计本轮执行 | `已完成`；治理基线、Phase 18.1 规划与审查修订已落入 `4922784d`，不代表 checker 或模型能力已实现 |
| 17.1 | `acceptance-criteria-identity-uniqueness` | 消除重复 `AC-070` 对 Spec、矩阵和 policy/checker 语义的覆盖，建立机械唯一性门禁和历史追溯 | Phase 17 文档冻结；用户已授权 | AC identity grammar、Product Spec、acceptance matrix、required producer/test policy | 每个 live AC identity 唯一；dependency lock 保留 `AC-070`，API docs 迁移为 `AC-089`，两者各自保留正确行为、producer、test 和 evidence；历史引用可追溯 | `Product-Spec.md`、`Product-Spec-CHANGELOG.md`、`DEV-PLAN.md`、`docs/acceptance-matrix.md`、`scripts/acceptance_matrix{,_policy}.py`、`tests/contracts/test_dependency_version_policy_contracts.py`、`tests/contracts/test_acceptance_identity_uniqueness_contracts.py` 与相关 contract tests；本计划/矩阵由主控同步 | 串行；不与会修改 Product Spec/验收矩阵的 change 并行 | 单一 worktree；禁止局部顺手重编号 | 2-5 小时 | `已归档`；TDD、冻结 evidence、direct validator、fresh review 与主规格同步已闭合，归档路径为 `openspec/changes/archive/2026-07-28-acceptance-criteria-identity-uniqueness/` |
| 18 | `controlled-real-model-runtime` | 受控接入一个真实 text completion deployment，同时保留显式 fake 路径 | Phase 17 文档冻结并审查；Phase 17.1 已验证并归档；用户授权 | typed config、agent descriptor、`ModelRequest`/`ModelRoutePlan`/`ModelResponse`、budget/evidence、composition | deployment∩agent allowlist；请求只缩权；secret/endpoint/timeout/retry/bulkhead/price/capability；离线兼容与 opt-in smoke | `config/{schemas,settings,secret_files}.py`；`models/{providers,router,invocation}.py`；`adapters/models/pydantic_ai.py`；`runtime/services.py`；`registry/**`；profile/agent config；相关 tests/docs | **串行**。config/model/router/composition/tests/docs 全部共享 | 单一 worktree、单一 owner；禁止拆成并行 provider/config 子 change | 20-32 小时；真实 smoke 另 1-3 小时墙钟 | `计划`；目录未创建 |
| 18.1 | `controlled-model-streaming` | 在 Phase 18 route/provider lifecycle 上生产有界、可持久化、可恢复的 provider-neutral 普通文本增量，复用既有 RUN-006/CLI reader | Phase 17.1 与 Phase 18 已验证、fresh review、同步并归档；用户授权 | provider stream protocol、route/attempt、CanonicalEvent、event capacity/outbox、usage/budget settlement、RUN-006/CLI committed reader | bounded delta/coalescing；稳定 chunk identity；跨 chunk 安全；completed/usage/terminal 顺序；reader 断线不取消 run；取消/unknown 不重试不记零；Last-Event-ID 不重放 provider；offline/live 时延证据 | `models/{providers,router,invocation,_invocation_settlement,usage_events}.py`；Pydantic AI/fake adapter；`events/{capacity,bus}.py`；event/usage/evidence repositories 与 local/PostgreSQL sinks；runtime composition；API Contract；model/capacity/recovery/SSE/CLI tests/docs；migration 仅在 design 证明需要时纳入 | **串行**。provider、event capacity、settlement、recovery、tests/docs 是同一安全不变量 | 单一 worktree、单一 owner；只读威胁建模可并行，禁止把 stream adapter 与 capacity/outbox 拆开写 | 20-30 小时；若需 migration 暂按 24-36 小时；live smoke 另 1-3 小时墙钟 | `计划`；目录未创建 |
| 19 | `provider-neutral-structured-output` | 增加稳定 schema identity、结构化结果、验证与失败语义，不泄漏 SDK 类型 | Phase 18.1 已验收并归档 | Phase 18/18.1 route/provider/result seam；`ModelRequest`/`ModelResponse`；agent input/output schema refs；usage/evidence | text/non-stream 兼容；schema invalid/unsupported/repair exhausted 可追踪；修复尝试受预算和次数约束；structured streaming 仍非目标 | `models/providers.py`、新的 structured DTO/service、Pydantic AI adapter、registry schema refs、contract/eval tests、相关 docs | 串行；与 Phase 18/18.1 不并行 | 从 Phase 18.1 归档 HEAD 新开 worktree；不得在旧基线开发 | 16-28 小时 | `计划` |
| 20A | `provider-neutral-tool-call-contract` | 模型只产出稳定 tool intent；完成参数 schema/来源校验但不执行工具 | Phase 19 已归档 | structured result、tool intent DTO、`ToolRegistry` metadata、CanonicalEvent | provider SDK tool-call 不外泄；未知工具/无效参数零工具副作用 | model/tool DTO、adapter normalization、`tools/registry.py` 只读/解析 seam、events/tests/docs | 与 20B/20C 串行；认知调查可并行 | 独立 worktree可以，但只能在 Phase 19 新 HEAD 上；同一时刻不启动 20B | 8-12 小时 | `计划` |
| 20B | `policy-gated-tool-loop` | 经 ToolRegistry、PolicyEngine、audit、HITL 执行受控工具循环 | 20A 已归档 | tool intent、registry allowlist、policy decision、approval/checkpoint、context assembly | approval 前零副作用；agent/request 不能扩权；工具结果按 untrusted 输入回注 | runtime executor/loop、tools、policy、approval、audit、context、events、tests/docs | **串行**；共享核心 runtime/tool/policy seam | 单一 worktree、单一 owner | 9-15 小时 | `计划` |
| 20C | `durable-tool-loop-resume` | 冻结 loop/turn/tool-call identity，完成 checkpoint/resume、exact replay、unknown 与 terminal fencing | 20B 已归档 | run state、checkpoint、usage ledger、tool evidence、CanonicalEvent、queue/recovery | replay 不重复模型/工具副作用；turn/token/cost/time 有上界；unknown 不自动重试 | runtime lifecycle/state/continuation、storage repositories/migration（如确需）、worker、events、tests/docs | **串行**；与任何 runtime/storage hotspot change 冲突 | 单一 worktree；完成后重新冻结 Phase 21 基线 | 11-18 小时 | `计划` |
| 21-0 | `architecture-hotspot-rebaseline`（只读 inventory） | 用最新 CodeGraph、源码和测试固定 service locator、storage 扩散、run state 与两个语义 SCC 的真实 blast radius | Phase 17；可读到当时最新 HEAD | symbol/call graph、测试覆盖、文件集合；不改公共接口 | inventory 可复现；不把旧图或生成图当行为真相 | 只更新本计划、矩阵或该阶段获批的分析附件；不改生产代码 | 可与 Phase 18、18.1、19、20 的认知工作并行，不能抢写共享文档 | 无需独立实现 worktree；若更新矩阵，由主控独占 | 2-4 小时 | `计划` |
| 21A | `typed-agent-execution-services` | 用 typed capability container 取代字符串键 service locator，限制 executor 可见依赖 | 20C 已归档；21-0 重基线 | `build_agent_execution_services`、executor service injection、CLI/API/worker composition | 行为等价；缺失/错误 capability 启动或解析时 fail closed；不可序列化进 DTO/checkpoint | `runtime/services.py`、`runtime/executor.py`、registry executor seam、CLI/template composition、contract tests | 默认串行；很可能与 21B/21C 共享 composition | 单一 worktree；在重基线后冻结 owner | 8-14 小时 | `计划` |
| 21B | `storage-port-seams` | 将核心用例对具体 `SQLAlchemyStorage` 的依赖缩到最小 ports/UoW seam | 21-0；默认在 21A 后 | storage repository/UoW、audit/event/budget/runtime service constructors | SQLite/PostgreSQL 逐值一致；事务、锁、replay 与恢复语义不变 | `storage/**` ports/exports、受影响 service constructors、composition、contract/integration tests | 默认串行；只有按 domain 拆分且无共享 UoW/exports 时有限并行 | 首次 change 单一 worktree；后续 domain cut 逐项证明独立 | 12-20 小时 | `计划` |
| 21C | `run-state-transition-kernel` | 把分散状态分支收敛为显式、可穷举、可审计的 transition seam | 20C；21-0；通常在 21A/21B 公共 seam 稳定后 | run status、checkpoint/resume、queue claim、approval、terminal event、recovery | 合法 transition 完整；非法/重复/并发 transition fail closed；既有 API/event 语义不漂移 | `runtime/state.py`、lifecycle/continuation/orchestrator、run repositories、worker、tests/docs | **串行**；与 runtime/storage changes 高冲突 | 单一 worktree、单一 owner | 10-18 小时 | `计划` |
| 21D | `cross-domain-cycle-cuts` | 对重基线确认的两个跨域语义 SCC 逐个建立单向 seam | 21-0；相关 typed/storage/state seam 已稳定 | 以重基线记录的实际 symbol/API 为准；不得按历史名称猜边界 | 每次 cut 行为等价；依赖方向可机械验证；无新反向 import/service lookup | 重基线后按每个 SCC 分配互斥文件集、checker 和 contract tests | **有限并行候选**；只有两个 SCC 无共享接口/验收/文件/顺序依赖时才拆为 A/B | 默认串行；满足四项独立证据后可用两个 worktree | 10-20 小时 | `计划` |
| 21E | `architecture-checker-expansion` | 把已稳定且可机械判断的层依赖、public/internal seam、vendor/ORM/config 规则逐条接入 checker/contract/CI | 21-0；相关边界 change 已稳定 | import boundary declarations、checker diagnostics、CI required jobs | 新规则有稳定正反例；既有合法路径不误报；无法稳定机械判断的规则仍留在 review checklist | `scripts/import_boundary_check.py` 或独立 checker、contract fixtures、CI/Make 入口、维护文档 | 可与无共享规则/fixture/CI owner 的行为 change 做认知并行；集成写入串行 | 默认由主控集成；每批只加入一个可解释规则族 | 10-18 小时 | `计划` |

## 4. 首个 Change 的串行边界

`controlled-real-model-runtime` 不允许按“配置组、provider 组、测试组”拆为并行 worktree，原因是它们共同决定同一个安全不变量：

```text
typed deployment config
  -> deployment 与 agent allowlist 交集
    -> 请求缩权
      -> 冻结 route plan
        -> budget / policy / audit
          -> provider side effect
            -> usage / cost / latency / error settlement
```

以下文件或语义任一变化都会使其他部分的契约与 review 失效：

- `ModelSettings`、agent descriptor 与 profile/agent YAML shape；
- `ModelRequest`、`ModelRoutePlan`、`ModelRouter.plan/execute`；
- provider factory/client 的 endpoint、credential、timeout、retry 和 bulkhead；
- shared budget 的价格 identity、reservation 与 settlement；
- CLI/API/worker composition；
- negative contract、integration、smoke 与维护文档。

可以并行派 sub-agent 做只读代码调查、威胁建模或独立审查，但写入由单一 owner 在一个 worktree 串行整合。

Phase 18.1 同样不能拆成“provider stream 组”和“event/usage 组”。一旦 adapter 观察到 delta，event capacity、durable prefix、部分 usage、budget reservation、retry 禁止和 terminal fencing 就成为同一个不可分割的安全不变量：

```text
frozen route / budget / stream capacity
  -> provider delta normalization
    -> cross-chunk safety
      -> durable chunk commit
        -> completed / usage settlement
          -> run terminal
```

RUN-006 / CLI reader 可以保持独立的 transport seam，但其 contract tests 与 API 文档属于 Phase 18.1 集成范围。reader 断线只停止读取；任何 worktree 都不得借 reconnect 引入 provider cursor、SDK resume token 或第二次 provider 调用。

## 5. 共享接口和验收约束

### 5.1 Phase 18 → 18.1

- Phase 18.1 只扩展 Phase 18 已冻结的 deployment/route/provider/cancel/usage seam，不得重新引入请求直选 provider、endpoint 或 credential。
- Phase 18 的非流式 text behavior、默认 fake/local 离线门禁和 opt-in live smoke 必须保持兼容。
- 任何 delta 之前仍须完成 route、budget 和受信最大 event capacity reservation；已观察 delta 后禁止 retry/fallback 到新 attempt。
- SSE/CLI 只读 committed CanonicalEvent；subscriber 生命周期不拥有 durable run/provider 生命周期。

### 5.2 Phase 18.1 → 19

- Phase 19 只扩展 Phase 18/18.1 已冻结的 deployment/route/provider/result seam，不得重新引入由请求直接选 provider 的旁路。
- text-only public behavior 必须保持兼容；structured capability 必须显式声明，未声明时 fail closed。
- price、timeout、retry、attempt 和 usage evidence 继续由同一 route identity 关联。
- Phase 18.1 只交付普通文本增量；structured delta 拼装、repair 和 schema stream 仍须在 Phase 19 或其后独立契约中决定，不能因已有 text stream 自动开放。

### 5.3 Phase 19 → 20

- tool intent 与业务 structured output 使用不同判别类型，不能靠“某个 JSON 字段看起来像 tool”触发执行。
- provider adapter 只归一化 tool intent，不拥有 ToolRegistry、PolicyEngine、HITL 或实际执行权限。
- Phase 20 三个 change 共享 tool intent、loop identity、CanonicalEvent 和 replay 验收，因此默认是关联 change；各自 strict/review 之外还需联合审查。

### 5.4 Phase 20 → 21

- Phase 21 是结构演进，不得借 typed services、ports 或 state kernel 改写已经验收的 tool loop 行为。
- characterization/contract tests 必须先固定 Phase 20 的公开行为，再移动依赖方向。
- 任何新 DTO、port 或 event 状态如果改变公开 schema，先返回行为 change 更新 Product Spec/API Contract/OpenSpec，而不是藏在 refactor 中。

## 6. 并行与 Worktree 决策规则

### 6.1 Sub-agent

适合并行：

- 读取不同模块并返回 symbol/call-path 证据；
- 独立威胁建模、测试缺口分析、契约审查；
- 在不写共享文件时验证技术假设。

不适合并行：

- 多个 Agent 同时修改同一配置、DTO、router、composition、公共 export、矩阵或集成测试；
- 用一个 Agent 的摘要代替另一个 Agent 读取完整 Spec/change 原文；
- 让执行型 Agent 自行改变 change 范围、commit、sync 或 archive。

### 6.2 Worktree

只有同时满足以下四项，才允许两个写入型 change 并行 worktree：

1. 无顺序依赖；
2. 无共享公共接口或 schema；
3. 无共享验收、迁移或 review gate；
4. 无文件所有权冲突，包括集成文件和文档。

证明必须写入本矩阵并绑定当时的 Git HEAD。只满足“文件列表暂时不同”不够；共享 DTO、共同 budget/replay 语义或同一 smoke 都会使 changes 相关。

### 6.3 集成文件

`Makefile`、CI workflow、checker manifest、公共 `__init__.py` export、`DEV-PLAN.md`、本计划与本矩阵默认由主控独占。并行 worktree 只生产自己的稳定 seam 和证据，最后按 DAG 在主控 worktree 串行接入集成文件。

## 7. 状态更新协议

每次 session 开始：

1. 运行 Git/OpenSpec 基线命令；
2. 读取 active change 全部 artifacts；
3. 把本矩阵状态与磁盘事实比较；
4. 确认 owner 和 worktree 后才写入。

每次 session 结束：

1. 只把有证据的行推进一个状态；
2. 更新直接依赖、共享 seam、验收和文件集的任何变化；
3. 记录 external-blocked，而不是把未跑的真实 smoke记成通过；
4. 把下一 owner、下一文件和下一门禁同步回上位计划的 Handoff Snapshot。

如果 change 产生了超出矩阵的共享接口、验收或文件，立即停止并行写入，先更新 proposal/design、本矩阵和 review 范围。任何 post-review 实质 diff 都使旧 verdict 失效。

## 8. 当前下一步

1. Phase 17.1 已同步主规格并归档：dependency lock 保留 `AC-070`，API docs 迁移为 `AC-089`，全局唯一性 checker、独立 policy、矩阵与 changelog 已闭合，历史 archive 未被改写。
2. fresh 实现 review 已复用冻结验证证据完成静态审查，Stage 1/2 PASS；状态类 artifact finding 的用户 `owner-waived` 仍不写成 artifact reviewer PASS。
3. 实现冻结 identity `8789075d…42d15` 下 18 个 CI producer gate 全部 PASS；`test-aggregate` 为 `1352 passed, 223 skipped`，direct validator 为 `98/98`，strict validation 与 `git diff --check` PASS；完成状态记录按 owner 裁决不重跑 evidence。
4. 当前唯一动作是由 owner 决定是否创建 scoped local commit；未经授权不 commit、push，也不创建 Phase 18 change。
5. scoped local commit 收口后，才由用户决定是否授权 `controlled-real-model-runtime`；Phase 18 完成并归档且再次授权后才创建 `controlled-model-streaming`。

当前不存在可并行启动的实现 change。
