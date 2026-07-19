## ADDED Requirements

### Requirement: Shared-budget recovery 按外部副作用阶段 fencing
Runtime 与 worker SHALL 区分三个恢复阶段：reservation 已提交且durable `side_effect_state=not_started`；`side_effect_state=started`但没有与shared settlement同UoW提交的可信result；可信result与全部shared settlement已原子提交但最终event尚未发布。恢复 MUST复用稳定claim，不得重复reservation、provider call、child run或queue operation；第一阶段才可继续原operation或在证明零副作用后释放，第二阶段进入needs_review且不得重放外部调用，第三阶段只从既有outbox补投event。新writer MUST NOT产生“result已持久化、ledger未结算”或cache claim/evidence单边提交；这种pre-0016 legacy半状态只能由`0016`migration预检/backfill处理。

#### Scenario: Worker reclaim 不重复预算或外部执行
- **WHEN** service worker 在上述任一阶段 crash 后 reclaim 同一 operation
- **THEN** worker 按durable phase恢复相应阶段；前两阶段没有可补投result，第三阶段只补投event，shared ledger与外部执行计数均保持幂等，SQLite/local与PostgreSQL/Redis语义一致
