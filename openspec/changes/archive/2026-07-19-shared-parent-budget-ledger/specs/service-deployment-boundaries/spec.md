## ADDED Requirements

### Requirement: Service smoke 证明共享预算跨进程一致性
Service profile SHALL 使用真实 PostgreSQL row lock/CAS 与 Redis worker delivery/reclaim 验证 shared parent ledger；SQLite/local evidence MUST 单独报告，不能替代真实 service 证据。

#### Scenario: PostgreSQL 与 Redis 混合竞争和恢复
- **WHEN** direct 与 delegation 对同一 parent 并发，并在 reservation、外部副作用和 settlement 各崩溃窗口触发 worker reclaim
- **THEN** service smoke 证明 token/cost 不超支、外部调用不重复、unknown 保守占用、child 不双计且最终 terminal 只在全部 claim 封闭后可见

#### Scenario: Service profile 隔离同 tenant 多 root
- **WHEN** 同一 tenant 的两个独立 root runs 与其中一个 root 的 delegation 跨 API/worker 进程并发执行
- **THEN** PostgreSQL 以各 root 自身非空 `budget_owner_run_id` 隔离两条 ledger，同时让同一 root 的 direct、delegation与child allocation命中同一 row lock/CAS；Redis reclaim不得改变owner或串用另一root余额

#### Scenario: Cost-disabled service recovery 不误封锁
- **WHEN** `max_cost_usd_per_run=null` 的普通model、embedding miss或delegated child在PostgreSQL/Redis流程返回可信token与合法`cost=null/cost_status=unavailable`并经历worker reclaim
- **THEN** service按token维度幂等settle、cost impact为0且不单独needs_review，terminal在其他claim封闭后可见；非法cost/status反例仍稳定拒绝

#### Scenario: Service fingerprint secret 复用 CFG-001
- **WHEN** Compose 通过只读 `AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE` 为 API 与 worker 注入同一 fingerprint key，并分别执行正常启动及 direct/file 冲突、symlink、超限、非 UTF-8 失败反例
- **THEN** 两个进程只经 typed settings 获得相同 key semantics，正常路径生成可重放 opaque fingerprint；失败路径在任何 run/provider/queue 副作用前结构化退出，Compose config、health、doctor、logs、PostgreSQL 与 artifacts 均不含原值
