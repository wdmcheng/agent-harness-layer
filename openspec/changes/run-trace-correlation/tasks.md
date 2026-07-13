## 1. Trace 合同与 Normalizer

- [ ] 1.1 先增加 API/CLI/internal red contracts，覆盖缺失 caller trace 生成、合法 trace 保留、非法/冲突 trace 零副作用失败，以及同一 idempotent run 在 trace 缺失或相同时安全重放、不同 trace 返回 `409 trace.idempotency_conflict`；CLI 明确使用 `--trace-id`，对格式/全局/idempotency 冲突分别断言稳定 stderr code、非零 exit、空业务 stdout 与逐表/queue/provider side-effect count。
- [ ] 1.2 实现 provider-neutral run trace normalizer 和稳定 validation/conflict error，确保 HTTP `X-Trace-Id`、CLI `--trace-id` 与内部 run create 共用同一入口且不 trim/大小写折叠。
- [ ] 1.3 增加静态/import contracts，禁止 approval、EventBus、tool/model adapter 或业务 agent 为已有 run 自行生成第二 trace。

## 2. 数据迁移与 Repository 门禁

- [ ] 2.1 在 `0012a_embedding_cache_tenant_scope` 后增加 `0013` 的 `run_trace_bindings(trace_id PK, tenant_id NOT NULL, root_run_id UNIQUE)` 与非唯一 `agent_runs.trace_id` 投影 Alembic migration 和 SQLite/PostgreSQL red contracts；以复合外键或等价事务安全数据库门禁保证 binding root/tenant、parent/child tenant 和 root/child trace projection/binding tenant 一致，repository claim/conflict 按已认证 tenant 限定但 trace 全局唯一。Backfill 先完整预检每个 root lineage 的非空候选：单一合法值优先并只填空、全空按固定 namespace/root id 生成 UUIDv5、单一非法值/多个不同值/跨 lineage 碰撞均在 mutation 前拒绝。随后确定性填充 execution context、checkpoint state、approval、run-scoped event/envelope、audit、tool invocation、eval case/run/score 与独立 `trace_refs.trace_id`；`eval_runs.run_id` 非空时直接投影对应 trace，无 run_id 的多来源聚合 eval run 保持 null 且不得任选 case；不得覆盖 provider `external_trace_id`。合法 non-run evidence 保持 nullable，孤立 parent/run-scoped record、跨租户 parent edge 与 lineage 环也必须整批 fail closed；测试必须覆盖 SQLite/PostgreSQL 的部分空、全空、非法单值、多值、全局碰撞、root tenant mismatch、child tenant mismatch 和绕过 repository 的数据库写入。
- [ ] 2.2 收紧 ApprovalRecord/ApprovalCreate 与 repository 写入 trace 非空合同；已有非空值不得覆盖，重复 migration/backfill 结果必须幂等。
- [ ] 2.3 为通用 event/audit 表增加显式 `record_scope=run|non_run` 或等价 typed discriminator 与 repository 门禁，证明 non-run telemetry 可保持独立语义，run scope 必须带可解析且一致的 run/trace；新 ordinary telemetry 不得再只用合成 envelope run_id 表达归属。
- [ ] 2.4 增加 local sink manifest 与单一 `agent-harness migrate-local-state` 离线命令及 red contracts：新 sink 首写前登记 canonical path/kind/version/state-dir，legacy event JSONL 与 eval `scores.jsonl` 通过显式重复 path 参数进入 inventory；命令要求 state-dir，并强制 typed settings 的 `--profile <name> [--profiles-dir <dir>]` 完整 bundle 模式与 `--file-only` non-run 模式二选一，credential 只从环境或受信 `_FILE` 读取，完整 DSN 不进入 argv、进程列表、shell history、日志或错误。覆盖 lock、全量预检、逐文件 journal/原始备份/同目录临时文件、fsync、atomic rename、中断恢复；用 `payload.telemetry.context.run_id` 区分 legacy ordinary telemetry 的真实 scope，覆盖 envelope 合成 trace id/`telemetry` fixtures，证明 file-only 接受真实 non-run、拒绝真实 run-scoped。普通入口不得自动推进旧 schema；不得把 Alembic PASS 或 manifest 外未知文件冒充已迁移。
- [ ] 2.5 增加 `0013` downgrade contracts：空库无/非法/重复 opt-in 都拒绝，只有精确 `-x allow_empty_evidence_downgrade=true` 且 evidence 全空时可恢复 `0012a` trace-nullable 形状；存在任一 binding、run-scoped canonical trace 或 backfill 完成 evidence 时即使 opt-in 也拒绝，保留兼容读取且不删除 evidence；不得绕过 `0012a` 自身门禁。

## 3. Runtime、Worker 与恢复传播

- [ ] 3.1 在 run create/submit 之前绑定 canonical trace 并写入私有 execution context，覆盖 local inline 与 service enqueue 两条路径。
- [ ] 3.2 让 queue/worker recovery、checkpoint/resume、cancel/fail/terminal evidence 读取 persisted trace；增加进程重建和新 request_id 下 trace 不变的合同测试。
- [ ] 3.3 验证同一 idempotency key 在 caller trace 缺失或相同时重放首次 run/trace；caller 后续提供不同 trace 时返回 `409 trace.idempotency_conflict`，且不改写 context、不重复事件或 queue message。

## 4. Approval、Event 与公开入口

- [ ] 4.1 让 approval required/resolve、audit 和 continuation 从 run context 继承 trace；删除或封闭允许调用方覆盖 ApprovalRecord trace 的路径。
- [ ] 4.2 让所有 run-scoped CanonicalEvent 在 local JSONL 与 PostgreSQL sink 前校验 canonical trace，并覆盖错误 trace 零 fan-out 失败。
- [ ] 4.3 更新 RUN-001 可选 `X-Trace-Id`、CLI-RUN-001 可选 `--trace-id`、公开 `CanonicalEvent.trace_id` 必填、RUN-003/OpenAPI/API Contract，覆盖缺失生成、API 非法 422/冲突 409、CLI 稳定 stderr/exit、service 202、local 200 和事件 schema 漂移。

## 5. 联合验证与收口

- [ ] 5.1 运行 runtime/approval/event/API/CLI 定向 unit、contract、integration tests，并分别验证 local restart 与真实 PostgreSQL/Redis worker recovery 的 trace 一致性。
- [ ] 5.2 运行 `run-trace-correlation` 与相关 active change 的联合 OpenSpec review，证明 `model-usage-evidence` 只消费 canonical trace，delegation/SSE 不提前实现。
- [ ] 5.3 运行 `make quality`、`make test`、`make eval`、`make smoke-local`、`make smoke-service`、`make build`、`make license-check`、pre-commit、`git diff --check` 和 strict OpenSpec validation；完成后只停在 `ready-to-archive`。
