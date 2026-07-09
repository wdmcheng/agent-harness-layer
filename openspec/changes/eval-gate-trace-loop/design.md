## Context

Phase 4 已有 CanonicalEvent、local JSONL event sink、artifact store 和初版 `eval_cases` / `eval_runs` 表；Phase 7 已把危险动作、approval 和 audit 固定为受控流程；Phase 10 已新增 `TelemetryFacade`、provider 前 redaction 和 Logfire / Phoenix / Langfuse adapter contract。Phase 11 的缺口不是再造 observability，而是把 failed/low-score trace 变成受控 eval case，再把 eval score 通过本地和 provider seam 写回。

外部资料核验结论：
- Logfire Python 当前公开能力以 OTel span/log/metric 为主，可在 span 内聚合 custom metrics；P0 score 写回应通过 provider-neutral telemetry record / metric attribute 表达，不在核心包绑定 Logfire SDK。
- Phoenix Python 支持 datasets、experiments、evaluators，并可将 evaluation results 作为 span annotations 记录；P0 adapter contract 可把 score DTO 映射为 experiment result 或 span annotation。
- Langfuse Python SDK 支持 trace / observation score，包括 `span.score(...)` 和 `api.scores.create(...)`；P0 adapter contract 可把 score DTO 映射为 trace 或 observation score。

## Goals / Non-Goals

**Goals:**
- 建立 `agent_harness.evals` provider-neutral public seam：case、factory、review queue、runner、score sink。
- 让 failed/low-score trace 半自动生成 draft case，但 approved dataset 只能由人工审核写入。
- 让 `make eval` 和 eval CLI/API 只消费 approved cases，并输出可查询 eval run / score evidence。
- 让 score 先写 local JSONL，再通过 Phase 10 telemetry/provider seam 写回外部 provider，provider 失败不影响本地 evidence。
- 把 secret redaction、artifact/ref、tenant/agent/run/trace 关联纳入 eval 全链路。

**Non-Goals:**
- 不接入 provider-native hosted dataset 的完整生命周期管理。
- 不实现独立 eval worker、调度队列或分进程运行。
- 不实现 Phase 12 的完整示例 agent 用户流。
- 不允许自动 detector 默认写 approved dataset。

## Decisions

1. **Eval public seam 使用 DTO + repository / adapter，避免 provider SDK 泄漏。**
   `EvalCaseFactory`、`EvalRunner` 和 `ScoreSink` 只交换 Pydantic DTO、artifact refs 和 repository DTO。替代方案是直接在 runner 内调用 Logfire/Phoenix/Langfuse SDK；拒绝，因为会违反 Phase 10 provider import boundary，也让 fake/local profile 难以稳定测试。

2. **Draft 与 approved 分成两个 dataset adapter 路径。**
   draft queue 支持自动生成和人工 review；approved dataset 只允许 approve 动作写入。替代方案是单表单目录靠 status 区分；拒绝，因为文件型模板中 `eval-cases/drafts` 和 `eval-cases/approved` 是用户可见边界，必须避免自动 detector 误写 approved。

3. **ScoreSink local-first，provider 写回只影响 degraded status。**
   eval run 先写 local JSONL score，再 fan-out 到 provider adapter。替代方案是 provider 成功后才写本地；拒绝，因为 provider failure 会丢失 release gate 证据。

4. **API 契约先补正式 EVL 条目，再实现 FastAPI route。**
   `EVL-001` / `EVL-002` / `EVL-003` 从保留索引升级为正式契约，contract tests 对照 `/openapi.json` 或 route schema 做局部漂移检查。这样 CLI、API 和 template app 的入口语义一致。

5. **Storage schema 补齐 score，不依赖完整 provider dataset。**
   现有 `eval_cases` / `eval_runs` 表需要增加 agent/run/trace/source/audit 关联和 `eval_scores` 表。P0 只保存本地可验证 evidence 和 provider refs；provider-native dataset id / experiment id 作为 metadata/ref，而非主模型。

## Affected Surfaces

- `packages/agent-harness/src/agent_harness/evals/`: DTO、factory、review queue、runner、score sink。
- `packages/agent-harness/src/agent_harness/adapters/evals/local_jsonl.py`: local JSONL score sink。
- `packages/agent-harness/src/agent_harness/storage/models.py`、repositories、UoW、migration: eval case/run/score persistence。
- `packages/agent-harness/src/agent_harness/audit/` 与 policy seam: approved dataset 写入审计。
- `packages/agent-harness/src/agent_harness/observability/`: `TelemetryFacade` score record fan-out。
- `packages/agent-harness/src/agent_harness/cli.py` 或 template CLI: eval draft / approve / list / run / scores。
- `templates/service-app/app/api/routes/evals.py`、`app/main.py`: EVL API routes。
- `templates/service-app/eval-cases/drafts/`、`templates/service-app/eval-cases/approved/`、`Makefile`。
- `API-Contract.md` 与 tests/contracts 中 Phase 11 contract、OpenAPI drift、secret/provider failure tests。

## Testing Seams

- OpenSpec seam: proposal/spec/design/tasks 与 `eval-gate-trace-loop` delta spec。
- Module seam: `EvalCaseFactory.from_trace()` / detector、`ReviewDatasetAdapter.approve()`、`EvalRunner.run_approved()`、`ScoreSink.write_score()`。
- Persistence seam: SQLite migration 后 `eval_cases`、`eval_runs`、`eval_scores` 可 round trip，并保留 tenant/agent/run/trace。
- API seam: draft / approve / list / run / scores endpoints 的 auth、side effect、secret redaction 和 provider degraded semantics。
- CLI seam: `agent-harness eval draft`、`eval approve`、`eval list`、`eval run`、`eval scores`。
- Template seam: `make eval` 只读取 approved cases，draft case 不参与。
- Import boundary seam: eval core、template app、business agent 不直接 import Logfire/Phoenix/Langfuse。

## Risks / Trade-offs

- [Risk] provider score API 差异大。→ 缓解：P0 只定义 provider-neutral score DTO，adapter contract 映射 Logfire metric/span attribute、Phoenix annotation/experiment、Langfuse score。
- [Risk] 自动 detector 被误用为自动 approved。→ 缓解：repository、dataset adapter、API 和 tests 都把 automatic source 限定为 draft。
- [Risk] eval case 复制大 payload 或 secret。→ 缓解：factory 走统一 redaction 和 artifact/ref 策略，contract tests 用 secret fixture 和大 payload fixture。
- [Risk] eval API 引入未认证 side effect。→ 缓解：沿用 Phase 7 auth dependency，invalid auth contract tests 断言无 case/run/score/audit side effect。
- [Risk] schema migration 影响已存在空表。→ 缓解：新增 revision 用 `ALTER TABLE` / table creation，可在 SQLite/PostgreSQL service smoke 中验证。

## Migration Plan

新增 Alembic revision 扩展 eval schema：补齐 `eval_cases`、`eval_runs` 的 agent/run/trace/source/status metadata，并新增 `eval_scores`。已有 P0 开发数据只包含早期薄表时，通过 nullable / default / metadata 兼容；不删除用户数据。回滚时移除新增 score 表和新列，保留原薄表。

## Open Questions

- Phase 12 是否把 RAG assistant 和其他示例 agent 的 approved eval cases 迁入统一 dataset adapter；本 change 只提供运行和写分数的公共 seam。
