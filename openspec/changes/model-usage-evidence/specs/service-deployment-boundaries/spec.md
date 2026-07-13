## MODIFIED Requirements

### Requirement: Service smoke 证明真实 HTTP 到 worker 链路
`make smoke-service` SHALL 通过真实 HTTP RUN-001 创建 run，并从共享 detail/events seam 观察独立 worker 执行同一 `run_id`。worker pickup 前，smoke MUST 从隔离 Redis stream entry 逐值核对 `request_id`、effective `idempotency_key`、`tenant_id`、`run_id` 与 API/PostgreSQL 证据一致。smoke MUST 输出 migration、Redis、API/auth、worker、fenced receipt、DBOS workflow、PostgreSQL event/checkpoint、usage、approval resolution 与终态关联证据；local、in-memory、日志推断或 mock 结果不得替代这些证据。approval approve/deny 场景 MUST 注入 outbox sink 写前失败、写后 ack 前失败与进程重启，证明公开状态只在唯一 `approval.resolved` 和对应 terminal 依序持久化后收口，恢复不重放 provider、tool handler 或 continuation。

#### Scenario: 暂停 worker 后提交再恢复
- **WHEN** smoke 暂停或尚未启动 worker、调用 API 创建 run，再启动 worker
- **THEN** API 先返回 `status=created` run 与 `run.queued` evidence，随后同一 run 被 worker 执行并产出可通过 HTTP 读取的 PostgreSQL event stream 与唯一终态

#### Scenario: Worker crash 后真实 reclaim 无重复 run
- **WHEN** smoke 让 worker A pickup 但在应用结果持久化与 ack 前确定性退出，等待 idle lease 后启动 worker B 通过 `XAUTOCLAIM` reclaim，再以同一 idempotency key 重复 RUN-001
- **THEN** Redis 显示同一 stream entry ownership/delivery count 变化，旧 receipt ack 被拒绝，B 复用同一 DBOS workflow/application run 并完成；所有 HTTP/DBOS/event 证据指向同一 `run_id`，数据库只有一条逻辑 run 且 terminal 唯一

#### Scenario: Queue 四字段逐值保持
- **WHEN** smoke 比较 RUN-001 请求/响应、PostgreSQL execution context、Redis entry、worker receipt 和最终 CanonicalEvent
- **THEN** `request_id`、effective `idempotency_key`、`tenant_id`、`run_id` 在所有适用边界逐值一致，重试 attempt id 不改写首次 queue correlation

#### Scenario: DBOS owner 建立后 hard crash 再恢复
- **WHEN** singleton worker A 以稳定 executor id 持久化 initial workflow/application owner/ref 并进入 PENDING/durable 状态，但在结果/terminal/ack 前 hard-exit
- **THEN** smoke 确认 A 完全退出后才启动 B；B 复用同 executor id、reclaim 同 entry 并让 DBOS 恢复同 workflow/durable step，逐值证明 executor/workflow/owner refs 与 crash 前一致且不是首次启动

#### Scenario: Shared application checkpoint 经审批恢复
- **WHEN** smoke 通过真实 HTTP 为 `examples.dev_assistant` 提交确定性需要审批的动作，worker 执行到 waiting 并写 PostgreSQL checkpoint/approval/event，随后 reviewer 通过 APR-002 approve，并分别在 resolution outbox 写前、写后 ack 前注入故障与重启
- **THEN** API 可读取同一 `run_id` waiting/checkpoint evidence；approve operation 由独立 worker 恢复原 continuation且 tool handler 只执行一次，恢复只补投稳定 outbox；唯一 `approval.resolved` 先于唯一 terminal 持久化，两者确认后才公开 approved 与 run 终态

#### Scenario: Service deny 不创建 continuation
- **WHEN** smoke 对另一条确定性 waiting run 提交 deny，并分别在 denied resolution outbox 写前、写后 ack 前注入故障与重启
- **THEN** Redis 中没有该 approval continuation operation，DBOS workflow 和 tool handler 计数为零；公开状态保持 waiting，恢复仅补投稳定 outbox，直到唯一 denied resolution 与 failed/fallback terminal 依序持久化后才收口
