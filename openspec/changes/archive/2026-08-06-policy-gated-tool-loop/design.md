## Context

20A 冻结 provider-neutral `ToolIntent` 和无副作用 Registry resolve。现有 `ToolRegistry.call()` 已覆盖 schema、Agent allowlist、PolicyEngine、output guard、artifact 和 audit，`call_approved()` 已覆盖 approval-bound at-most-once claim；`ContextAssembler` 能持久化 source/trust/truncation，但当前没有 coordinator 把这些 seam 按模型工具循环的固定顺序连接起来。

本 change 在单一 runtime owner 中增加循环编排。它允许正常完成与 approval wait/resume，但不承诺任意崩溃窗口的跨进程自动继续；无法证明的中断必须保持 waiting/needs-review，完整 exact replay 与 worker recovery 由 20C 交付。

## Goals / Non-Goals

**Goals:**
- 建立唯一绑定的 loop façade 与固定副作用顺序。
- 复用 Registry/Policy/HITL/approval claim，不建立第二套授权控制面。
- 将工具结果作为 untrusted `ContextFragment` 通过 ContextAssembler 回注。
- 在循环开始冻结并强制 turn/token/cost/output/time 上界。
- 以既有 CanonicalEvent 词汇记录可重放的 model/tool/context/approval evidence。

**Non-Goals:**
- 不交付通用 crash replay、worker recovery 或所有普通工具 at-most-once 持久化；20C 负责。
- 不支持并行工具调用、tool-call streaming、后台 scheduler 或真实 provider+真实工具 smoke。
- 不重构已有 approval API、ToolRegistry、ContextAssembler 或 runtime state 为 Phase 21 候选架构。

## Decisions

### D1. 新建窄 `BoundModelToolLoopService`，不把循环塞进 model adapter

Loop façade 绑定 tenant/run/agent/request/trace 和语义 operation key，公开 exact 签名为 `run(request: ModelRequest, *, operation_key: str, tool_selection: ToolCatalogSelection | None = None, limits: ModelToolLoopLimitOverrides | None = None)`，内部协调 `BoundModelInvocationService`、ToolRegistry、Policy/Approval、ContextAssembler、EventBus 与 shared budget。两个可选 DTO 均独立于 `ModelRequest`：20A 的选择只保序缩小 catalog，本 change 的限制只逐项缩小 Agent hard maxima。Adapter 只生成 turn result；工具调用与下一轮决策都留在核心 runtime。Tool-intent capability 只把 `final_text` 作为成功终态；`final_structured` 是必须结算已发生模型影响的协议违规，不能由 loop 返回。

备选是让 Pydantic AI Agent 注册工具并自动循环，代码更少，但会绕过 Registry/Policy/HITL、隐藏调用次数并把 SDK 状态变成恢复真相，拒绝。

### D2. 状态推进以显式 step 结果为边界

单进程循环按 `model_turn → resolved → waiting|executing → tool_result → context_assembled → next_turn|terminal` 推进；每步只接收前一步的 immutable DTO/ref。20B 不引入通用 transition framework，只用封闭 enum/函数和公共 contract 锁定顺序。任何非法跳步、重复 turn 或 mixed identity fail closed。

### D3. Approval 复用现有 record/grant/lease 与 `call_approved`

Policy `require_approval` 创建 `agent_executor_approval` 类型的 checkpoint，continuation exact 绑定 loop/turn/tool-call/catalog/arguments/schema/action/resource 与运行身份。`APR-002` 取得 active resolution lease 后由现有 runtime resume 进入 executor `resume()`；loop façade 重算原 intent，验证 grant/lease 和 hard bounds，再调用 `ToolRegistry.call_approved()`。普通 resume token 永远不构成工具授权。

### D4. 工具结果先 guard，再转换成 untrusted fragment

Registry 已返回脱敏/截断/可 artifact 化的 `ToolCallResult`。Loop 再通过专用纯转换器生成 `ContextFragment(kind=tool_result, trust_level=untrusted)`，保留 source/artifact/truncation/token/injection 摘要。ContextAssembler 输出的 `output_ref`、assembled text digest 与 trace 成为下一 model turn 的唯一工具结果输入；不得重新读取 handler 原始返回或 artifact 全文拼 prompt。

### D5. Agent `model_tool_loop` 是循环上限唯一真相源

任一有效 route 支持 `tool_intent` 时，Agent exact config 必须显式给出 `model_tool_loop.max_turns/max_total_tokens/max_total_cost_usd/max_tool_output_bytes/max_duration_seconds`，无 capability 时必须缺失。Registry 在导入 executor/client 前验证全部字段、固定范围以及 token/cost 不扩大根预算，并投影为只读 descriptor summary；deployment、环境与代码常量都不是备用真相源。

公开 exact `ModelToolLoopLimitOverrides` 含相同五个全部必填 nullable 字段；DTO 缺省或字段为 null 均表示继承 Agent maximum，非 null 只能逐项缩小。Loop 从受信启动时间加 effective duration 推导 absolute deadline，调用方不能自报时间。Freeze 包含 effective 五项 bounds、absolute deadline 与 catalog identity；approval/reload/resume 只复用，不重新解释。每个 model turn 仍先按 root shared budget reservation；tool output 在 context/model 副作用前按 byte/token 上限裁剪或终止。达到任一上限返回稳定 limit terminal，不再请求 model/tool。

### D6. Event producer 复用既有目录并严格区分拒绝与执行

无效 intent/Registry/policy deny 只记录 validation/policy audit，不生成 `tool.call.started`。只有 execution claim 成功且 handler 即将运行时发布 started；确定完成/失败各自发布唯一 final。Context Assembly started/completed 包含 loop/turn/tool-call correlation 与 refs/digest，不含原始 payload。Capacity/outbox 早于相应副作用预约。

## Affected Surfaces

- Registry/config：扩展 Agent exact loader、public descriptor 与 scaffold/example fixtures，增加 capability-gated `model_tool_loop` 五项 maxima；不修改 deployment schema，不增加默认值。
- Runtime：新增 `runtime/model_tool_loop.py` 及 exact `ModelToolLoopLimitOverrides`；`runtime/{executor,services,continuation,_run_continuation}.py` 只做绑定与 approval handoff。
- Model/Tool：消费 20A DTO；`tools/{registry,approved_execution,execution_support}.py` 防御性重验与 claim/event hook。
- Policy/Approval：`approvals/{service,_continuation}.py` 与 checkpoint exact continuation；不改变 HTTP DTO。
- Context：`context/{assembler,service}.py` 与新增 tool-result converter；storage context record shape如不足必须在契约重审后处理，本 change 不先加迁移。
- Events/Artifacts：既有 CanonicalEvent/outbox/capacity registry 与 artifact store。
- Tests：公开 bound loop、allow/deny/approval、result injection/secret/large output、各 hard bound、event order/capacity、text/structured/tool CLI兼容。

上述生产/测试/文档在同一 worktree 由单一 owner 串行修改。20C 不得与 20B 并行写 runtime/approval/storage/event。

## Testing Seams

- `build_execution_context()` 注入的 `BoundModelToolLoopService.run()`：`final_text`、单工具、多轮、`final_structured`协议违规、invalid、deny、waiting/resume、limit。
- Registry/config：`model_tool_loop` required-iff-tool-intent、exact字段、类型/范围、根预算交叉约束、无默认值、descriptor投影和 scaffold/example兼容。
- DTO缩权：tool selection 缺省/空/保序子集；limit overrides缺省/逐项null继承/合法缩小/扩大与非法类型零副作用拒绝；受信时钟推导deadline。
- `RunOrchestrator` + 真实 ApprovalService/ToolRegistry/ContextAssembler 的本地 SQLite contract；不以私有 helper 代替。
- handler、MCP、shell、file/network 计数：invalid/deny/waiting 为零，allow/matching approval 恰一次。
- `ContextAssemblyResult`/repository：untrusted/source/artifact/truncation/injection 与下一 turn 输入 digest逐值一致。
- EventSink：started/final/context/approval 顺序、stable id、capacity exhaustion零业务副作用、terminal prerequisite。
- 默认 fake model + fake tool integration/eval 与 service composition smoke；不读取真实凭据。

## Risks / Trade-offs

- [20B 与 20C 边界模糊] → 20B 只承诺顺序和现有 approval resume；进程/commit outcome 不确定时停在 needs-review，不做自动跨进程继续。
- [Result guard 已在 Registry，loop 再 guard 可能重复] → Registry 负责 secret/artifact/output shape，loop converter 负责 context trust/injection metadata；职责和测试分开。
- [多轮预算被逐轮检查但总量仍可超] → loop freeze 维护累计 hard cap，并让每轮 shared-budget reservation 与剩余 loop cap 取最小。
- [Event started 与 handler 间崩溃] → 20B 不自动 retry；20C 通过 durable claim/unknown fence完善。

## Migration Plan

1. 从公开 bound loop 与 Registry config 添加 red contracts，先证明 `model_tool_loop` 无默认值、required-iff-capability、选择/上限 DTO 只能缩权、`final_structured`不能结束loop、deny/waiting/invalid 零副作用、result 必经 ContextAssembler、上限不能重置。
2. 接入 loop façade 与 fake model/tool；再接 Policy/HITL approval continuation 和 event/capacity。
3. 更新示例/eval/双语维护文档并重跑工具、approval、context、Phase18/19回归。
4. 本 change 默认不迁移数据库；回滚删除 loop capability和新增 checkpoint kind，既有 tool/model/approval行为保持可用。

## Open Questions

- 无阻断性问题。若现有 approval checkpoint 无法原子保存完整 loop binding，必须在本 change 中先修订 design/spec 并重审；不能以普通 resume token 或当前 Agent config 现场重建授权。
