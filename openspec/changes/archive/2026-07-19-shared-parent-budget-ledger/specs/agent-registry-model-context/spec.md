## ADDED Requirements

### Requirement: Agent budget 提供可信共享上界
Agent descriptor 的 `max_tokens_per_run` / `max_cost_usd_per_run` SHALL 表示 parent execution tree 共享硬上限。Token维度始终启用；cost维度仅在`max_cost_usd_per_run`非null时启用。Router/adapter MUST 为每个实际 model 或 embedding route 的每个已启用维度产生受信、有限的最坏 intent；cost关闭时不要求伪造cost上界。Fallback改变provider/model时 MUST在外部调用前按实际route重新校验并预约，调用方不得提供更小值绕过预算。

#### Scenario: Fallback 使用实际 route 上界
- **WHEN** router 从首选模型切换到 fallback provider/model
- **THEN** shared ledger 在调用 fallback 前使用该实际 route 各已启用维度的可信最坏上界，任一已启用维度无法证明上界时 fail closed；关闭的 cost 维度不因合法 unavailable 单独拒绝

### Requirement: Run budget snapshot 在创建时冻结
Root run SHALL在创建且任何业务副作用前冻结tree budget snapshot。该snapshot MUST区分owner envelope与agent sub-snapshots：owner envelope保存`max_tokens_per_run`、`max_cost_usd_per_run`、cost-enabled状态、registry/config/catalog versions和snapshot ID；root agent与当时显式允许的P0 delegation targets各自保存独立descriptor version、model policy、target budget ceiling、允许provider/model routes及price source refs/versions。Child MUST继承同一owner snapshot ID与shared hard limits，并按自身target `agent_id`使用root时刻冻结的对应sub-snapshot，不得继承source agent descriptor或读取reload后的target配置。Target ceiling只能进一步收紧owner已启用维度，不能提高shared hard limit或重新启用owner已关闭的cost维度。Reload MUST只影响新root run。Fallback MAY在当前agent对应的frozen route/price sub-snapshot内按实际route重算trusted reservation，但 MUST NOT修改该run hard limit或使用reload后配置/价格。

#### Scenario: Reload 不改变在途 run
- **WHEN** root run已冻结budget snapshot，随后registry/provider/budget/price配置reload
- **THEN** 该run及其child继续使用原hard limits、config version和price source/version，新root run才使用reload后snapshot

#### Scenario: Fallback 重算 reservation 但不改上限
- **WHEN** 在途run按frozen policy选择另一个允许的fallback route
- **THEN** router使用该route在frozen price source/version下重算trusted reservation，并继续受原frozen parent hard limit约束

#### Scenario: 跨 agent child 使用冻结的 target sub-snapshot
- **WHEN** source agent委派到descriptor/model policy/budget与source不同、但在root创建时显式允许的target agent
- **THEN** child继承同一owner snapshot ID与shared hard limits，同时使用snapshot内该target自己的descriptor/model-policy/route/price版本；source/target descriptor不同不构成冲突，target ceiling只能进一步收紧已启用owner维度

#### Scenario: Target reload 不改变既有 tree
- **WHEN** root创建后target descriptor、model policy、budget或price catalog reload，再创建或恢复该target child
- **THEN** child仍使用root tree snapshot中的target sub-snapshot；未在该snapshot中冻结的target或route在provider/child/queue副作用前拒绝，新root才使用reload后版本
