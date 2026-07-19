## ADDED Requirements

### Requirement: Runtime composition 统一注入 shared-budget seam
Local app、service API 与 worker composition SHALL 为 model、embedding、delegation 和 terminal guard 注入同一 shared-budget repository/UoW，并统一把 root 自身或 tenant-fenced delegation relation 解析为非空 `budget_owner_run_id`；P0 MUST NOT 新增公开 budget ledger HTTP route，也不得把内部 owner、余额、reservation、price secret 或 needs_review 细节加入公开 response。

#### Scenario: Local 与 service 使用同一合同
- **WHEN** 相同 parent budget 场景分别通过 local inline 与 service PostgreSQL/Redis 执行
- **THEN** 两条入口命中相同非空 owner 并得到逐值一致的 allow/reject、claim state 与公开错误语义，OpenAPI route 集合不增加 budget endpoint

#### Scenario: Direct budget reject 的公开 code 逐值一致
- **WHEN** local 或 service 的 direct model/embedding 因无可信有限上界、静态硬不合格、当前余额不足、snapshot无效或ledger needs-review而拒绝
- **THEN** 两条入口的module/runtime与usage rejection evidence都使用`budget.reservation_rejected`；内部reason可区分原因但不得进入公开response或泄露余额，delegation仍使用`delegation.budget_exceeded`

#### Scenario: Local 与 service 使用相同组合错误优先级
- **WHEN** local/SQLite 与 service/PostgreSQL 对相同 stable key、relation/snapshot、budget 与 event-capacity 组合执行新 claim 或 replay
- **THEN** 两条入口都按 exact replay/identity conflict、integrity、`event.sequence_state_invalid`、budget、`event.sequence_exhausted`、unique-race重读的顺序收敛；capacity-only逐值返回`event.sequence_exhausted`，budget+capacity返回对应budget code，数据库异常不进入公开消息
