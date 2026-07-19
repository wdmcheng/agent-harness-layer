## ADDED Requirements

### Requirement: Policy review threshold 不得绕过 shared hard limit
单次model/embedding调用的`policy review threshold` SHALL是独立软阈值，只能产生可追踪allow、有限fallback、deny或`require_approval`；`max_tokens_per_run`与`max_cost_usd_per_run` MUST是不可由审批提高、重置或覆盖的parent execution tree共享硬上限。系统 MUST先在frozen config内完成context/route降级并取得trusted finite intent；无有限上界或intent静态不可能满足hard limit时直接hard reject。Hard-eligible intent才可进入软阈值策略；fallback MUST回到route/trusted-intent步骤并有限终止。Allow或approve后 MUST在任何外部副作用前以当前余额执行shared-ledger原子reservation；approval不预约或持有额度，等待期间余额变化必须在resume时重检。

#### Scenario: Approval 不能覆盖 hard limit
- **WHEN** operation超过软review threshold并获得approve，但等待期间其他direct/delegation claim使当前parent余额不足
- **THEN** continuation在provider/child/queue副作用前的原子reservation处hard reject，approval不提高或重置hard limit，外部副作用计数为零

#### Scenario: 无可信上界不进入审批
- **WHEN** 实际route无法为启用的token或cost hard limit提供trusted finite worst-case bound
- **THEN** 系统直接hard reject，不创建用于绕过该失败的approval，也不调用provider、创建child或投递queue；只允许封闭脱敏内部rejection evidence

#### Scenario: Soft fallback 重新进入有界顺序
- **WHEN** soft threshold策略选择fallback route
- **THEN** 系统在frozen route/price snapshot内重新计算actual route trusted intent并再次评估soft policy，循环必须由封闭fallback列表有限终止，最终仍须通过shared-ledger原子reservation
