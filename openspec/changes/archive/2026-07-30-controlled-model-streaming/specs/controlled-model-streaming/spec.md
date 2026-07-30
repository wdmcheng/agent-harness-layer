## ADDED Requirements

### Requirement: 供应商中立的文本流协议
系统 SHALL 通过独立于既有一次性 `ModelProvider.complete` 的流式能力协议公开文本增量与最终结果。公开协议只允许追加式文本增量和供应商中立的最终 `ModelResponse`；供应商 SDK 的事件类、游标、tool/reasoning/structured delta、重试与连接对象 MUST 留在 vendor adapter 内。router MUST 在任何容量预留或供应商副作用前拒绝不支持流式文本能力的 route 或 provider，不得把一次性结果伪装成流。

#### Scenario: 受支持 provider 输出纯文本增量
- **WHEN** route 声明 `text_stream` 能力且 provider 实现流式协议
- **THEN** invocation 按顺序接收非空文本增量并最终取得一个供应商中立 `ModelResponse`
- **AND** 调用方观察不到任何 Pydantic AI 或其他供应商事件类型

#### Scenario: provider 不支持流式能力
- **WHEN** route 或 provider 不支持 `text_stream`
- **THEN** 系统在容量预留、started 事件和供应商副作用前以稳定的不支持错误关闭失败
- **AND** 系统不调用 `complete` 来伪造流式输出

#### Scenario: SDK 混合事件被适配器隔离
- **WHEN** Pydantic AI 流包含文本、reasoning、tool、structured 与最终结果事件
- **THEN** 适配器仅将追加式文本部分转换为文本增量，并将最终结果转换为 `ModelResponse`
- **AND** 非文本事件不会成为公开 delta，也不会改变公开 chunk ordinal

### Requirement: 固定上限的增量分片
每次调用 MUST 最多发布 64 条 `model.output.delta`，每条 delta 的 UTF-8 文本 MUST 非空且不超过 4096 bytes。运行时可选择 1～4096 bytes 的目标分片大小，但不得改变 64 条硬上限。系统 MUST 合并或切分供应商片段以形成自己的稳定分片；不得把供应商分块边界当作公共契约。超过硬上限、单条事件 envelope 上限或安全缓冲上限时 MUST 关闭失败，且不得丢失、重排或静默截断文本。

#### Scenario: 供应商片段被确定性合并和切分
- **WHEN** 供应商以任意非空文本片段边界返回可接受的完整文本
- **THEN** 系统发布不超过 64 条、每条不超过 4096 UTF-8 bytes 的公共 delta
- **AND** 按 `chunk_ordinal` 拼接公共 delta 恰好得到安全处理后的最终文本

#### Scenario: 输出超过可证明边界
- **WHEN** 后续供应商文本会导致第 65 条 delta、超大 envelope 或安全候选缓冲溢出
- **THEN** 系统停止发布后续文本并请求关闭 provider 流
- **AND** 若无法证明远端副作用已停止，调用进入 unknown，不发布 completed、最终 usage 或运行终态

#### Scenario: 单个供应商片段超过 collector 总上限
- **WHEN** 单个 provider fragment 的 UTF-8 文本已经超过 `64*4096` bytes，或加入该 fragment 会使 adapter collector 越过同一总上限
- **THEN** `ModelStreamDelta`/adapter 在把该 fragment 加入任何观察列表或 runtime collector 前立即关闭失败
- **AND** 字节计数不得构造与任意大 fragment 等长的临时 bytes，runtime 不得再次编码已校验 fragment
- **AND** 系统不通过扩大内存缓冲、补预留或截断输出继续运行

### Requirement: 副作用前的双容量预留
流式 invocation MUST 在同一持久化事务中、供应商副作用前，完成既有模型用量 evidence 的 2 槽位预留，以及版本化 `model_stream` 操作的 65 槽位预留。`model_stream` 的 65 个槽位 MUST 对应 64 个稳定 delta 占位和 1 个 completed 占位；每个占位在副作用前具有稳定 `event_id`、`group_id`、`sequence_in_group` 和 `started` 状态。group id MUST 为 `model-stream:{usage_call_id}` 且长度为 77。任一预留、占位或提交失败时 MUST 不调用 provider。

#### Scenario: 两笔预留原子成功
- **WHEN** 流式调用通过策略、预算和 capability 检查
- **THEN** 同一事务提交 2 个 usage 槽位、65 个 stream 槽位及全部 stream outbox 占位
- **AND** provider 的第一次可能网络副作用发生在该事务成功提交之后

#### Scenario: started 发布后首次 provider 迭代前取消
- **WHEN** 双预留、stream 占位与内部 started 已提交，但调用在首次 SDK stream context 创建或 provider 迭代前被取消、deadline 耗尽且 close result 为 `not_started`
- **THEN** 已发布 started 与其 high-water 保持不变，65 个 stream 占位全部取消且只释放 65 outstanding
- **AND** 系统发布 `outcome=cancelled`、provider 未调用、token/cost 为 null、budget charge 为可信零的 stream usage final，消费剩余 usage 槽位
- **AND** 预算与本地 lease 释放后才允许 `run.cancelled`，不删除 evidence、不把 provider usage 记零

#### Scenario: 容量或存储不足
- **WHEN** usage 或 stream 任一容量预留、占位写入或事务提交失败
- **THEN** 两笔预留与占位均不留下部分提交
- **AND** provider 未被调用，started、delta、completed、usage 和运行终态均未被伪造

### Requirement: 稳定身份、可见性与完成校验
流式事件 MUST 使用调用前已确定、已包含 tenant 关联的 64 位小写 SHA-256 `usage_call_id` 作为定长关联根。delta 的稳定事件标识 MUST 为 `model-stream:{usage_call_id}:d:{chunk_ordinal}`，其中 `chunk_ordinal` 使用无前导零十进制从 1 连续递增到 64，最大 event id 长度为 82；completed 的标识 MUST 为 `model-stream:{usage_call_id}:c`，长度为 79。配套 usage started/final 的 `stream-usage-v1` 标识 MUST 分别为 `usage-stream:{usage_call_id}:s` 与 `usage-stream:{usage_call_id}:f`，长度均为 79。不得把原始 `tenant_id` 再拼进这些新 event id。delta 与 completed MUST 为 `public`、`terminal=false`。delta payload MUST 包含 `correlation.usage_call_id`、`attempt=1`、`chunk_ordinal`、`text`；completed payload MUST 包含同一关联、`attempt=1`、`chunk_count`、`text_utf8_bytes` 与小写 SHA-256 `text_sha256`。completed 摘要 MUST 与已发布 delta 拼接结果和最终 `ModelResponse.output_text` 完全一致。

#### Scenario: 成功完成的事件身份和摘要
- **WHEN** 流式调用成功完成并发布三个 delta
- **THEN** 三条 delta 使用连续 ordinal 1～3 和确定性 event id，completed 使用同一调用根身份
- **AND** completed 的 `chunk_count=3`、字节数与 SHA-256 同时匹配 delta 拼接文本和最终响应文本

#### Scenario: 最终结果与增量不一致
- **WHEN** provider 最终结果文本与安全处理后的 delta 拼接文本不一致
- **THEN** 系统不发布 completed、最终 usage 或运行终态
- **AND** 在不能证明供应商结果可安全重建时将调用标记为 unknown 并保留未决容量

### Requirement: 跨供应商分块的敏感内容安全
系统 MUST 在任何公共 delta 持久化前对连续文本执行有状态安全处理。安全处理 MUST 保留可能构成现有凭证模式前缀的跨块重叠，以及从敏感触发词开始到确定终止符的候选文本；只有证明不会参与敏感值的前缀才可发布。增量处理 MUST 与既有 `redact_secrets()` 自由文本语义一致；`api_key`、`password`、`secret`、`token` 不要求左侧单词边界，内嵌于常见配置名时也必须遮蔽匹配的键值片段。敏感候选的 UTF-8 缓冲硬上限 MUST 为受约束配置，范围 128～4096 bytes、默认 512 bytes；匹配内容替换为 `[REDACTED]`。候选超过上限时 MUST 关闭失败，不得泄漏候选前缀。需要完整结果才能判定的 guardrail MUST 禁用 speculative delta，只允许在最终结果通过后生成公共分片。

#### Scenario: 内嵌配置键跨任意分片保持既有脱敏语义
- **WHEN** provider 分别输出包含 `OPENAI_API_KEY`、`db_password`、`client_secret` 或 `access_token` 键值的文本，并在任意字符边界切分 fragment
- **THEN** guard、durable outbox 与公共 `model.output.delta` 的拼接文本逐字等于对完整输入调用既有 `redact_secrets()` 的结果
- **AND** 任一持久化或公开 payload 都不包含凭证原值

#### Scenario: 凭证跨越供应商分块
- **WHEN** `sk-`、`authorization`、`cookie`、`api_key`、`password`、`secret` 或 `token` 凭证形态跨越两个或更多供应商片段
- **THEN** 任一公共 delta 都不包含凭证的任何已识别敏感值
- **AND** 公共文本在对应位置仅包含 `[REDACTED]`

#### Scenario: 敏感候选迟迟没有终止符
- **WHEN** 已识别敏感触发词后的候选文本超过配置的硬上限仍无法确定边界
- **THEN** 系统在发布该候选任何部分前关闭失败
- **AND** 不扩大缓冲、不发布 completed、不发布零用量占位

#### Scenario: 完整结果 guardrail
- **WHEN** 当前输出 guardrail 声明必须观察完整结果
- **THEN** 系统在最终结果通过 guardrail 前不发布任何 delta
- **AND** 通过后才从安全最终文本生成有界公共 delta，未通过则不发布 delta 或 completed

### Requirement: 中断、unknown 与恢复
显式取消、deadline、消费者慢处理、存储失败或进程崩溃 MUST 先关闭本地流资源。只有 provider seam 明确证明远端已停止且结果已知，系统才可取消未使用占位、释放未消费 stream 容量，并按可证明事实发布中断 usage 与相应运行终态；否则 MUST 将调用置为 unknown，保留剩余 stream 和 usage 预留、未决 outbox、budget/provider lease，并阻止运行终态。恢复 MUST 只补投 `result_persisted` 的既有事件或完成本地结算，不得重新调用 provider、重新开始流或合成缺失文本。

#### Scenario: 取消后远端停止已证明
- **WHEN** 调用被显式取消且 provider seam 明确返回已停止、无更多远端副作用和 `finality=complete` 的可信 usage
- **THEN** 系统取消未使用 stream 占位并释放对应未消费容量
- **AND** 只按完整可信 usage 发布中断 usage、结算预算并释放 lease，随后才允许 `run.cancelled`
- **AND** 系统不发布 `model.output.completed`

#### Scenario: 已证明停止但 usage 不完整
- **WHEN** provider 证明远端停止，但 close result 的 usage 为 null、`finality=partial`，或 `finality=complete` 但 input/output token、当前启用 cost 任一维度不完整
- **THEN** 系统不取消未使用 stream 占位，并在同一 UoW 保留全部剩余 stream/usage 容量、reservation、未决 outbox、预算和 provider lease，进入 needs_review
- **AND** 不发布最终 usage 或运行终态，不把缺失 token/cost 记零

#### Scenario: deadline 后远端状态未知
- **WHEN** deadline 或本地取消发生后无法证明远端已停止
- **THEN** 调用进入 unknown 并保留已有 delta、剩余预留、outbox、预算和 provider lease
- **AND** completed、最终 usage 和所有运行终态保持被阻止
- **AND** close result 中已观察的 partial usage 只进入 attempt 审计，不授权释放或退款

#### Scenario: delta 持久化后发布前崩溃
- **WHEN** 某个稳定 delta 已进入 `result_persisted` 但尚未确认发布时进程崩溃
- **THEN** 恢复流程以相同 event id 和相同 payload 补投该 delta并继续既有 outbox 状态机
- **AND** 恢复流程不调用 provider，也不生成新的 chunk ordinal

#### Scenario: delta 已耐久但本次公开失败
- **WHEN** 某个 delta intent 已提交为 `result_persisted`，但 persist 返回确认、EventBus、telemetry 或 outbox published mark 尚未全部闭合，且进程内 chunk 计数可能仍为 0
- **THEN** 即使 close seam 回报 stopped 且 usage 完整，调用也进入 `needs_review` 并返回稳定 `model.provider_side_effect_unknown`
- **AND** 系统扫描完整 65 槽 durable group，保留该 delta、全部剩余 stream/usage 容量、预算与 provider lease，不按偏小的本地/已公开 chunk 计数取消任何 durable 槽位或提前发布中断 usage/terminal

#### Scenario: 持久化或发布在总 deadline 内无进展
- **WHEN** `prepare_stream` 已消耗部分冻结 route `total_timeout_ms`，当前安全 chunk、完整结果 guardrail 或尾部分片的 outbox persist / CanonicalEvent publish 无法在同一个绝对 deadline 的剩余时间内完成
- **THEN** 系统不拉取下一个 SDK event，退出本地 provider stream context，并按停止证明分类为已知中断或 unknown
- **AND** 系统不在 prepare 后重启完整 timeout、不延长 deadline、不创建 invocation queue 或无界后台任务，也不缓存、丢弃或重排后续片段

### Requirement: 成功路径的严格事件顺序
成功流式调用的可观察顺序 MUST 为：内部 `model.request.started`，零到 64 条公共 `model.output.delta`，公共 `model.output.completed`，内部 `model.usage.updated`，最后才允许运行终态。每一条流事件 MUST 先以完整 payload 持久化为 `result_persisted`，再发布 CanonicalEvent，再标记 outbox 为 `published`。取得可信最终结果后，completed intent、最终 usage result、shared-budget settlement 与未使用 delta 槽释放 MUST 在同一 UoW 提交；提交后才按 completed、usage 顺序公开。未使用 delta 占位 MUST 在 completed 可发布前取消并释放相应容量；取消的占位不产生事件。发布 sequence `n` 前 MUST 在同一锁定查询中观察到恰好且唯一的 `1..n-1` 前驱；前驱缺失、重复、非连续、未发布或未取消，以及任一预留未安全结算时，completed、usage 或运行终态 MUST 按顺序被围栏阻止。

#### Scenario: 正常完成顺序
- **WHEN** provider 返回两个安全文本增量和可信最终 usage
- **THEN** committed event seq 的相对顺序是 started、delta 1、delta 2、completed、usage、run terminal
- **AND** 每个后继发布前其 outbox 前驱均为 `published` 或明确未使用的 `cancelled`

#### Scenario: 前驱行缺失或序列损坏
- **WHEN** sequence 2 或更后的 stream outbox 准备发布，但锁定查询得到的前驱缺失、重复或不连续
- **THEN** SQLite repository 与 PostgreSQL sink 均关闭失败，不写 CanonicalEvent、不递减容量，也不把空查询解释为已结算

#### Scenario: completed 发布失败
- **WHEN** completed 已 `result_persisted` 但 CanonicalEvent sink 暂时失败
- **THEN** 同一事务保存的最终 usage result 与 shared-budget settlement 保持 `result_persisted`，usage 公开和运行终态仍被阻止
- **AND** 恢复只重放相同 completed envelope，再补投同一耐久 usage，且不重新调用 provider
