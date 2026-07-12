## 1. 精确漂移测试

- [ ] 1.1 在 `tests/contracts/test_runtime_checkpoint_runs_contracts.py` 建立 RUN-001 至 RUN-005 的 `(path, method)` 精确 response status 基准，集合比较必须同时报告缺失与多余 status。
- [ ] 1.2 为每个 operation 断言成功 response 的 DTO 引用，并固定 RUN-002 当前引用 `RunCreateResponse`、不引用 `RunDetailResponse`。
- [ ] 1.3 参数化检查所有已声明错误 response 均引用 `ApiErrorEnvelope`，并保留真实无效输入返回统一 422 envelope 的证据。

## 2. Run OpenAPI Metadata

- [ ] 2.1 移除 `templates/service-app/app/api/routes/runs.py` 的 router 级共享 response 全集，改为 RUN-001 至 RUN-005 各自的 operation-specific error response map。
- [ ] 2.2 保持 RUN-001 的 local `200`、service `202` 与 `RunCreateResponse` schema，核验 RUN-002、RUN-004、RUN-005 继续返回 `RunCreateResponse`，RUN-003 继续返回 `RunEventsResponse`。
- [ ] 2.3 通过真实 `create_app().openapi()` 证明五个 operation 的 status/schema 精确相等，并核验 `app/api/errors.py` 与 `app/main.py` 的既有 handler 没有产生未声明的错误语义。
- [ ] 2.4 在唯一 OpenAPI factory 以精确 `(path, method)` allowlist 移除 RUN-002/RUN-004 不适用的 FastAPI 自动 422；增加最小框架回归测试，证明其他 operation 的真实 422 及统一 envelope 不受影响。

## 3. 回归验证

- [ ] 3.1 运行 RUN/OpenAPI 定向 contract tests 与相关 auth/policy、split-runtime tests，保存通过结果。
- [ ] 3.2 运行 `make quality`、`make test`、`make smoke-local`、真实 PostgreSQL/Redis 的 `make smoke-service` 与 `git diff --check`，分别记录离线和 service evidence。
- [ ] 3.3 运行 `uv run openspec validate run-openapi-contract-accuracy --type change --strict`，确认工件和未勾选任务保持可解析。
