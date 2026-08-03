## ADDED Requirements

### Requirement: Structured reservation覆盖有限repair最坏情况
Shared parent budget SHALL 在结构化provider副作用前以checked arithmetic计算`provider_request_limit=transport_attempt_limit * (1+repair_limit)`，并预约每次冻结`trusted_input_token_bound + output_token_cap`及对应cost bound的总和。该总reservation、schema identity、transport/repair policy MUST 进入immutable operation identity；direct与delegation allocation遵守相同公式。Input/output price values与price source ref/version分别 MUST 成对完整；cost启用必须绑定完整source，cost关闭允许price values/bounds为null且完整catalog/source identity继续进入route/evidence。任一半pair、超出owner/Agent hard limit或算术溢出时 MUST 在公开structured seam以`budget.reservation_rejected`零claim/provider副作用拒绝，内部`ModelRouteError`不得逸出。

Claim建立后的确定`failed`终态只有在全部attempt副作用、usage、cost与prepared cleanup均已证明完整时才按actual结算并释放未使用reservation；核心为每个repair/transport ordinal建立fresh prepared call并显式推进循环，只有`StructuredProviderPrepareError(retryable=true)`或send前取消能由核心构造完整proof并以零actual收口。任何到达send的attempt都只对应一个provider-local request并由核心映射全局ordinal；structured收到HTTP response、call error、cancel/deadline或未知异常后都停止transport retry，classifier不适用。Send后usage不完整或cleanup失败一律使direct/allocation/owner ledger一致保持`needs_review`并保留reservation。任一request结果、usage、取消、cleanup、commit ack或request/repair基数不确定都不得借`failed`或伪造计数提前退款。

#### Scenario: Direct与allocation使用相同structured公式
- **WHEN** 相同route/schema/repair policy分别从root direct和delegated child调用
- **THEN** 两者 SHALL 以同一单次bound乘以request limit，并各自受owner/allocation上限约束，不放大额度

#### Scenario: 预算不足在发送前拒绝
- **WHEN** structured总reservation超过token或cost hard limit
- **THEN** ledger SHALL 不建立started副作用、不调用provider并返回封闭`budget.reservation_rejected`，错误不公开余额或hard limit

#### Scenario: Cost关闭仍保留完整catalog身份
- **WHEN** owner关闭cost hard limit且冻结route的input/output price values与cost bounds为null，但catalog/source ref/version完整
- **THEN** structured planning SHALL 接受该route并把完整身份带入durable route/evidence；任一value或source半pair则在claim/provider前以`budget.reservation_rejected`关闭失败

### Requirement: Structured settlement与replay不丢失repair影响
成功、invalid或repair exhausted只在所有started attempts启用维度actual完整时 SHALL 以全部attempt实际token/cost原子替换reservation。任一started/unknown attempt维度不完整时claim、allocation与owner ledger MUST 保持needs-review及不低于可信影响。相同stable operation exact replay只复用首次identity/result；schema、repair policy或语义请求变化必须在provider前冲突，不能创建第二claim。

#### Scenario: 全部actual完整后原子结算
- **WHEN** 两次structured request的token/cost均完整且最终valid或exhausted
- **THEN** shared budget SHALL 以两次actual总和一次性结算并保存exact durable result

#### Scenario: Unknown不释放reservation
- **WHEN** 第二次request已started但结果或usage未知
- **THEN** claim/ledger SHALL 进入needs-review并保留未决reservation，恢复不得退款、repair或重调provider

#### Scenario: Mark commit ack未知对direct与allocation共同围栏
- **WHEN** durable mark commit ack未知但send仍可证明未调用
- **THEN** direct claim或delegation allocation及其owner/top claim/ledger SHALL 一致进入needs-review，actual token/cost保持null并保留完整reservation；零provider request proof不得授权退款

#### Scenario: Schema冲突不创建第二claim
- **WHEN** 同一operation slot以不同schema identity或repair limit重放
- **THEN** repository SHALL 返回immutable identity conflict，现有claim/result不变且provider调用为零
