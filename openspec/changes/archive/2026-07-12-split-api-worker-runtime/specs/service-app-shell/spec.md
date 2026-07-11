## ADDED Requirements

### Requirement: Service profile 分离 API 提交与 worker 执行
service-app SHALL在 service profile分离 API/worker：RUN-001先持久化 `status=created`与私有 enqueue_pending，Redis接受并记录 queued/message ref后才发布 `run.queued`/返回成功；worker消费同 message执行。approve continuation同样持久化可补投状态；deny不排队。local/CLI继续 inline。

#### Scenario: API 不在请求进程执行 agent
- **WHEN** service profile 调用 RUN-001 且 worker 暂停
- **THEN** API 返回同一 `status=created` run，run 不进入 terminal，executor调用计数为零，message留在 Redis等待 worker

#### Scenario: Worker 启动后完成 API run
- **WHEN** 独立 worker 随后消费该 message
- **THEN** worker 执行 API 创建的同一 `run_id`，共享 PostgreSQL detail 与 event seam 最终返回 completed、failed 或 waiting 真实状态

#### Scenario: Local profile 无外部依赖回归
- **WHEN** 开发者使用 local profile 或 `agent-harness run`
- **THEN** run 继续通过 SQLite/local event seam inline 执行，不要求 Redis、DBOS system database 或 service worker

#### Scenario: Service approval API 不执行 approve continuation
- **WHEN** reviewer在 service profile批准 executor-produced waiting approval且 worker暂停
- **THEN** APR-002完成 lease/policy/audit/enqueue状态并返回 queued/in-progress语义，executor/tool调用计数保持零；worker恢复后才执行原 continuation

#### Scenario: Service deny 不进入 queue
- **WHEN** reviewer在 service profile拒绝同类 waiting approval
- **THEN** API原子收口 denied，queue/DBOS operation为零，worker无需参与且 handler保持零

### Requirement: Worker 只在确定性收口后确认 delivery
runtime worker MUST在消费新消息前恢复同 tenant的 run `enqueue_pending` operation；approve recovery只允许 active `resolution_state=claimed`、`enqueue_pending` lease、尚无 tool claim且已保存完整 reviewer/decision/规范化 request hash的 operation，其他 approval state fail closed。approval pickup必须先 CAS为 `execution_owned`并持久化 DBOS workflow owner/ref。pickup到 API中断窗口的 run message先补齐 queued/message/`run.queued` evidence再执行。run到 terminal/waiting后才 ack；不确定异常不 ack，确定性失败先写 failed terminal再 ack。

#### Scenario: 不确定失败保留 pending
- **WHEN** worker 在持久化执行结果前遇到连接中断或被取消
- **THEN** delivery 未 ack，run 不被伪造为 completed，后续 worker 可 reclaim 同一 message

#### Scenario: 确定性失败先落证据再 ack
- **WHEN** executor 返回受控失败且 runtime 成功持久化 failed terminal event
- **THEN** worker ack delivery，后续 reclaim 不再执行该 message
