## Source Links

- Product-Spec.md：REQ-005～REQ-006、REQ-010～REQ-012、REQ-014、REQ-024、REQ-029 / AC-108～AC-111
- API-Contract.md：5.3～5.9 run/checkpoint/event、5.14～5.21 approval/tool、RUN-002～RUN-006、APR-002、MOD-006
- DEV-PLAN.md：Phase 20 / 20C Durable resume 与 evidence
- docs/plans/architecture-evolution-plan.md：7.6 Phase 20 退出门禁
- docs/plans/architecture-evolution-change-matrix.md：20C、5.4 Phase 19 → 20、5.5 Phase 20 → 21
- ADR：docs/adr/0001-p0-service-boundaries.zh-CN.md、docs/adr/0002-vendor-adapter-isolation.zh-CN.md

## Why

20B 可以在受控顺序中执行循环，但进程退出、提交确认丢失和跨 worker 恢复仍可能把“继续”误写成第二次模型或工具副作用。Phase 20C 必须冻结 loop/turn/tool-call 的耐久身份、状态转换、exact replay 和 unknown 围栏，才能让服务恢复具备可证明的 at-most-once 语义。

## What Changes

- 增加 `model-tool-loop-state-v1` 的封闭状态与 repository/UoW 合同，保存 request/catalog identity、冻结上界、next turn、累计 usage、model/tool/approval/context refs 和最终结果。
- 从受信 tenant/run/agent/request/trace/operation key 派生稳定 loop identity；turn、model usage call、tool call、event/outbox/checkpoint identity 按 ordinal 唯一生成并逐值交叉校验。
- 所有普通和 approved 工具调用在 handler 前建立 `tool_call_id` 唯一 execution claim；completed/failed exact replay 返回既有结果，executing/提交确认未知进入 needs-review，绝不自动重放 handler。
- 恢复按已耐久 state 继续：既有 model usage claim exact replay 不重调 provider，既有 tool result 不重调 handler，既有 Context Assembly 不重新读取当前 payload；identity conflict 在任何副作用前失败。
- 冻结跨 resume 的 turn/token/cost/tool-output/wall-clock 上界和 shared-parent budget；restart、approval 和 config reload 不重置 deadline、ordinal 或余额。
- 把 usage、tool claim、approval、Context Assembly、CanonicalEvent/outbox 与 run terminal 置于同一恢复/围栏规则；未知 provider/tool/result/event/commit 状态保持 reservation、capacity、lease 和 owner ledger 并阻止 terminal。
- 强制以 `0018_model_tool_loop_state` 增加 loop 协调表、不可回退的 schema marker 及 tool/context 的 nullable v1 identity、claim lease/fence 字段；在 SQLite 与真实 PostgreSQL 上验证全部崩溃窗口、回滚拒绝和旧 binary fail-closed 规则。

## Non-Goals

- 不引入自动人工处置、补偿性重放、通用 workflow/state-machine 框架、后台 scheduler 或多 worker 并行执行同一 loop。
- 不做 tool-call/structured streaming、并行工具调用、跨 provider structured fallback、真实 provider+真实工具默认 smoke 或 Phase 21 重构。
- 不 sync/archive 前置 change，不改写 20A/20B 已冻结的公开行为。

## Capabilities

### New Capabilities

- `durable-tool-loop-resume`：冻结模型工具循环的耐久状态、稳定身份、exact/conflict replay、unknown 和 terminal fencing。

### Modified Capabilities

- `runtime-checkpoint-runs`：checkpoint/resume 必须恢复同一 loop/turn/tool call，禁止原始 token 或调用方 key 重建身份。
- `tool-execution-boundaries`：所有模型驱动工具调用都使用持久化 at-most-once claim，而非仅 approved 调用。
- `model-usage-evidence`：多轮模型 usage 与 loop/turn identity、累计上界和 replay 状态交叉校验。
- `shared-parent-budget-ledger`：每轮 reservation/actual 共用 root owner，resume 不重置或重复扣减。
- `canonical-events-artifacts`：未决 tool/model/context/outbox evidence 阻止 terminal，恢复只补投 exact envelope。
- `auth-policy-hitl-approvals`：approval 恢复与 loop state/tool claim 的 lease/fencing 必须逐值一致。

## Impact

- 预计影响 `runtime/{continuation,_run_continuation,orchestrator}`、`storage/` loop/tool/usage/outbox repositories、强制新增的 `0018_model_tool_loop_state` migration、`approvals/`、`tools/` claim、`models/` usage/budget/replay、`events/` terminal publication、worker recovery 和对应 SQLite/PostgreSQL contracts。
- 20C 只能从 20B 已同步归档的 HEAD 串行实现；完成后才能冻结 Phase 21 characterization 基线。
