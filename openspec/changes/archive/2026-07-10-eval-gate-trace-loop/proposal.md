## Source Links

- Product-Spec.md: `FLOW-004: Trace 到 Eval 的闭环`、`REQ-016: Eval Gate 与 trace/eval 闭环`、`6.3 数据规则`、`11.5 评估与可观测`。
- DEV-PLAN.md: `Phase 11: Eval Gate 与 Trace 到 Eval 闭环`、数据实体表中的 `eval_cases` / `eval_runs` / `eval_scores`、Phase 10 后续入口说明。
- API-Contract.md: 第 2 节 Eval Gate / Observability 边界、第 4.7 节数据保护、保留 API 索引 `EVL-001` / `EVL-002` / `EVL-003`。
- Design-Brief.md 或设计稿：不适用，本 change 不涉及产品化前端 UI。
- CONTEXT.md / ADR: 未发现本轮必须读取的领域上下文或 ADR。

## Why

Phase 10 已把 observability provider adapter 和 provider 前脱敏边界立住，但 failed/low-score trace 还不能受控回流为 eval case。Phase 11 需要把 trace、人工审核、approved dataset、eval run、score sink 和 provider 写回串成一条可测试闭环，否则 eval 与 observability 仍只是并排能力。

## What Changes

- 新增 `agent_harness.evals` public seam，包含 `EvalCaseFactory`、failed/low-score detector、review queue / `ReviewDatasetAdapter`、`EvalRunner` 和 `ScoreSink`。
- 新增 draft / approved dataset 分离行为：自动 detector 只能写 draft，approved dataset 只能通过人工审核流程写入，并记录 audit。
- 新增 local JSONL score sink，并通过 Phase 10 `TelemetryFacade` / provider adapter contract 写回 Logfire、Phoenix、Langfuse 等 provider seam；provider 失败时本地 score evidence 不丢。
- 新增 eval CLI/API：draft、approve、list、run eval、查看 score，并把 `API-Contract.md` 的 `EVL-*` 从保留索引扩展成正式契约。
- 强化 eval 侧 secret / privacy redaction、artifact/ref 和 identity contract，确保 secret 不进入 trace、eval case、audit、local/jsonl、API body/error 或 provider payload。

## Non-Goals

- 不实现 Phase 12 的四个 P0 示例 agent 完整用户流；本 change 可复用已有 RAG eval fixture，但不把示例产品化。
- 不实现 Phase 13 API/worker 分进程、独立 eval worker 或独立 observability pipeline。
- 不实现 provider-native hosted dataset / experiment / annotation 的深集成；P0 只提供 provider-neutral score sink 和 adapter contract。
- 不允许自动写入 approved dataset，不提供默认开启的自动入库策略。
- 不执行 `openspec archive`，归档仍由用户显式确认。

## Capabilities

### New Capabilities

- `eval-gate-trace-loop`: Trace 到 draft eval case、人工审核、approved dataset、eval runner、score sink、本地与 provider 写回、CLI/API 契约。

### Modified Capabilities

- 无。

## Impact

- 代码：新增 `agent_harness.evals`、`agent_harness.adapters.evals`，扩展 storage models/repositories/UoW、migration、audit、observability score seam、template app API/CLI。
- API：将 `EVL-001`、`EVL-002`、`EVL-003` 扩展为 draft / approved / eval run 正式 endpoint 契约，并补局部 OpenAPI drift tests。
- 数据：补足 `eval_cases`、`eval_runs`、`eval_scores` schema；大 payload 通过 artifact/ref 保存。
- CLI / 模板：新增 `agent-harness eval ...` 或 template 等价入口，`make eval` 只读取 approved cases。
- 安全：draft 到 approved 必须执行 secret / privacy scan；写 approved dataset、读取 trace/eval evidence 和 provider score 写回必须保留 tenant、agent、run、trace 关联。
