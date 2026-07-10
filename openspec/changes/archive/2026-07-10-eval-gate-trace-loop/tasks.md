## 1. 契约与测试先行

- [x] 1.1 新增 Phase 11 OpenSpec / contract tests，覆盖 `eval-gate-trace-loop` spec、draft/approved 分离、failed/low-score detector、secret redaction、ScoreSink local-first 和 provider failure 降级。
- [x] 1.2 扩展 API-Contract EVL-* 条目和局部 OpenAPI drift tests，先让 draft、approve、eval run、score 查询契约断言失败。
- [x] 1.3 更新 storage migration / repository contract tests，先让 `eval_cases`、`eval_runs`、`eval_scores` 的 tenant/agent/run/trace/audit/score round trip 断言失败。

## 2. Eval public seam 与 storage

- [x] 2.1 新增 `agent_harness.evals` DTO / exports，公开 eval case、trace source、review decision、eval run、score record 和 provider status DTO。
- [x] 2.2 实现 `EvalCaseFactory` 和 failed/low-score detector，把 trace source 转成脱敏 draft case，自动路径只能写 draft。
- [x] 2.3 扩展 ORM models、repositories、UoW 和 Alembic migration，支持 eval case/run/score 持久化和 audit/source refs。

## 3. Review queue、runner 与 score sink

- [x] 3.1 实现 review queue / `ReviewDatasetAdapter`，支持 draft list、approve、approved dataset 写入和 audit log。
- [x] 3.2 实现 `EvalRunner`，只读取 approved cases，处理 empty dataset、completed/failed run 状态和 per-case score summary。
- [x] 3.3 实现 `ScoreSink` 和 `adapters.evals.local_jsonl`，local-first 写 score，再通过 `TelemetryFacade` / provider adapter contract fan-out，provider failure 返回 degraded status。

## 4. CLI、API 与模板入口

- [x] 4.1 接入 eval CLI：draft、approve、list、run、scores，并覆盖 fake/local profile 不需要真实 provider key。
- [x] 4.2 新增 template service eval API routes，覆盖 auth failure 无 side effect、approve 审核身份、score sink 降级响应。
- [x] 4.3 更新 `make eval` 和 template `eval-cases/drafts` / `approved` 目录，确保只运行 approved cases。

## 5. 收口验证

- [x] 5.1 跑 `openspec validate eval-gate-trace-loop --type change --strict`、Phase 11 targeted tests、`uv run pytest`、`make quality`、`uv run openspec validate --all --strict`、`uv run python scripts/smoke_local.py`、`make smoke-service`、`make eval`、`make build`、`make license-check`、`uv run pre-commit run --all-files`。
