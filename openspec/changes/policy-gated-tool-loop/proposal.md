## Source Links

- Product-Spec.md：REQ-006、REQ-010～REQ-012、REQ-014、REQ-024、REQ-029 / AC-106～AC-108、AC-111
- API-Contract.md：5.9 `CanonicalEvent`、5.14～5.21 approval/tool DTO、APR-001～APR-002、TLS-001～TLS-003、MOD-006
- DEV-PLAN.md：Phase 20 / 20B Policy/HITL 工具循环
- docs/plans/architecture-evolution-plan.md：7.6 Phase 20 固定执行顺序
- docs/plans/architecture-evolution-change-matrix.md：20B 与 5.4 Phase 19 → 20
- ADR：docs/adr/0001-p0-service-boundaries.zh-CN.md、docs/adr/0002-vendor-adapter-isolation.zh-CN.md

## Why

Phase 20A 只让模型产生可验证 intent，仍没有授权执行和结果回注。Phase 20B 在不改写既有 ToolRegistry、PolicyEngine、HITL 和 ContextAssembler 安全语义的前提下，建立唯一受控的多轮执行顺序，使模型输出永远不能直接触发 handler。

## What Changes

- 增加绑定到 tenant/run/agent/request/trace 的 `BoundModelToolLoopService`，按固定顺序协调 model turn、Registry resolve、policy/audit、approval/checkpoint、tool claim/execution、output guard、ContextAssembler 和下一 model turn。
- Agent 任一 route 支持 `tool_intent` 时必须显式配置 exact `model_tool_loop` 五项 hard maxima，并由 Registry 投影为只读 descriptor summary；公开 `ModelToolLoopLimitOverrides` 只能逐项缩小，absolute deadline 只从受信启动时间推导。
- Bound loop 复用20A独立 `ToolCatalogSelection`，不扩展 `ModelRequest`；tool-intent protocol 只有 `final_text` 能成功结束循环，`final_structured` 保持独立 structured-output 能力。
- `allow` 才能进入工具 execution claim；`deny` 保持零工具副作用；`require_approval` 只创建逐值绑定原 loop/turn/tool call/arguments/schema/action/resource 的 waiting checkpoint。
- 批准恢复复用 active grant/lease 和 `ToolRegistry.call_approved` 的 at-most-once claim，并重新校验当前 hard bounds；伪造、过期、跨 run 或 identity 漂移在 handler 前失败。
- 把 `ToolCallResult` 转成固定 `untrusted` 的 `ContextFragment`，统一脱敏、截断、artifact、token estimate、来源和 injection summary；下一轮只消费 Context Assembly 的冻结 output ref/digest。
- 冻结 turn、token、cost、tool output bytes 与 wall-clock 上限；request 只能缩小，审批和 config reload 不能提高或重置。
- 使用既有 CanonicalEvent 词汇生产 model usage、tool call、context assembly、approval evidence，并在外部副作用前预约容量。

## Non-Goals

- 不提供跨进程 crash recovery、普通工具全路径 exact replay 或 unknown 自动处置；这些属于 20C。20B 遇到无法证明的中断只允许 fail closed/waiting/needs-review，不自动续跑。
- 不做 provider/tool streaming、并行工具调用、后台调度、跨 provider structured fallback 或真实外部调用。
- 不新增 HTTP route，不替换现有 approval API/CLI，不进行 Phase 21 typed-service 或 state-kernel 重构。

## Capabilities

### New Capabilities

- `policy-gated-tool-loop`：冻结受策略控制的模型工具循环、HITL 等待/恢复、结果回注与全局上界。

### Modified Capabilities

- `tool-execution-boundaries`：模型驱动调用必须复用 Registry resolve/call/call_approved、output guard、artifact、audit 和 execution claim。
- `auth-policy-hitl-approvals`：approval record/grant/checkpoint 增加 loop/turn/tool-call/catalog 绑定且不得扩权。
- `agent-registry-model-context`：绑定循环入口只能使用当前 Agent catalog，工具结果只通过 ContextAssembler 进入下一轮。
- `canonical-events-artifacts`：固定 tool/context/approval/model evidence 顺序、稳定 event id 和容量预约规则。

## Impact

- 预计影响 `runtime/` loop orchestration、`tools/` resolve/execution、`policy/`、`approvals/`、`context/`、`events/`、`models/` bound invocation、`storage/` 既有 claim/checkpoint seam 和 service-app 示例/合同测试。
- 20B 必须在 20A 同步归档的公开 DTO 上串行实现；与 20C 共享 runtime/approval/storage/event owner，不允许并行写入。
