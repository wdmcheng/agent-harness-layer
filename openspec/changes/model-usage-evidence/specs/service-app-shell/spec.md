## MODIFIED Requirements

### Requirement: Service profile 分离 API 提交与 worker 执行
service-app SHALL 在 service profile 分离 API/worker：RUN-001 先持久化 `status=created` 与私有 enqueue_pending，Redis 接受并记录 queued/message ref 后才发布 `run.queued`/返回成功；worker 消费同 message 执行。approve continuation 同样持久化可补投状态。deny 不排队、不调用 executor/tool，但 API 只原子提交 deny 仲裁与有序 outbox；公开 approval/run 必须保持 waiting，直到唯一 `approval.resolved` 与对应 failed/fallback terminal 依序持久化后才进入终态。local/CLI 继续 inline，并遵守相同的“resolution 先于 terminal”证据顺序。

#### Scenario: API 不在请求进程执行 agent
- **WHEN** service profile 调用 RUN-001 且 worker 暂停
- **THEN** API 返回同一 `status=created` run，run 不进入 terminal，executor 调用计数为零，message 留在 Redis 等待 worker

#### Scenario: Worker 启动后完成 API run
- **WHEN** 独立 worker 随后消费该 message
- **THEN** worker 执行 API 创建的同一 `run_id`，共享 PostgreSQL detail 与 event seam 最终返回 completed、failed 或 waiting 真实状态

#### Scenario: Local profile 无外部依赖回归
- **WHEN** 开发者使用 local profile 或 `agent-harness run`
- **THEN** run 继续通过 SQLite/local event seam inline 执行，不要求 Redis、DBOS system database 或 service worker；approval resolution 与 terminal 仍按相同顺序持久化

#### Scenario: Service approval API 不执行 approve continuation
- **WHEN** reviewer 在 service profile 批准 executor-produced waiting approval 且 worker 暂停
- **THEN** APR-002 完成 lease/policy/audit/enqueue 状态并返回 queued/in-progress 语义，executor/tool 调用计数保持零；worker 恢复后才执行原 continuation，并在 resolution/terminal evidence 均已持久化后公开 approved 与 run 终态

#### Scenario: Service deny 不进入 queue
- **WHEN** reviewer 在 service profile 拒绝同类 waiting approval
- **THEN** API 原子提交 deny 仲裁与有序 outbox，queue/DBOS operation 为零，worker 无需执行 continuation 且 handler 保持零；公开状态在 denied resolution 与 failed/fallback terminal 持久化前保持 waiting

### Requirement: Worker 只在确定性收口后确认 delivery
runtime worker MUST 在消费新消息前恢复同 tenant 的 run `enqueue_pending` operation；approve recovery 只允许 active `resolution_state=claimed`、`enqueue_pending` lease、尚无 tool claim 且已保存完整 reviewer/decision/规范化 request hash 的 operation，其他 approval state 封闭失败。approval pickup 必须先 CAS 为 `execution_owned` 并持久化 DBOS workflow owner/ref。pickup 到 API 中断窗口的 run message 先补齐 queued/message/`run.queued` evidence 再执行。run 到 waiting 后可确认 delivery；run 或 approval 进入公开终态前，worker MUST 确认该动作要求的 usage evidence、唯一 `approval.resolved` 与对应 terminal 已按顺序持久化。不确定异常或任一前置 evidence 未确认时不得 ack；确定性失败也必须先完成相同的证据顺序再 ack。恢复只补投稳定 ID 的 outbox，不得重放 provider、tool handler 或 continuation。

#### Scenario: 不确定失败保留 pending
- **WHEN** worker 在持久化执行结果、usage、approval resolution 或 terminal evidence 前遇到连接中断或被取消
- **THEN** delivery 未 ack，run/approval 不被伪造为公开终态，后续 worker 可 reclaim 同一 message，并只恢复未确认的执行步骤或稳定 outbox

#### Scenario: 确定性失败先落证据再 ack
- **WHEN** executor 返回受控失败或 approved tool 返回已持久化的确定性 failed result
- **THEN** worker 先确认适用的 usage、唯一 `approval.resolved` 与唯一 failed terminal 均已按序持久化，再 ack delivery；后续 reclaim 不再执行 provider、tool handler 或该 message
