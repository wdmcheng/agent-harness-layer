# 工程原则：围绕不变量渐进演进

[English](engineering-principles.md) | [简体中文](engineering-principles.zh-CN.md)

适用读者：需要修改 Agent Harness Layer 或复制后 service app 架构、跨模块边界的维护者，以及受托执行修改的 AI / Agent。

导航：[根 README](../README.zh-CN.md) · [架构边界](architecture/README.zh-CN.md) · [五层两翼开发 Agent](building-an-agent.zh-CN.md) · [Adapter 合同](adapter-contracts.zh-CN.md) · [ADR-0001](adr/0001-p0-service-boundaries.zh-CN.md) · [ADR-0002](adr/0002-vendor-adapter-isolation.zh-CN.md)

本文是约束后续变更的可执行维护合同，明确区分当前已有优势、已知热点和目标依赖规则。写出目标规则，不代表当前每条路径已经满足它。

`MUST`、`MUST NOT`、`SHOULD` 是规范性用词；当前行为仍以仓库合同、生产代码、测试和已接受 ADR 的证据为准。

## 1. 先找不变量，不先套模式

选择类、package 或设计模式前，先写清楚：

1. 必须保持的行为，包括 identity、trust、顺序、事务、恢复和失败语义；
2. 真实发生变化的轴，例如 provider、持久化后端、部署 profile、生命周期状态或 policy；
3. 能隔离这条变化轴的最小公共 seam；
4. 能证明预期变化的第一条失败合同。

然后选择最小充分实现。只有一种行为、一个实现时，直接函数或已有 seam 优先。只有在模式能隔离已证实的变化、保护不变量或建立必要测试 seam 时，才引入模式。

本仓库**不要求**每个模块都套一个有名字的设计模式，也不允许为了让目录看起来分层就全 package 重写。优先完成窄范围纵向切片；consumer 迁移期间保留兼容层；只有证据证明已无 consumer 依赖时，才删除旧路径。

抽象过早的警告信号：

- 只有一个 caller，也没有可信的第二种实现或 policy 边界；
- 每个方法都原样转发，仍泄漏相同具体类型；
- 只改名或移动目录，没有减少耦合；
- 隐藏了顺序敏感的副作用或失败语义；
- 唯一验收理由是“架构更干净”。

## 2. 五层两翼依赖合同

运行请求链路和源码依赖是两个视角。运行时可以从 Access 流向 Infra，但核心代码 MUST 依赖稳定合同，不能依赖具体基础设施。

### 允许的源码依赖

| 区域 | 负责 | 可以依赖 | 禁止依赖 |
|---|---|---|---|
| 1. 接入与交互层 Access | HTTP/CLI 输入、认证注入、校验、transport DTO 转换、错误信封 | Runtime facade 与公共 DTO/protocol 合同 | Engine 具体实现、具体 storage、provider SDK、可变 runtime 内部状态 |
| 2. 编排与运行时层 Runtime | run 生命周期、幂等、checkpoint/resume、审批、delegation、budget | Engine executor 合同、窄 tool capability、repository/provider/facade 合同、`CanonicalEvent` | template route、业务 Agent 具体实现、vendor SDK object |
| 3. 引擎与认知层 Engine | 类型化业务输入输出、推理与 context 决策 | provider-neutral model/retrieval/tool protocol 与 DTO | Access 代码、Infra 具体实现、ORM session、vendor SDK |
| 4. 工具与能力层 Tools | 类型化 capability schema、授权、workspace/policy/HITL 门禁 | 窄 policy、audit、workspace、provider 与 DTO 合同 | transport route、无关 Engine 具体实现、不受控 SDK client |
| 5. 基础设施与数据层 Infra | storage、queue、provider、retrieval、secret 与业务系统 adapter | 自身实现的公共 protocol/DTO，以及 adapter 内部 vendor library | 业务 Agent、template entry point，或从核心合同反向依赖 adapter |
| 左翼：Eval Gate | approved 行为证据、regression/holdout/acceptance 执行 | 公共 Agent/runtime 入口与授权 evidence reader | 私有状态改写、直接 ORM 访问、把 draft 自动提升为 approved case |
| 右翼：Observability | 本地优先的 event、usage、audit、trace 与可选 provider fan-out | `CanonicalEvent`、授权 reader、`TelemetryFacade`、observability adapter 合同 | 原始 secret/provider response、业务决策，或因 provider 失败删除本地 evidence |

正常依赖与装配形态如下：

```text
Access -> Runtime facade -> Engine executor
                         <-> Tool capability contracts

Runtime / Engine / Tools -> 公共端口（protocol、repository、provider、facade）
Infra adapter             -> 实现这些公共端口
组合根                     -> 选择并注入具体实现，负责资源释放

Eval Gate      -> 公共执行与 evidence reader seam
Observability  <- 各相关阶段提交的 CanonicalEvent / TelemetryFacade 调用
```

`Engine <-> Tools` 表示通过 capability 合同发起受控调用，不允许任意具体实现模块相互 import。

### 每次跨边界都要遵守

- 同一进程内跨层只能通过批准的 DTO、protocol、facade、repository/UoW、provider 合同或 `CanonicalEvent` seam。
- 跨进程只交换版本化、可序列化的 DTO、command、event 和稳定 ref；接收进程必须在自己的组合根中重建 repository、provider、client 和其他依赖。
- DTO 只携带已校验、可序列化的值和稳定 ref；ORM session、SDK object、client、closure、credential 与进程内可变对象不得越界。
- Vendor SDK import 只能位于已批准的 adapter/integration boundary；adapter 负责把请求、响应、错误、脱敏和降级转换为 provider-neutral 合同。
- 组合根（composition root）选择具体实现，并负责其进程/profile 生命周期、启动和释放；业务模块不得临时自行构造基础设施。
- 禁止全局可变单例（singleton）。进程内可以只有一个经注入的资源实例，但生命周期数量不等于可以暴露隐藏全局状态。
- 跨层便捷不能成为绕过 policy、identity、budget、workspace、event 或事务边界的理由。

## 3. 延续已有优势，局部隔离热点

### 值得继续使用的已有模式

| 已有做法 | 当前价值 | 继续使用的条件 |
|---|---|---|
| Provider/Strategy 加 Adapter | model、embedding、retrieval、MCP、queue/runtime 与 observability 的变化可以保持 provider-neutral | 真实变化共享同一行为合同，且 adapter 能封闭 vendor 特有错误和 payload |
| Repository 与 Unit of Work | SQLAlchemy query 和 commit/rollback 所有权留在 storage seam 内 | use case 需要持久化或一个明确原子边界；返回 DTO/record，不返回 session |
| Facade | `TelemetryFacade` 和公开 runtime seam 协调 policy、evidence 与 provider-neutral 行为 | 多个有顺序要求的 collaborator 共同完成一个高内聚 use case |
| Factory 与组合根 | service/CLI runtime 装配和 factory 选择 profile 对应实现 | 构造过程需要配置、生命周期所有权或一组受控 collaborator |
| EventBus 与 `CanonicalEvent` | Runtime、API 和 eval 可以提交稳定、本地优先的 evidence，不把 caller 绑定到 telemetry vendor | 该事实需要持久化/审计，且 consumer 不控制 command 结果 |
| 对象能力与执行期绑定服务 | 一次 execution 只收到装配路径授予的 capability | capability 必须显式、窄、类型化，且不能由请求数据伪造 |
| 事务性 outbox、幂等恢复与 Saga-like 生命周期 | 审批、queue、run、budget、delegation 和发布步骤可以恢复，不盲目重放副作用 | 多步骤持久化工作流跨事务或进程，并有明确所有权、补偿/对账与 terminal 规则 |

“Saga-like”只描述现有持久化生命周期纪律，不表示每条工作流都实现了通用 Saga 框架。

### 需要局部演进的热点

| 热点 | 当前信号 | 推荐的局部方向 |
|---|---|---|
| `registry/runtime/tools/adapters` | discovery、execution、capability construction 与 vendor integration 的所有权相邻，导航面较宽 | 每次只厘清一条变化轴；先依托现有公共 seam 分开 registry metadata、runtime lifecycle、capability policy 与 vendor translation，再判断是否需要移动文件 |
| `models/events/observability/storage` | invocation、usage/capacity、event publication、telemetry 与 durable state 共享顺序和事务语义 | 只提取能保持本地 evidence 优先与原子不变量的窄 coordination port/facade；不要创建通用“平台服务” |
| `storage` 外依赖具体 `SQLAlchemyStorage` | 许多非 storage collaborator 接收具体 engine/UoW factory | 每次迁移一个 use case 到最小 repository/UoW/capability protocol；具体构造留在组合根，caller 迁移完成前保留兼容性 |
| `AgentExecutionContext` 字符串 service lookup | 绑定服务通过 `Mapping[str, object]` 授予，再按字符串名取回 | 停止增加不受治理的字符串键；按高内聚能力组引入类型化 capability bundle 或窄 protocol，迁移期间提供兼容 adapter |
| Run continuation 与状态分支 | resume、approval、queue、recovery 与 terminal 路径持续增加条件分支 | 显式定义 state、event、guard、允许 transition、副作用和 idempotency key；先有失败合同覆盖，再把一个分支族迁入 transition table |
| 真实 model adapter 与装配 | 当前共享 execution-service builder 只支持 fake provider，选择其他 provider 会被拒绝 | 通过 provider-neutral 合同、adapter、配置、脱敏/错误测试和组合根选择增加真实 provider；绝不把 SDK client 传给业务 Agent |

这些只是优先级信号，不是大爆炸重构许可。只有产品需求、缺陷、可运维风险或可度量维护成本给出验收目标时，才修改对应热点。

## 4. 把设计原则落实为项目检查

| 原则 | 项目规则 | 审查问题 |
|---|---|---|
| 单一职责原则（SRP） | 一个 module/service 只承担一组高内聚的变更原因，不以“只有一个 class/verb”冒充单一职责 | provider 升级、生命周期变化和 API 变化会不会因为不同理由同时修改这个单元？ |
| 开闭原则（OCP） | 当语义真正可替换时，在稳定合同后增加 provider、backend、policy 或 handler | 新变化能否加入而不让所有 caller 都新增分支？ |
| 里氏替换原则（LSP） | 实现不仅保持 method signature，还要保持校验、副作用顺序、错误封闭、幂等与可观测性 | fake/local 换成 real/service 后，原本安全的 caller 会不会出现泄漏、额外重试或不同成败语义？ |
| 接口隔离原则（ISP） | Consumer 只接收自己使用的最小 capability | caller 为一次 repository/provider 操作是否被迫接收整个 storage/runtime/service map？ |
| 依赖倒置原则（DIP） | 核心 policy 与生命周期依赖 provider-neutral 合同，adapter 向内依赖这些合同 | 业务/runtime module 是否 import 了本应由组合根提供的 ORM、driver 或 vendor SDK？ |
| 高内聚、低耦合 | 一起变化的数据和规则放在一起；跨所有权通过稳定 seam | 一次修改是否要求跨无关 package 协调修改，或了解私有字段？ |
| 信息隐藏 | 隐藏 session、client、credential、锁细节、provider payload 与状态表示 | caller 能否观察或修改合同没有承诺的实现细节？ |
| 显式副作用 | 持久化、queue publish、tool/model call、approval 与 event fan-out 在 use-case 顺序中可见 | constructor、property、decorator 或隐式 lookup 会不会意外触发外部工作？ |
| 失败关闭（fail closed） | 未知 identity、permission、policy、state、contract 或 durable outcome 必须阻止执行，或进入有边界的 recoverable/`needs_review` 状态 | 缺配置、未知 transition 或 provider 歧义会不会静默放行？ |
| 可测试 seam | time、ID、provider、storage、queue 与外部副作用可从公共边界注入或控制 | 不依赖隐藏全局状态或真实 vendor account，合同测试能否覆盖成功、拒绝、失败、重试和恢复？ |

不要因为两段代码长得相似就立即去重。不同不变量或变化轴被错误合并时，共享抽象的代价高于重复代码。

## 5. 模式选择目录

| 模式 | 适用信号 | 不适用信号 | 本项目现有或候选落点 |
|---|---|---|---|
| 适配器 + 策略（Adapter + Strategy） | 两种以上 provider/backend 共享稳定 caller 语义；实现随 profile/config 变化 | 变化承载不同业务含义，或 wrapper 仍泄漏 raw SDK type | 继续使用 `ModelProvider`、retrieval/embedding provider、event/queue/runtime adapter；真实 model 支持落到 `adapters/models` 与组合根选择 |
| 工厂 / 组合根（Factory / composition root） | object graph 随 profile 变化、需要已校验配置，或负责 startup/disposal | 纯本地对象构造简单，或 factory 只是给 constructor 改名 | 继续使用 service-app/CLI runtime composition、`build_agent_execution_services` 与 `ToolRegistryFactory`；生命周期集中在这里，不要遍地增加 factory |
| 仓储 + 工作单元（Repository + Unit of Work） | 需要隔离持久化 query 或一个原子 transaction，不让 use-case policy 依赖实现 | 纯计算、transport 转换，或 caller 完全不需要持久化 | 继续使用 storage repository/UoW；引入窄 port，逐步减少 `storage` 外对具体 `SQLAlchemyStorage` 的依赖 |
| 门面（Facade） | 一个高内聚 use case 必须按顺序协调多个 collaborator | “门面”变成杂物箱、隐藏无关操作或抹掉失败细节 | 继续使用 `TelemetryFacade` 和 provider-neutral `RunOrchestrator` seam；语义耦合被证实后，再考虑窄 invocation/publication facade |
| 观察者 / 事件总线（Observer / EventBus） | 多个可选 consumer 响应同一事实，publisher 不应绑定 provider | caller 需要立即得到 command 结果，或 consumer 成功是当前 transaction 的必要条件 | 继续使用 `EventBus` 与 `CanonicalEvent`；先落本地 durable evidence 再做可选 fan-out，进程内 bus 永远不是分布式锁 |
| 状态 / 状态转换表（State / transition table） | 有限生命周期反复出现 state/event/guard 组合，非法 transition 必须失败关闭 | 只有简单线性函数、没有分支生命周期，或 state 无法有意义地枚举 | 候选用于 run/approval/continuation/recovery；编码允许 transition 与副作用/幂等规则，再渐进迁移分支族 |
| 命令（Command） | 工作需要跨 queue/process、重试/重放，或需要携带可审计稳定 intent | 只是无独立生命周期的本地同步调用，或 command 需要携带进程内可变对象 | 延续稳定 run-queue DTO；continuation/publication action 可候选类型化 command，并显式带 idempotency 与 authorization ref |
| 装饰器（Decorator） | 同一正交关注点包裹多种可替换调用，同时保持原合同 | 隐藏顺序敏感 workflow/policy、改变返回/错误语义，或 retry 可能重复副作用 | 候选用于 provider call 周围有边界的 metrics、tracing、redaction、timeout 或 retry；decorator 顺序必须显式并有合同测试 |
| 断路器 / 舱壁（Circuit Breaker / Bulkhead） | 远程 provider 有持续失败/延迟风险，需要有界并发、隔离或受控降级 | 本地 validation、durable evidence write 或必需 policy check；breaker 不能把失败变成功 | 候选位于真实 model、MCP、telemetry 和未来 gateway adapter；按依赖隔离 pool/budget，可选 provider 降级期间仍保留本地 evidence |
| 单例（Singleton） | 资源具有由组合根选择并释放的**进程级生命周期** | 隐藏全局访问、可变 registry/cache、测试顺序依赖，或声称跨进程所有权 | 只在 composition 中描述 DB/client/provider 生命周期并显式注入；不得新增 `get_instance()`、module-level 可变全局或 service-locator 访问 |

只有每个模式承担不同职责时才组合使用。例如 factory 可以选择 adapter strategy，并通过 protocol 返回它；这不表示 policy、retry、lifecycle transition 和 storage 都该塞进同一 factory。

## 6. 架构变化流程

修改依赖、生命周期、provider、持久化或边界时，按以下顺序执行：

1. **写清合同。** 记录不变量、变化轴、受影响的五层两翼区域、失败行为、迁移边界和所需证据。
2. **先更新真相源，再改代码。** 产品行为/范围更新 `Product-Spec.md`；公共字段或 transport 行为更新 `API-Contract.md`；难逆转架构决策写 ADR；增量行为合同更新对应 OpenSpec change。不要为了凑清单创建或修改无关产物。
3. **绘制当前依赖面。** 仓库有 `.codegraph/` 时，先用 CodeGraph，再做文本搜索或人工遍历。找出 caller、具体类型泄漏、所有权和兼容 consumer。
4. **先建失败合同。** 增加能证明缺失行为/边界的失败合同测试或 checker case。文字 TODO、与变更无关的通过测试、单独 `openspec validate` 都不算红色证据。
5. **实现最小纵向切片。** consumer 无法一次迁完时，旧 seam 作为显式兼容 adapter 保留。避免全目录移动和投机接口。
6. **确定性验证。** 运行聚焦合同测试、import boundary checker、静态质量检查，以及能证明边界的最小真实 integration/smoke。不能拿本地 mock 或架构图证明跨进程行为。
7. **审查冻结证据与合同。** 对照已更新真相源检查实现和负路径。使用 OpenSpec 时，实现/收口前必须分别完成严格校验与独立合同审查，两者不能相互替代。
8. **同步说明。** 在同一个语义变更中更新受影响的中英文文档、ADR 链接、架构图源/预览和迁移说明。只有 caller 与测试证明兼容路径已无使用时，才删除它。

只有需求触发热点时，才建议按以下顺序渐进处理：

1. 先用合同/checker 测试冻结当前边界；
2. 增加类型化 execution capability，但不立即删除字符串 service 兼容；
3. 把一个依赖 storage 的 use case 迁到窄 repository/UoW port；
4. 把一个 continuation 分支族编码为显式 transition；
5. 通过组合根增加一个真实 model adapter；
6. 基于缩小后的依赖图重新评估 package 所有权，再决定是否移动文件。

每一步都必须能独立审查、独立发布；后续步骤不是前一步的验收条件。

## 7. 确定性规则进入 checker 与 CI

文档负责解释意图和判断。机械可判定的规则，只有在违规时能让 checker 或合同测试失败，才算已经执行。

当前 `scripts/import_boundary_check.py` 已覆盖以下证据：

- core package metadata 不得反向依赖 template/examples；
- service-app template 必须通过已声明 dependency/workspace source 解析 core package；
- 已配置 vendor SDK import 必须留在已批准 adapter/integration path；
- template app/Agents 与 examples 不得直接 import SQLAlchemy session type。

它**不能**证明 runtime sandbox、动态 import 安全、provider 可替换性、状态机正确性，也不能证明所有非 storage module 已经依赖窄 storage port。

| 规则类型 | 可执行门禁位置 |
|---|---|
| 禁止 layer/vendor import | boundary 声明加 `scripts/import_boundary_check.py`，由 `make quality`/CI 调用 |
| DTO/schema/serialization 不变量 | 合同 schema、round-trip 与负路径测试 |
| Repository/UoW 事务与并发不变量 | 合同测试；涉及数据库方言/进程行为时增加真实 PostgreSQL integration |
| 允许的生命周期 transition 与 replay 行为 | transition-table 合同测试，覆盖 illegal、duplicate、crash 与 unknown-outcome |
| 失败关闭 identity/policy/configuration | API/CLI/runtime 负路径合同测试 |
| Provider 降级与隔离 | Adapter 合同测试加有边界 integration；本地 evidence 写入失败仍是主失败 |
| 进程生命周期与资源释放 | 组合根测试和对应 local/service smoke 路径 |

增加新的机械可判定规则时，必须在同一变更中新增或扩展 checker，再宣称已执行。不得静默增加 allowlist 例外：先更新治理合同/ADR，解释有边界的必要性，并增加 regression case。

## 8. 人与 Agent 的完成清单

提出设计前：

- [ ] 已明确不变量和变化轴。
- [ ] 已从当前 checkout 读取 caller 与所有权；存在索引时先使用 CodeGraph。
- [ ] 所选模式小于问题本身，并明确了不适用情况。
- [ ] 五层两翼依赖与 vendor 边界保持有效；若要改变，已先更新合同。

实现验收前：

- [ ] 实现前已有失败合同/checker。
- [ ] 副作用、事务所有权、幂等、恢复和失败关闭都可见。
- [ ] 具体 adapter 由组合根选择并释放，没有增加全局可变 singleton。
- [ ] 跨边界数据保持 provider-neutral 且可序列化，没有泄漏 session、SDK object、credential 或 closure。
- [ ] 聚焦测试与相关真实 integration/smoke 证据通过。
- [ ] 新增的机械可判定规则已进入 checker/CI。
- [ ] 中英文维护文档事实等价并互链。

这些问题无法回答时就停在规划阶段，不要用更多模式掩盖合同缺失。

## 9. 文档维护

- 本中文文件与[英文版](engineering-principles.md)必须在同一变更中保持事实等价。
- 已接受的架构决策要链接对应 ADR，并区分当前实现与候选方向。
- 模式选择规则或依赖合同变化时更新本文；可执行语法变化时更新对应 checker/test。
- 架构图与部署事实以[架构边界](architecture/README.zh-CN.md)为入口，具体 seam 和验证细节以 [Adapter 合同](adapter-contracts.zh-CN.md)为入口；不要在本文复制易漂移 inventory。
