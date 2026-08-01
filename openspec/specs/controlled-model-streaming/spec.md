# controlled-model-streaming Specification

## Purpose
TBD - created by archiving change controlled-model-streaming. Update Purpose after archive.
## Requirements
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
流式invocation MUST继续在同一持久化事务、provider副作用前完成既有模型用量evidence的2槽位预留和版本化`model_stream`的65槽位预留；65个槽位仍对应64个稳定delta占位与1个completed占位，每项在副作用前具有稳定event/group/sequence和started状态，group id仍为`model-stream:{usage_call_id}`。任一预留、占位或提交失败时provider调用为零。Legacy单route的预留、取消和释放行为逐字保持Phase 18.1。

显式route chain在同一usage call、stream group和双容量预留下串行执行多个candidate；这些容量属于完整调用，不属于单candidate。Chain顺序为双容量/outbox提交→candidate reservation→attempt started identity→permit→isolated client/prepare→send/iterate。当前candidate在send/SDK context前以`client_not_started`确定失败且调用未被显式取消时，runtime MUST保留全部stream/usage槽位和outbox，在同一owner UoW写proof、关闭lifecycle并transfer reservation；不得取消65个stream占位或发布cancelled usage final。只有整个调用因显式取消、deadline或chain terminal结束时，才可按可证明事实取消未使用占位；route-chain取消只有在close result证明`stopped + complete usage`且完整durable stream group没有delta intent、`result_persisted`前缀或发布确认不明时，才可在actual取消结算的同一UoW取消未使用占位。started lifecycle未关闭、proof/transfer未知、usage不完整、关闭unknown/失败或存在任一durable delta不确定性时必须保留未决容量并needs-review。

#### Scenario: 两笔预留原子成功
- **WHEN** legacy流式调用或chain通过策略、预算和capability检查
- **THEN** 同一事务提交2个usage槽位、65个stream槽位及全部stream outbox占位
- **AND** provider第一次可能网络副作用发生在该事务成功提交之后；chain还须先完成candidate reservation与attempt-start

#### Scenario: Legacy started 发布后首次 provider 迭代前取消
- **WHEN** legacy双预留、stream占位与内部started已提交，但调用在首次SDK stream context创建或provider迭代前被取消、deadline耗尽且close result为`not_started`
- **THEN** 已发布started与high-water保持不变，65个stream占位全部取消且只释放65 outstanding
- **AND** 系统发布`outcome=cancelled`、provider未调用、token/cost为null、budget charge为可信零的stream usage final，随后释放预算与本地lease

#### Scenario: Chain candidate 准备失败保留调用级容量
- **WHEN** chain双预留已提交，当前attempt identity已耐久，permit/client/prepare在send前以`client_not_started`收敛且调用本身未取消
- **THEN** runtime不取消任何stream/usage占位、不发布cancelled final；proof、lifecycle关闭与reservation retry/transfer在同一UoW提交
- **AND** 后继attempt继续复用同一usage call、stream group与剩余容量

#### Scenario: 容量或存储不足
- **WHEN** usage或stream任一容量预留、占位写入或事务提交失败
- **THEN** 两笔预留与占位均不留下部分提交
- **AND** provider未被调用，started、delta、completed、usage和运行终态均未被伪造

### Requirement: 稳定身份、可见性与完成校验
Legacy 单 route审批流 SHALL 继续以 `approved:{approval_id}` 作为既有语义槽位。Route chain无论普通调用、审批等待还是 approved continuation，都 MUST 复用首次可信入口以原始语义 operation key生成并写入 waiting state/checkpoint 的同一 `usage_call_id`，不得因 approval id rekey或新建第二 stream/usage group；续跑必须先从私有 checkpoint重算并匹配 operation identity digest。

流式事件 MUST 使用调用前已确定、已包含 tenant 关联的 64 位小写 SHA-256 `usage_call_id` 作为定长关联根。delta 的稳定事件标识 MUST 为 `model-stream:{usage_call_id}:d:{chunk_ordinal}`，其中 `chunk_ordinal` 使用无前导零十进制从 1 连续递增到 64，最大 event id 长度为 82；completed 的标识 MUST 为 `model-stream:{usage_call_id}:c`，长度为 79。配套 usage started/final 的 `stream-usage-v1` 标识 MUST 分别为 `usage-stream:{usage_call_id}:s` 与 `usage-stream:{usage_call_id}:f`，长度均为 79。不得把原始 `tenant_id` 再拼进这些新 event id。delta 与 completed MUST 为 `public`、`terminal=false`。delta payload MUST 包含 `correlation.usage_call_id`、非 bool 正整数 `attempt`、`chunk_ordinal`、`text`；completed payload MUST 包含同一关联、同一个 `attempt`、`chunk_count`、`text_utf8_bytes` 与小写 SHA-256 `text_sha256`。`attempt` 表示全链 provider attempt ordinal：legacy 单 route 固定为 1；route chain 中按所有候选及同候选 retry 从 1 连续计数，发生安全 failover 后，所有 delta/completed 必须使用实际产出文本候选的同一个 ordinal，不能按候选重置。该值不进入 event id，事件 payload 不新增 candidate/provider 字段；消费者通过同一 usage evidence 的 attempt/candidate 映射解释它。completed 摘要 MUST 与已发布 delta 拼接结果和最终 `ModelResponse.output_text` 完全一致。

#### Scenario: 成功完成的事件身份和摘要
- **WHEN** 流式调用成功完成并发布三个 delta
- **THEN** 三条 delta 使用连续 chunk ordinal 1～3、确定性 event id 和实际产出候选的同一全局 attempt，completed 使用同一调用根身份与 attempt
- **AND** completed 的 `chunk_count=3`、字节数与 SHA-256 同时匹配 delta 拼接文本和最终响应文本

#### Scenario: 安全 failover 后由第二个 provider attempt 产出文本
- **WHEN** 全链 attempt 1 以 `client_not_started|trusted_business_not_started` 安全收敛，attempt 2 产生全部 delta并完成
- **THEN** 所有 delta/completed payload 的 `attempt=2`，event id 与 stream group 仍只由原 usage call id 和 chunk ordinal 决定
- **AND** route-chain usage evidence 的 attempt 2 逐值映射到 selected candidate；attempt 1 若为 `client_not_started` 则保留未发送事实并记零，若为 `trusted_business_not_started` 则保留 started/request/response 历史并凭完整 trusted proof 以 actual zero 结算

#### Scenario: 最终结果与增量不一致
- **WHEN** provider 最终结果文本与安全处理后的 delta 拼接文本不一致，或 delta/completed attempt 不同、为 bool/零/负数、在同一 stream 中变化
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
显式取消、deadline、消费者慢处理、存储失败或进程崩溃 MUST先关闭本地流资源。Legacy单route继续只有provider seam明确证明远端已停止且结果已知时才能取消未使用占位、释放未消费stream容量并按事实发布中断usage/terminal；否则进入unknown，保留容量、outbox、budget/provider lease并阻止terminal。Legacy recovery仍只补投`result_persisted`事件或完成本地结算，绝不重新调用provider或重新开始流。本地`prepared`对象只证明资源所有权，不证明request已发送或provider已调用；`aclose()`失败必须完成最佳努力清理，但不能覆盖已形成的稳定`model.invocation_cancelled|model.provider_side_effect_unknown`。

显式route chain同样禁止重放任何已存在started identity、已发送attempt、已观察response/usage/text/delta或delta-fenced流。唯一允许的后继调用不是重放：当前candidate从首次到末次的全部lifecycle已在单一UoW原子关闭为`not_started_proven`并与proof逐值匹配，reservation已按冻结ordinal提交retry/transfer，后继global attempt identity尚不存在，且没有显式run取消/deadline/unknown时，recovery MAY先创建新的后继attempt identity，再调用该冻结candidate一次。Proof与transfer必须同事务，因此不存在“proof已提交但尚未决定是否transfer”的可重选窗口；commit-ack未知、悬空started、任何结果观察或首delta fence均保留reservation/capacity并needs-review。显式取消/deadline只有在close result逐值证明`state=stopped`、usage `finality=complete`且所有启用维度完整，同时durable group无delta intent/发布不确定性时，才以同一owner UoW把lifecycle/candidate/claim收敛为`settled/cancelled/result_committed`、按actual替换reservation并清空active/waiting/selected/current reservation；不追加transition、不发布completed、不fallback。Recovery只消费durable chain决定，不按current config、余额或健康状态重选。

#### Scenario: Legacy 取消后远端停止已证明
- **WHEN** legacy调用被显式取消且provider seam明确返回已停止、无更多远端副作用和`finality=complete`的可信usage
- **THEN** 系统取消未使用stream占位并释放容量，只按完整usage结算，随后才允许`run.cancelled`
- **AND** 不发布`model.output.completed`，不重新调用provider

#### Scenario: Chain 取消以完整 stopped usage 结算actual
- **WHEN** route-chain stream因显式取消或deadline结束，provider close result证明远端已停止、usage finality为complete且启用维度完整，完整durable group没有delta intent、result-persisted前缀或发布确认不明
- **THEN** runtime按actual usage结算，candidate=`cancelled/invocation_cancelled/result_committed`、matching lifecycle=`settled/result_committed/completion_observed=false`，selected/active/waiting为空且current reservation为canonical空
- **AND** 取消未使用stream占位并释放相应容量，final usage的outcome为cancelled且error为`model.invocation_cancelled`；不发布`model.output.completed`、不调用后继provider

#### Scenario: Chain 取消的 not-started 或空关闭事实不推断调用
- **WHEN** route-chain attempt identity已耐久，显式取消时只取得prepared对象、close返回not-started或没有request/response/result/usage/text/delta正观察
- **THEN** runtime不生成not-started proof、不fallback，按unknown/needs-review保留reservation与容量；安全错误的`provider_called=false`且attempt_count仍包含该identity
- **AND** cleanup失败不得覆盖稳定安全错误，也不得因prepared对象把request-sent或provider-called改为true

#### Scenario: Chain 已提交安全 transfer 后恢复后继
- **WHEN** candidate A全部attempt均已proof-close为not-started，owner UoW已原子提交A→C transfer并跳过零attempt候选B，C的attempt identity尚不存在，且无取消、deadline、delta或unknown
- **THEN** SQLite/PostgreSQL recovery只按durable ordinal先创建C的新started identity，再让C恰好调用一次
- **AND** 不重发A、不重评B、不按恢复时余额或配置重选

#### Scenario: Chain started 或 transfer 确认未知不调用后继
- **WHEN** 任一attempt lifecycle仍为started/unknown，或proof/transfer commit-ack未知
- **THEN** recovery保留stream/usage容量、outbox、budget与lease并进入needs-review
- **AND** 不重发当前attempt、不创建下一attempt或candidate、不发布completed/final usage/terminal

#### Scenario: 已证明停止但 usage 不完整
- **WHEN** provider证明远端停止，但close result usage为null/partial，或complete但启用维度不完整
- **THEN** 两种模式均不取消未使用占位，保留全部剩余容量、reservation、outbox、budget与lease并needs-review
- **AND** chain不把stopped或不完整usage解释为not-started proof

#### Scenario: deadline 后远端状态未知
- **WHEN** deadline或本地取消发生后无法证明远端已停止
- **THEN** 调用进入unknown并保留已有delta、剩余预留、outbox、预算和provider lease
- **AND** completed、最终usage、运行终态与任何后继provider均被阻止

#### Scenario: delta 持久化后发布前崩溃
- **WHEN** 某个稳定delta已进入`result_persisted`但尚未确认发布时进程崩溃
- **THEN** 恢复以相同event id和payload补投该delta并继续既有outbox状态机
- **AND** legacy不调用provider；chain一旦存在delta intent也设置/视为delta-fenced，不调用当前或任何后继provider、不生成新chunk ordinal

#### Scenario: delta 已耐久但本次公开失败
- **WHEN** 某个delta intent已提交为`result_persisted`，但persist返回确认、EventBus、telemetry或outbox published mark尚未闭合，且本地chunk计数可能仍为0
- **THEN** 即使close seam回报stopped且usage完整，两种模式都进入`needs_review`并返回稳定`model.provider_side_effect_unknown`
- **AND** 系统扫描完整65槽durable group，保留delta、全部剩余stream/usage容量、预算与provider lease，不按偏小计数取消槽位、发布中断usage/terminal或切换provider

#### Scenario: 持久化或发布在总 deadline 内无进展
- **WHEN** `prepare_stream`已消耗部分冻结route `total_timeout_ms`，当前安全chunk、完整结果guardrail或尾部分片的persist/publish无法在同一绝对deadline剩余时间内完成
- **THEN** 系统不拉取下一个SDK event，退出本地provider stream context，并按停止证明分类为已知中断或unknown
- **AND** 系统不在prepare后重启完整timeout、不延长deadline、不创建无界后台任务、不缓存/丢弃/重排后续片段，chain也不把deadline解释为fallback授权

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

### Requirement: 流式 route chain 只在首 delta 前可信切换
`text_stream` route chain SHALL 复用同一 frozen chain、usage stable key、stream group 和 candidate reservation。显式 chain 的唯一顺序为 `capacity/outbox reservation → candidate reservation → durable attempt started identity → Bulkhead permit → candidate-isolated client lease/prepare_stream → send/iterate`；legacy 单 route 继续保持既有 `reservation → permit → client → durable side_effect_started → send`，不得被 chain 顺序反向改写。每个首次stream调用或retry都必须按所属模式在client/send/iterate边界前先耐久追加全局连续、不可覆盖的attempt started identity；已存在started record即使`request_sent=false`也不得在恢复时自动重发。在尚未观察 provider delta、尚未持久化任何 stream output event且 `delta_fenced=false` 时，runtime 只可在当前 candidate 从首次到末次的每个 lifecycle record 都已原子关闭为`not_started_proven`、与连续不可覆盖的 `client_not_started|trusted_business_not_started` proof逐值匹配且没有started/unknown/settled冲突项时，按冻结 ordinal扫描并原子 transfer 到首个可预约后继 candidate；中间 `static_ineligible|budget_ineligible` candidate保持零 attempt/proof/reservation/provider，只有一条 current→selected successor transition。每次同 route retry 前 MUST 先耐久追加上一 attempt proof并关闭同一lifecycle，再创建下一started identity；permit/client/prepare在send前失败时也必须原子追加`client_not_started` proof并关闭同一started lifecycle，崩溃或commit-ack未知则保留reservation并进入needs-review。trusted-business record由端点绑定 classifier、显式状态白名单、无 response identity/usage/text/delta 共同证明，并保留 claim/candidate started与 `request_sent=true/http_response_observed=true` 聚合历史。末次 client proof不能抹除早期 trusted-business response；proof 缺失/覆盖/重排、其他 started/response、write/read timeout、unknown 或任一 delta 都 MUST 永久禁止跨 provider transfer。观察首 delta 时立即设置进程内 fence，持久化首 delta intent 的同一 UoW MUST 设置 durable `delta_fenced=true`。

#### Scenario: 首 delta 前且 send 前的 not-started
- **WHEN** 第一 streaming candidate 的durable attempt started identity已提交，随后permit/client/prepare在send前以`client_not_started`确定失败，且没有任何HTTP response或delta
- **THEN** runtime 可按 frozen ordinal transfer，并让第二 candidate 恰好启动一次
- **AND** 首候选保持`request_sent=false`，proof与lifecycle的`started→not_started_proven`及reservation→第二候选reservation在同一UoW原子替换；不得删除started identity

#### Scenario: 首 delta 前 retry started 后崩溃
- **WHEN** 前一attempt已证明not-started，下一attempt的started identity已提交，但stream在send前或send后、proof/settlement前崩溃
- **THEN** SQLite/PostgreSQL恢复保留current reservation并进入needs-review，不把数据库中尚无delta或`request_sent=false`解释为可重试
- **AND** 不重发该attempt、不创建下一attempt、不切换provider，reader重连也只能读取已提交事件

#### Scenario: 首 delta 前的受信业务未开始 response
- **WHEN** 第一 streaming candidate 已发送并收到白名单 429/503 或显式启用的 403，端点 classifier 给出合法 not-started proof，且尚无 response identity、usage、text 或 delta
- **THEN** runtime 保留 request/response/started 历史，以 `trusted_business_not_started` actual zero 原子转移并恰好启动下一候选一次
- **AND** 缺 header、状态未列入白名单或发生含糊 timeout 时不切换并进入原错误或 unknown

#### Scenario: 首 delta 前混合两类 proof 跨三候选
- **WHEN** streaming 候选 A 以 `trusted_business_not_started` 收敛，候选 B 在 client/send 前以 `client_not_started` 收敛，且全链尚未观察或持久化 delta
- **THEN** claim 高水位保持 started，A/B 的逐候选 side-effect/request/response/proof 历史独立保真，候选 C 恰好启动一次
- **AND** SQLite/PostgreSQL 恢复只继续 C，不回退 started、不重放 A/B；反向 proof 顺序遵循同一规则

#### Scenario: 首 delta 前跳过预算不可用中间候选
- **WHEN** streaming A 已可信 not-started，B 为 `budget_ineligible`，C 可预约且全链尚无 delta fence
- **THEN** 同一 UoW只产生 A→C transfer，B 的 attempt/client/provider调用为零，随后仅启动 C
- **AND** 若没有可预约后继则原子 exhausted并释放 A reservation，recovery不按新余额重选

#### Scenario: 首 delta 已观察但持久化失败
- **WHEN** provider delta 已进入 invocation，但 durable delta intent 或 fence 提交失败
- **THEN** 调用进入 unknown 并保留 stream/usage/预算 reservation
- **AND** 不因数据库中尚无 delta 而切换 provider

### Requirement: 首 delta 后永久禁止跨 provider fallback
一旦任一 delta 被观察或提交，取消、deadline、storage backpressure、reader 断线、`Last-Event-ID`、CLI reconnect、恢复与重启 MUST NOT 推进 candidate ordinal或调用另一 provider。系统 MUST 保留 committed prefix、未决 stream/usage 容量、reservation 与 provider lease，按 Phase 18.1 stopped/unknown 证明结算。

#### Scenario: delta 后取消
- **WHEN** 一个 public delta 已提交后调用被取消
- **THEN** committed prefix 保持可读，chain selected ordinal 不变
- **AND** 后续 provider 调用次数为零

#### Scenario: reader 重连
- **WHEN** SSE/CLI 在 delta 后断线并携带 committed cursor 重连
- **THEN** reader 只续读已提交 CanonicalEvent
- **AND** 不访问 route chain controller、provider cursor 或 reservation transfer

