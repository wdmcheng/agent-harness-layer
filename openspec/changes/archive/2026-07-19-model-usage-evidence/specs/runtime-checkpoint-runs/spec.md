## MODIFIED Requirements

### Requirement: Service approve continuation 由 worker 执行，deny 在 API 原子收口
service profile 的 `APR-002 decision=approve` SHALL 在 API 进程完成认证/policy、原子取得 resolution lease 后，持久化 lease、operation id、首次 request id、`resolution_state=claimed` 与 enqueue 状态，再投递独立 `resume_approval` operation；worker MUST 从 approval/resolution/run execution context 重建匹配的 `ApprovalGrant`，并在启动该 lease 专属 DBOS workflow 前以 CAS 把 resolution state 从 `claimed` 迁移为 `execution_owned`、持久化 workflow owner/ref，再通过相同 provider-neutral runtime resume seam 恢复原 executor/tool continuation。`decision=deny` MUST 由 API/repository 原子仲裁且不得创建 resolution lease、queue operation 或 DBOS workflow，但公开 approval/run 终态不得先于唯一 `approval.resolved` 与对应 terminal 的有序 outbox 证据持久化；API 只提交 deny 仲裁与 outbox，不执行 executor/tool。approve continuation 的真实结果也 MUST 先生成稳定 ID 的唯一 `approval.resolved`，再生成对应 completed/failed terminal；只有两者均已由 outbox 确认持久化后，公开 approval/run 才可进入终态。恢复流程 MUST 重放同一 outbox 记录，不得重放 provider、tool handler 或 continuation。

#### Scenario: Approval resolve 排队后由 worker 恢复
- **WHEN** executor-produced approval 处于 waiting，reviewer 通过 APR-002 approve
- **THEN** API 返回 resolution queued/in-progress 语义并投递 approval refs，worker 验证 tenant/identity/agent/run/action/resource/arguments hash/lease 后恢复同一 continuation，handler 恰好一次；真实 result 持久化后先确认唯一 `approval.resolved`，再确认唯一 terminal，随后才公开 approved 与 run 终态

#### Scenario: Deny 原子仲裁且零 continuation message
- **WHEN** reviewer 在 service profile 对 waiting approval 提交 deny
- **THEN** API/repository 原子写入 deny 仲裁与有序 outbox，且不创建 resolution lease、operation/message/DBOS workflow，executor/tool handler 计数为零；公开状态保持 waiting，直到 denied resolution evidence 与 failed/fallback terminal 依序持久化后才公开 denied 与 run 终态

#### Scenario: Approve 与 deny 并发只有一个决策胜出
- **WHEN** approve 与 deny 并发提交同一 waiting approval
- **THEN** repository 条件仲裁只允许一个决策；deny 胜出则零 queue，approve 胜出则只有一个 lease/operation，失败方返回稳定 409；胜出方只产生一组有序 resolution/terminal outbox 与公开终态，不产生第二个 audit、handler 或 terminal

#### Scenario: Approval continuation 重启与旧 lease fail closed
- **WHEN** worker 在 approval resume 或有序 outbox 投递期间中断、message 被 reclaim，或旧 lease/message 重复到达
- **THEN** 新 worker 以当前 resolution lease 和同 DBOS owner 恢复；过期/不匹配 lease 不得调用 handler，已完成 claim 返回已持久化结果；未确认的 evidence 只按稳定 ID 重放 outbox，不产生第二个 provider/tool 调用、resolution 或 terminal
