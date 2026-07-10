## Context

registry、fake model、retrieval、context assembly、file/shell tool、policy、approval、artifact、eval 和 telemetry 已有 provider-neutral 公共 seam，但模板尚无可执行示例逻辑。四个示例既要展示不同扩展点，又不能直接绑 vendor SDK、ORM 或把后续 workflow/eval experiment 能力提前塞进来。

## Goals / Non-Goals

**Goals:**

- 为四个示例提供 typed request/result、依赖注入式执行 seam、local/fake 运行装配和确定性 eval。
- 真实穿过 retrieval/tool/policy/artifact 等公共边界，而不是只返回 prompt 文案。
- 让 registry、CLI run、approved eval 和 local trace 共同构成用户可复核证据。

**Non-Goals:**

- 不实现通用 agent plugin ABI、动态加载任意 Python callable、复杂 graph workflow 或完整示例产品。
- 不新增 HTTP endpoint，不修改 provider SDK，不实现 Phase 12.5 dataset split/experiment/accept。

## Decisions

1. **核心增加最小 provider-neutral `AgentExecutor` seam，模板提供具体 executors。** config 用 module/callable string 声明入口，`AgentRegistry` internal resolver 只允许入口位于 config 所属 agent package 并验证 protocol；public descriptor 不增加 callable/module/path 字段。`RunOrchestrator` 在 started event 后调用 executor，以 typed completed/waiting/failed result 推进同一 run。替代方案一是 template-local runner 与 CLI evidence 拼接；拒绝，因为不会真实执行 agent。替代方案二是把 callable 放进 public descriptor；拒绝，因为不可序列化且扩大攻击面。
2. **每个 agent 自己拥有 schema 和薄业务编排。** 共享 runtime 只定义 context/result/eval 形状，不放具体分类、检索或工具策略。替代方案是一个巨型 switch 文件；拒绝，因为四种能力会互相污染并突破文件规模门槛。
3. **RAG 强制穿过 `ContextAssembler`。** executor 调用 `RetrievalProvider` 后使用 `retrieval_results_to_context_fragments`，再通过注入的 assembly service 管理 UoW、token budget、持久化 trace，最后把 `assembled_text` 交给 `ModelProvider`。业务 agent 不手工拼接原始 chunk；无结果返回 `no_source`。
4. **Ticket triage 用可解释规则形成 typed classification，同时调用 fake model 记录推理 seam。** fake provider 当前是稳定 echo，不具备 JSON classifier；因此 P0 分类由明确关键词/阈值确定，model response 只作为可替换 provider evidence。替代方案是解析 fake echo 冒充模型分类；拒绝，因为测试无法证明结构化语义。
5. **Repo analyst 与 dev assistant 只接收 `ToolRegistry`；approval resume 必须恢复真实 continuation。** repo analyst 的 registry 强制只有 file read/search/list。dev assistant 首次调用遇到 `require_approval` 时，executor 把待执行 tool/action/resource、arguments 的 artifact ref + SHA-256、executor ref 和完整关联字段写进脱敏 checkpoint，orchestrator 再创建 approval record。approve 与 deny 必须通过同一个 repository 条件更新做原子 resolution 仲裁：deny 先赢时 public status 变为 denied、approve 无法取得 lease；approve lease 先赢时 public status 仍为 waiting、并发 deny 返回 `409 approval.resolution_in_progress` 且不得改写状态。lease/internal state 不进入 `ApprovalRecord` 或 OpenAPI。approve 胜出后构造 `ApprovalGrant`，让 `RunOrchestrator.resume_run` 调用 executor 的 resume seam；executor 以 grant 调用 `ToolRegistry.call_approved`，不能直接完成 run。只有真实 tool result 与 run terminal 状态落库后，public approval 才更新为 approved。仲裁失败方只记录冲突 audit，不得发布第二个有效 resolution event。
   `call_approved` 在 handler 前插入 nullable unique `tool_invocations.approval_id` claim，并绑定 tenant/agent/run/action/resource/args hash；完成后写 result artifact/ref。private lease 使用 `resolution_claimed_at` 作为 owner timeout/续租时间，默认恢复窗口为 300 秒。若进程在提交 raw `claimed` lease 后、tool claim 前硬退出，后续真实 API/CLI resolve 只有在 lease 过期且确认不存在对应 tool claim时，才能以条件更新换发新的 fencing id；未过期 lease保持 in-progress，已有 claim则进入既有确定性结果恢复或 needs-review 分支。`ApprovedToolExecutor` 必须在创建唯一 claim 的同一 UoW 内按当前 fencing id 续租，旧 owner 或竞态失败者立即停止且不得调用 handler。若已存在 executing claim 而无 result，系统把私有 resolution state 标记 `needs_review`，public status 保持 waiting，并拒绝自动重放外部副作用。若 handler 已返回并持久化确定性的 failed result，则 run 进入唯一 failed terminal，但 public approval 仍更新为 approved，因为该状态表达人工允许执行而非动作成功；private lease 正常封存，只发布一次 approved resolution event/audit，且不进入 `needs_review`。event sink 写入前失败或写入后丢失确认时，approve 标记 `recovery_pending`，deny 保持 `denied_pending`，下一次真实 API/CLI resolve 调用使用稳定 event id 补齐 `run.resumed`、terminal 和 resolution evidence 后仍返回原 409；`ApprovedToolExecutor` 只把当前 lease 的 `claimed`/`recovery_pending` 视为可执行恢复态，unique claim 继续阻止 handler 重放。重复 approve/resume 读取 completed/failed 结果或返回 invalid transition，绝不再次执行 handler。这提供正常路径“审批前 0 次、审批后 1 次”和持久化 at-most-once 防重放；不虚构跨崩溃的外部 exactly-once。deny 不创建 execution claim 且永不调用 handler。
6. **扩展现有 EvalRunner，而不是第二套 runner。** 核心 `EvalRunner.run_file_dataset` 增加可选的 approved case executor protocol；模板只提供 `ExampleEvalAdapter`，负责把 case 交给同一个 agent executor。score 始终走既有 `ScoreSink -> local evidence -> TelemetryFacade/provider fan-out`，provider 失败沿用 degraded 语义；draft 仅计数。
7. **Trace 使用现有 provider-neutral observability seam。** agent executor 把 model/retrieval/context/tool refs 交给 `TelemetryFacade`/local sink，记录 agent id、case/run/trace id、status 和脱敏 summary；大 payload 只留 `artifact_ref`，不再创建 template-local trace writer。
8. **Phase 12 三个 change 串行，并按文件所有权矩阵做局部补丁。** `service-app-template-surface` 先完成 app factory、base CLI/Makefile/API contract、standalone bootstrap/smoke 和 README 主体；本 change 后执行，主改 core executor/registry/runtime/approval/tool/eval/migration、`APR-002`、API/CLI composition、basic/fake 迁移和四示例，并对 Makefile/README 追加 example targets/link；`agent-scaffold-cli` 最后只增加 scaffold command/generator、root discovery和最终组合验收。本 change 扩展 retrieval foundation 已有 RAG config/eval，不宣称独占整个 `agents/examples/**`，也不重写模板入口结构。

## Affected Surfaces

- `agent_harness.runtime` 的 executor DTO/Protocol 与 orchestrator execution path；registry 的 private executor resolver。
- approval/tool storage repository 与 migration：不公开的 approval resolution lease/state、tool invocation `approval_id` unique execution claim、result ref 恢复。
- `templates/service-app/agents/examples` 下的四个 executor、schema、config 和 approved eval 数据。
- 模板 tests、Makefile/CLI 的 example run/eval 入口，以及 local trace/score 输出目录。
- `EvalRunner` 的可选 case executor seam、模板 example adapter 和 `docs/examples.md`；不新增数据库模型或 API endpoint。

## Change Dependency / File Ownership Matrix

| Surface | Primary change | Later allowed patch | Boundary |
|---|---|---|---|
| `API-Contract.md` HLT/APR-001A/EVL-001..003、app health/factory/serve、standalone bootstrap/smoke | `service-app-template-surface` | P0 只更新 APR-002 与 composition tests；scaffold 不改 HTTP | template 先提交，后续只做字段级局部补丁 |
| core registry/runtime/approval/tool/eval、migration、APR-002、basic/fake executor迁移 | `p0-example-agent-flows` | scaffold 只消费最终 executor contract | 不保留 fake fallback，不让 scaffold 自建第二套 resolver |
| RAG config/eval foundation | `retrieval-rag-foundation`（active dependency） | P0 扩展为可运行 executor与 Phase12 approved eval | 保留 retrieval/citation/trust 契约，不覆盖其 provider/storage边界 |
| template `agents/examples/**` 的 Phase12业务实现 | `p0-example-agent-flows` | scaffold 不修改既有示例 | basic/fake 仅作回归 fixture；四示例各自独立目录 |
| core CLI | P0 负责 run composition；`agent-scaffold-cli` 负责 scaffold group | 按提交顺序局部增加命令，不等价改写已有 group | 完整 help 在最终组合验收复扫 |
| template Makefile/README/docs | template change 建主体；P0 追加 examples；scaffold 追加新增 agent流程 | 每个后续 change 只追加自己的入口/链接 | copy-out、Phase12.5/13 边界由最终组合测试保护 |
| 已生成 agent 的 runtime 回滚兼容 | `agent-scaffold-cli` 持有 agent inventory 与 fail-closed preflight；P0 持有 executor compatibility seam | 最终组合验收覆盖生成后回滚 | 未迁移/隔离的 executor config 存在时不得移除 resolver/runtime seam，不自动删除用户 agent，不恢复 fake fallback |
| Phase 12组合验收 | `agent-scaffold-cli`（最后执行） | 无；验收后任何 diff重置 | 覆盖 template + P0 + scaffold最终形态 |

当前 active dependencies 还包括 `eval-gate-trace-loop` 与 `observability-provider-adapters`，本 change 只消费其 `EvalRunner`/`ScoreSink`/`TelemetryFacade` 公共 seam，不自动 archive 或改写其边界。若用户未来明确授权 archive，先处理被依赖的 retrieval/eval/observability changes，再按 `service-app-template-surface` -> `p0-example-agent-flows` -> `agent-scaffold-cli` 的依赖顺序收口；当前全部只停在 ready-to-archive。

## Testing Seams

- `AgentRegistry.load_from_directory` 与 `agent-harness agents list`：四个 descriptor/config。
- `agent-harness run <agent_id>`：断言 executor 真实被调用、typed output 写入 run、waiting/failed/terminal event 与 trace evidence。
- approval continuation：审批前 handler 0 次、approve 后 1 次且返回真实结果、重复/并发 resolve 不增加次数、deny 为 0、过期 raw lease 可由真实 API 重试换发 fencing id并继续、活跃 lease与已有 claim 不被抢占、旧 owner 被 fence、executing-without-result 进入 needs_review；approve/deny 的 resumed/terminal/resolution sink 写前失败与写后确认丢失均由真实 API 重试补偿，public 仍返回既有 409且 evidence 恰好一份。
- 四个 agent typed `run`：正常、空/未知、越界、长输出、require approval、deny。
- Example eval CLI/runner：approved-only、case 执行、score/trace、本地 JSONL 和 secret redaction。
- Static boundary scan：vendor SDK、ORM session、shell 权限和核心包依赖方向。

## Risks / Trade-offs

- [风险] executor resolver 扩大动态 import 攻击面。→ 只接受 agent package 内 module/callable reference，先完成 registry 整体校验，再 import；public descriptor 不暴露入口。
- [风险] 关键词分类被误当生产 classifier。→ README 和模块 docstring 明确它是 fake/local deterministic seam，真实 provider 通过 `ModelProvider` 替换。
- [风险] RAG 复述恶意 chunk。→ 强制 `RetrievalProvider -> ContextFragment -> ContextAssembler -> ModelProvider`，持久化 trust/truncation trace 并加入注入回归测试。
- [风险] shell 测试真的执行危险命令。→ require-approval/deny case 在 `ToolRegistry` policy gate 截停；允许 case 只用独立临时 workspace 的只读命令。
- [风险] 外部命令在执行后、结果持久化前崩溃，无法证明副作用是否发生。→ 先写 unique execution claim；未完成 claim 一律 needs_review，不自动重放。示例和测试使用可计数的受控 handler证明正常路径恰好一次，不把跨崩溃语义宣传为 exactly-once。
- [风险] 长输出测试污染用户目录。→ 每个测试使用临时 workspace 和 artifact root，结束后由 pytest 清理。

## Migration Plan

先完成、独立 review 并提交 `service-app-template-surface`，再应用本 change；scaffold 必须等本 change executor contract 稳定并独立 review/提交后实现。`0008_agent_execution_approval_claims` 为 approval 增加 private lease/state 字段，并为 tool invocation 增加 nullable `approval_id`、arguments hash、execution state 和唯一约束；SQLite/PostgreSQL contract 与 service smoke 都必须验证，public approval status schema 不新增枚举值。保留现有 basic smoke agent 作为非 P0 回归 fixture，但同步为它以及所有测试 config 增加显式 executor；不存在 legacy fake fallback。

生产回滚采用 **forward-only schema**：保留 `0008` 的 nullable 列、索引和唯一约束，只回滚 application code。回滚前必须停止新的 approval resolve，处理所有 `claimed` / `recovery_pending` / `denied_pending` / `executing` / `needs_review` 私有状态，确认不存在无确定性 result 的 execution claim或待补齐 evidence；completed/failed claim 与 resolution evidence 保留在数据库中，不删除、不重放。兼容性 contract test 必须在已升级到 `0008` 的 SQLite/PostgreSQL schema 上运行上一版本 repository/UoW 的基础读写，证明额外 nullable 列和 nullable unique `approval_id` 不破坏旧代码。

Alembic `downgrade()` 只用于 disposable/空数据环境：执行前查询并断言所有 approval private resolution 字段均为空，且不存在任何非空 `tool_invocations.approval_id`；任一 completed、failed、executing 或 needs_review claim 存在都必须拒绝 downgrade。满足空数据前置条件后，按 tool invocation index → unique constraint → execution columns → approval indexes → resolution columns 的逆序删除。

应用层回滚必须先禁用新的 scaffold 写入，并扫描所有受管 `agents_dir` 中的 config，不只检查四个内置示例。只要任一手工或 scaffold 生成 agent 仍声明新 `AgentExecutor` reference，回滚 preflight 就必须列出受影响 `agent_id` 并拒绝移除 resolver/runtime seam；操作者只能选择保留 compatibility seam，或把 agent 显式迁移/带审计地隔离到 registry 扫描根外，系统不得自动删除、改写用户 agent，也不得恢复固定 fake fallback。只有 inventory 清空或全部迁移验证通过后，才能移除四示例/executor refs并回退 runtime/registry seam。组合 contract test 必须先生成并成功运行 agent，再证明未迁移时 rollback fail-closed且 agent 仍可运行，显式迁移/隔离后才允许继续。任何未完成 claim 仍需人工处置，不能自动重放。实现完成后 change 停在 ready-to-archive，最终组合验收由最后的 scaffold change 执行。

## Open Questions

- 无阻塞问题；P0 只要求 local/fake 确定性执行，真实 provider 内容质量和 harness 优化留到 Phase 12.5 以后。
