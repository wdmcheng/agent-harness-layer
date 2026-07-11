## 1. Acceptance、Policy 与持久化幂等

- [x] 1.1 先通过公共 service/repository 测试锁定 create key+body hash、缺失/空白 key、同 reviewer accept 重试、跨 reviewer/decision/version 冲突、accepted version 与已比较 candidate 匹配、accepted/rejected decision record、production binding、comparison 门禁、`eval.harness.accept` allow/deny/require_approval 和原子 audit，再实现独立 `evals/acceptance.py`。

## 2. EVL-004 HTTP 与 OpenAPI

- [x] 2.1 先通过真实 app route 测试锁定 create/read/comparison/accept 成功路径、401/403/404/409/422、cross-tenant、candidate/version mismatch、policy require_approval、provider degraded、并发同 key 无 orphan split、active replay 与不确定执行 `needs_review` 无重复 evaluator/provider，以及逐表/adapter side-effect counts，再扩展既有 eval router/composition。
- [x] 2.2 通过运行时 `/openapi.json` 局部漂移测试锁定四个 EVL-004 paths、required `Idempotency-Key` header、HTTPBearer、五个稳定 request/response `$ref` schemas/required fields（含 reason codes 的 `minItems: 1` 与封闭枚举）、create 201/200 与其余 200 成功码、响应头说明，以及 create/read/comparison/accept 各自的适用 `ApiErrorEnvelope` 集合。

## 3. 等价 CLI 与维护指南

- [x] 3.1 先通过 Typer 公共入口测试锁定 `eval experiment create/show/compare/accept` 的稳定 JSON 输出、local identity、错误码、非零退出和无 secret，再实现 CLI composition。
- [x] 3.2 更新 `docs/eval-observability-loop.md` 的 tag curation、split、harness manifest、comparison、acceptance、provider degraded 操作指南，明确手写 case、生产 trace、外部数据集的准入标准及饱和/重复/失真 case 清理标准，并同步 DEV-PLAN Phase 12.5 真实状态与证据。
- [x] 3.3 运行 EVL-004 API/OpenAPI/CLI、SQLite/PostgreSQL、权限/租户、幂等、secret、degraded 与既有 eval gate 定向回归，记录可复核命令和结果。
