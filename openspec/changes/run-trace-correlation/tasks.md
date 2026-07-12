## 1. Trace 合同与 Normalizer

- [ ] 1.1 先增加 red contracts，覆盖缺失 caller trace 生成、合法 trace 保留、非法/冲突 trace 零副作用失败，以及同一 idempotent run 在 trace 缺失或相同时安全重放、不同 trace 返回 `409 trace.idempotency_conflict`。
- [ ] 1.2 实现 provider-neutral run trace normalizer 和稳定 validation/conflict error，确保 API、CLI 与内部 run create 共用同一入口。
- [ ] 1.3 增加静态/import contracts，禁止 approval、EventBus、tool/model adapter 或业务 agent 为已有 run 自行生成第二 trace。

## 2. 数据迁移与 Repository 门禁

- [ ] 2.1 增加 Alembic migration 和 SQLite/PostgreSQL red contracts，以 run id 为稳定输入确定性 backfill execution context、approval、run-scoped event/audit/trace refs；同一 run 的冲突非空 trace 与孤立记录必须输出脱敏标识并整批 fail closed，禁止覆盖或部分提交。
- [ ] 2.2 收紧 ApprovalRecord/ApprovalCreate 与 repository 写入 trace 非空合同；已有非空值不得覆盖，重复 migration/backfill 结果必须幂等。
- [ ] 2.3 为通用 event/audit 表增加 run-scoped repository 门禁，证明非 run telemetry 可保持独立语义，但带 run_id 的新记录不能写入空或不一致 trace。

## 3. Runtime、Worker 与恢复传播

- [ ] 3.1 在 run create/submit 之前绑定 canonical trace 并写入私有 execution context，覆盖 local inline 与 service enqueue 两条路径。
- [ ] 3.2 让 queue/worker recovery、checkpoint/resume、cancel/fail/terminal evidence 读取 persisted trace；增加进程重建和新 request_id 下 trace 不变的合同测试。
- [ ] 3.3 验证同一 idempotency key 在 caller trace 缺失或相同时重放首次 run/trace；caller 后续提供不同 trace 时返回 `409 trace.idempotency_conflict`，且不改写 context、不重复事件或 queue message。

## 4. Approval、Event 与公开入口

- [ ] 4.1 让 approval required/resolve、audit 和 continuation 从 run context 继承 trace；删除或封闭允许调用方覆盖 ApprovalRecord trace 的路径。
- [ ] 4.2 让所有 run-scoped CanonicalEvent 在 local JSONL 与 PostgreSQL sink 前校验 canonical trace，并覆盖错误 trace 零 fan-out 失败。
- [ ] 4.3 更新 RUN-001 可选 `X-Trace-Id` OpenAPI/API Contract 与 CLI seam，覆盖缺失生成、非法 422、冲突 409、service 202 和 local 200。

## 5. 联合验证与收口

- [ ] 5.1 运行 runtime/approval/event/API/CLI 定向 unit、contract、integration tests，并分别验证 local restart 与真实 PostgreSQL/Redis worker recovery 的 trace 一致性。
- [ ] 5.2 运行 `run-trace-correlation` 与相关 active change 的联合 OpenSpec review，证明 `model-usage-evidence` 只消费 canonical trace，delegation/SSE 不提前实现。
- [ ] 5.3 运行 `make quality`、`make test`、`make eval`、`make smoke-local`、`make smoke-service`、`make build`、`make license-check`、pre-commit、`git diff --check` 和 strict OpenSpec validation；完成后只停在 `ready-to-archive`。
