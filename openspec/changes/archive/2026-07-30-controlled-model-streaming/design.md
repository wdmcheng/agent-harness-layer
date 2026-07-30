## Context

Phase 18 已把一次性真实模型调用放进 policy、budget、provider permit、用量 evidence、outbox、unknown recovery 与运行终态围栏。当前 provider 契约只有最终 `ModelResponse`，事件枚举虽已预留 `model.output.delta` / `model.output.completed`，却没有生产者，也没有针对未知数量增量的副作用前容量证明。

Phase 18.1 不能在 SDK callback 上直接挂 SSE。SSE 和 CLI 已经是 CanonicalEvent 的只读传输，供应商 SDK 又必须留在 adapter；因此真正要补的是 provider → router → invocation → durable event 的生产链。设计还必须同时守住 AC-085～AC-088：有界、有序、跨块安全、最终一致、断点续读、取消/unknown 与崩溃恢复。

当前锁文件提供 Pydantic AI 2.5.0。该版本 `Agent.run_stream_events` 在首次迭代时才启动后台 run，使用 zero-buffer anyio memory stream 将 SDK 事件交给消费者，并要求 async context manager 负责提前退出时的确定性本地清理。它最终产生唯一 `AgentRunResultEvent`。这些是适配器可依赖但不得外泄的锁定版本语义；依赖升级必须由合同测试重新证明。

## Goals / Non-Goals

**Goals:**

- 以供应商中立协议交付追加式文本增量和最终 `ModelResponse`。
- 在 provider 副作用前证明模型用量和最坏流事件容量，并让每个可能事件已有稳定 outbox 身份。
- 让公开 delta 经过有状态安全处理、有界分片和持久化后才可见。
- 让成功、中断、unknown、存储失败和崩溃都复用现有 evidence、预算、lease 与终态围栏。
- 让 SSE/CLI 仅靠 committed event seq 读取和续读，无 provider cursor 或重启。

**Non-Goals:**

- 不新增 HTTP/WebSocket API、聊天 UI、供应商事件透传或动态容量。
- 不改变既有 `ModelProvider.complete`、非流式 fake 与 Phase 18 一次性结算。
- 不引入自动重试、provider fallback、远端恢复游标或后台无限缓冲。
- 不在默认测试中访问网络或凭证，也不在本 change 内 sync/archive/release。

## Decisions

### 1. 新增正交流式协议，不扩张 `complete`

在 `models.providers` 增加供应商中立的 `ModelStreamDelta`、`ModelStreamUsage`、`ModelStreamCloseResult`、`PreparedModelStreamCall` 与 `ModelStreamingProvider`。`ModelStreamDelta` 只承载追加文本，成功最终值仍复用 `ModelResponse`。`ModelStreamUsage` exact fields 为 `finality=partial|complete`、nullable input/output token、nullable cost、cost status 与 latency；`ModelStreamCloseResult` 组合 `state=not_started|stopped|unknown` 与 nullable usage，并校验 not_started 无 usage、unknown 不接受 complete。`PreparedModelStreamCall` 明确区分 prepare、首次迭代副作用、成功最终结果与关闭结果。

关闭后的 usage 只从 adapter 转换，invocation 不接触 SDK object。stopped + complete usage 仍要按当前 route 验证 token/cost 维度全部可信，才可生成中断 `ModelUsageEvidence`、结算预算和释放 lease；stopped 的 null/partial 与 unknown 的 null/partial 仅进入 attempt evidence，保持 needs-review。这样既不滥用成功 `ModelResponse`，也不为部分计量另造第二套结算账本。

router 新增 `prepare_stream`，仅接受 `capability=text_stream` 且 provider 实现流式协议的 route。当前配置规划与冻结快照恢复必须都把 `text_stream` 当作精确、受信的独立 capability；结算、重放与恢复校验必须接受同一冻结 route evidence，且不能把任意未知 capability 放宽为合法。prepare 可以获得 permit/client lease，但不得开始 SDK 迭代；invocation 完成两笔容量预留、outbox 占位和 started 发布后才迭代。

选择正交协议而不是把 `complete` 改成联合返回类型，是为了保持现有 provider/test double 的静态兼容，并让“不支持流式”在副作用前显式失败。也不使用“把完整字符串按字符切开”的 fallback，因为那会谎报实时语义。

### 2. 固定 64 + 1 的 stream 容量，usage 仍是独立 2 槽位

版本化 `EvidenceOperationKind.MODEL_STREAM` 的注册容量固定为 65：64 个 delta 加 1 个 completed。`MODEL_USAGE` 仍固定为 2：started 加 final usage。一次流式调用在同一 UoW 中同时 claim 两个 operation；任何一步失败都会回滚，provider 不会被调用。

选择两笔关联预留而不是把 `MODEL_USAGE` 从 2 扩为 67，是为了不改变既有非流式调用、历史回填和 validator 的含义。两笔 operation 共享 `usage_call_id` 关联，并同时纳入终态 pending 检查。

### 3. 副作用前预建 65 个有序 outbox 占位

在既有 `run_evidence_outbox` 中预建一个 stream group：delta 1～64 与 completed 共 65 行，初始为 `started`，每行 `reserved_event_count=1`，并写入稳定 event id、group id、operation kind 与 sequence。真实安全 chunk 形成后，把对应 delta 行补全为 `result_persisted`，调用方随即登记 durable chunk，随后 EventBus 发布，再标记 `published`。数据库提交成功但 persist 返回确认丢失时，进程内 chunk 计数可能仍为 0；因此任何中断结算都扫描完整 65 槽 group，而不是用本地计数裁剪 durable 检查。若 persist 确认、publish、telemetry 或 published mark 在 intent 提交后失败，该调用强制进入 needs-review，保留 `result_persisted` 与剩余围栏供恢复补投，不能先取消槽位或发布中断 usage。

成功取得最终结果后，在同一事务中取消所有未使用 delta 占位并释放等量未消费容量、将 completed 行写为 `result_persisted`，并持久化最终 usage result 与 shared-budget settlement。事务提交后才先发布 completed、再发布 usage；若任一公开发布失败，两类 `result_persisted` 均可按相同顺序补投，不能回退占位或重放 provider。每个 sequence 发布前都锁定并逐值核对完整 `1..n-1` 前缀；数据库唯一约束拒绝重复 sequence，repository/sink 共同拒绝缺失、非连续或未结算前驱，不能把只返回现存行的空查询解释为已结算。unknown 保留尚未使用的 `started` 占位和预留，天然阻止终态。

选择占位而不是副作用后逐条新增 outbox，是因为只有占位能在调用前同时证明容量、稳定身份和恢复边界。现有表已经有 `operation_kind`、`group_id`、`sequence_in_group`、`reserved_event_count`、`state` 与 JSON result，数据库结构足够；本 change 不需要迁移。

### 4. 公共事件身份与 payload 固定

- group id：`model-stream:{usage_call_id}`，固定长度 77。
- delta event id：`model-stream:{usage_call_id}:d:{chunk_ordinal}`，ordinal 为无前导零的 1～64，最大长度 82。
- completed event id：`model-stream:{usage_call_id}:c`，固定长度 79。
- stream usage started/final event id：`usage-stream:{usage_call_id}:s|f`，固定长度 79，私有 identity 版本为 `stream-usage-v1`。
- delta：`public`、`terminal=false`，payload 为 `correlation.usage_call_id`、`attempt=1`、`chunk_ordinal`、`text`。
- completed：`public`、`terminal=false`，payload 为关联、attempt、`chunk_count`、`text_utf8_bytes`、`text_sha256`。
- started 与 usage 沿用既有内部事件；run terminal 沿用既有公共终态。

`usage_call_id` 是已包含 tenant/run/request/agent/trace 与语义调用槽位的 64 位 SHA-256，因此不再把最长 64 字符 tenant 拼入新数据库 identity。上述 event/group id 均低于 `canonical_events.id` 与 outbox `event_id/group_id` 的 128 字符上限。`stream-usage-v1` 只用于新 `text_stream` row，并以 `started.decision.usage_event_identity={"ref":"stream-usage","version":"v1"}` 精确写入 durable evidence；旧 row 缺少该对象时继续按已持久化的 legacy identity 恢复，不重命名或重键，避免本 change 改写历史。invocation 维护安全文本的增量 SHA-256、UTF-8 byte count 和 chunk count。最终 `ModelResponse.output_text` 经过相同安全处理后必须逐字节等于公共 delta 拼接结果；不一致属于无法证明的 provider/adapter 结果，不发布 completed 或 usage。

### 5. 分片器以项目边界为准，不继承供应商分块

固定硬上限是 64 个 delta、单条最多 4096 UTF-8 bytes。typed config 只允许把目标 chunk size 设为 1～4096，默认 1024；它不改变容量合同。分片器在 Unicode 字符边界切分，忽略空 SDK delta，并仅生成非空公共 delta。

invocation 在进入 `prepare_stream` 前建立唯一绝对 monotonic deadline；同一个 `asyncio.timeout_at` 覆盖 prepare、SDK 消费、完整结果 guardrail、尾部分片及各 delta 的持久化/发布，prepare 后不得重新获得完整 `total_timeout_ms`。每个安全 chunk 持久化和发布完成后，invocation 才拉取后续 SDK 事件。Pydantic AI 2.5.0 的 zero-buffer event stream 会自然把背压传回 SDK run，不需要项目再建 invocation queue。outbox persist 或 EventBus publish 无法在剩余 deadline 内推进时退出 context，并按 adapter 返回的关闭状态分类；不延长 deadline，也不预拉取或缓存后续 SDK event。若未来 provider adapter 内部需要通道，该通道必须是 zero-buffer 或显式有限容量，不能改变公共上限。

### 6. 有状态安全处理在持久化之前

新增独立的 incremental text guard，复用 `security.redaction` 中已冻结的凭证触发词和替换语义，但不能对每个 SDK chunk 分别调用现有 regex。`authorization`/cookie 保留既有单词边界，`api_key`/`password`/`secret`/`token` 则与既有规则相同，不额外添加左侧边界；因此 `OPENAI_API_KEY`、`db_password`、`client_secret`、`access_token` 等配置名也按完整文本规则遮蔽匹配键值片段。authorization 的可选 `Bearer|Basic` scheme 也必须保留既有正则回退：流结束时只有 scheme 而没有 token，仍把 scheme 作为值遮蔽并保留尾随空白。guard 保留两类状态：可能成为触发词的最长后缀，以及从已识别触发词开始、尚未遇到终止符的敏感候选。只有已证明不会加入候选的前缀才能交给分片器。

敏感候选缓冲默认 512 UTF-8 bytes，typed config 只允许 128～4096。匹配完成后整体替换为 `[REDACTED]`；候选溢出时不释放任何候选字节，立即关闭流并按停止证明分类。这样对任意供应商边界都不会先泄漏半个 secret。声明必须观察完整结果的 output guardrail 则关闭 speculative 模式：SDK 文本只进入最多 `64*4096` UTF-8 bytes 的有界 collector，最终通过后再分片。provider-neutral delta 在 DTO 边界以逐 code point 的有界 UTF-8 计数拒绝单个超大 fragment，不构造同尺寸 bytes，并缓存合法字节数；adapter 在追加观察列表前核对累计上限，invocation 复用缓存值。collector 结束时才 join 一次，避免任意大输入先入列或高碎片输入形成二次方复制；超限时不扩容，立即关闭并按停止证明分类。

选择状态机而不是固定尾窗 regex，是因为 `authorization`、`cookie` 和 key/value 模式的值长度不固定；仅保留 N 个尾字符会在长值结束前泄漏前缀。候选硬上限配合 fail closed 才形成可证明边界。

### 7. Pydantic AI adapter 使用 `run_stream_events`

`PydanticAIModelProvider` 实现独立 stream prepare。adapter 进入 `Agent.run_stream_events` context 后，仅处理：

- `PartStartEvent` 中的 `TextPart.content`；
- `PartDeltaEvent` 中的 `TextPartDelta.content_delta`；
- 唯一尾部 `AgentRunResultEvent`。

`PartEndEvent` 的完整 part 不再重复输出；reasoning、tool、structured 和其他事件不进入公共协议。最终事件用于构造 `ModelResponse` 与读取 `RunUsage`。adapter 只读取一次 SDK usage 并缓存 provider-neutral 转换结果；读取异常或 bool、负数、非整数等非法 usage 统一保存为 unknown 事实，`result()` 返回稳定错误，`aclose()` 复用缓存并返回 `state=unknown`，不得二次读取后把原始异常逃逸出关闭边界。缺失/重复 final、非字符串最终输出或增量/最终文本不一致都关闭失败。禁止使用 `stream_text(delta=True)`，因为锁定版本文档明确该捷径会跳过结果 validator；也不使用 `run_stream` 的“首个符合 output_type 即结束”语义。

调用方取得 iterator 只表示请求首次迭代，不等于 provider 已开始。started telemetry 仍位于外部取消收口内，但不消耗冻结的 provider route deadline；该绝对 deadline 在 telemetry 完成后、进入 `prepare_stream` 前建立。若 runtime deadline 或 adapter deadline 在 `run_stream_events` context 创建前耗尽，adapter/编排仍能证明 `not_started`；invocation 按 started 后零调用合同取消 65 个占位、发布 cancelled usage final，并释放预算与 lease，不得误记为普通 failed。一旦 context 已创建，提前退出只证明本地后台 task 已清理，不能单独证明远端停止；因此 cancellation、deadline、transport error 和 context exit 默认分类为 `unknown`。只有受控 fake 或未来拥有明确供应商停止确认的 adapter 才能返回 `stopped`。

### 8. invocation 使用新的流式执行 mixin，结算仍复用 Phase 18

`ModelInvocationService` 增加底层 public 异步 `stream(...)->ModelResponse` seam；新的 `_invocation_streaming.py` 只承担策略与纵向协调，显式协作者合同、消费/跨块安全、事件持久化/发布和中断结算分别放入 `_streaming_contracts.py`、`_streaming_consumption.py`、`_streaming_events.py` 与 `_streaming_settlement.py`，避免把新的流式职责继续堆入 `_invocation_execution.py` 或单一核心文件。生产 Agent executor 不直接取得该未绑定服务：`build_execution_context()` 继续把它封闭成 `BoundModelInvocationService`，后者新增异步 `stream(request, operation_key=...)->ModelResponse` 与 `stream_approved(request, operation_key=..., grant=...)->ModelResponse`。两者在 durable completed/usage 闭合后才返回最终响应，provider delta 只进入 CanonicalEvent，不把第二个 iterator 暴露给 executor。普通入口只用绑定的 identity/tenant/run/agent/request/trace 和语义 `operation_key` 生成稳定 `usage_call_id`；审批入口复用 `complete_approved` 的 durable grant 全绑定、单次 lease 与 current hard-gate 重检，identity 槽位固定为 `approved:{grant.approval_id}`，不允许业务 executor 传入 call identity 或通过改变 `operation_key` 扩大批准次数。普通 stream 命中 soft approval 时以既有 approval-required seam 停止且零容量/started/provider 副作用；只有 continuation 的匹配 grant 可进入 `stream_approved`。

策略、route、budget、permit、usage evidence、attempt evidence 与成功/unknown 结算复用已有 helper；流式协调层只编排双预留、占位、guard、chunk publish 与 completed，四个内部模块通过无 `Any` 的 `StreamingRuntime` 协作者视图使用既有 UoW/预算能力。业务调用结束后，SSE/CLI 仍只从 event store 消费 committed 事件，既不直接持有 provider iterator，也不承担启动或重启调用的职责。

双预留事务尚未提交时，取消按普通 UoW 回滚。事务和 started 已提交后，任何路径都不得回退 high-water 或删除 evidence。started 与首次 provider 迭代之间若取消，adapter 返回 `not_started`：stream 模块取消全部 65 个占位并只释放 outstanding，复用 usage settlement 发布 `outcome=cancelled` final；attempt 为 not-started、provider 未调用、provider usage/cost 为 null/unavailable，而 budget charge 按既有 not-started 矩阵为可信零。usage final 消费第二个 usage 槽位，之后释放预算和本地 lease，最后才开放 `run.cancelled`。

成功路径严格是 started → delta* → completed → usage → run terminal。已证明中断不发布 completed；只有 close result 为 stopped 且 usage `finality=complete`、input/output token 与当前 route 启用的 cost 维度全部可信时，才发布带中断 outcome 的 usage 并结算。stopped 的 null/partial、虽标记 complete 但任一启用维度不完整，以及 unknown 的 null/partial 都只进入 attempt，均不发布 final usage/terminal，并保留需要人工处理的 budget、lease、容量和 outbox。任何 retry/fallback 都保持关闭。

### 9. 崩溃恢复只处理 durable state

stream recovery 扫描 `MODEL_STREAM` outbox：

- `result_persisted`：用既有稳定 event id/payload 补投并标为 published；
- `started` 且调用已 unknown：保持未决，供 needs-review；
- 正常完成但 publication 中断：按 group 前驱顺序补投，之后才恢复 usage final 和终态；
- cancelled：无事件可投，不推进 high-water。

恢复不得构造 provider 对象、进入 SDK context、调用 route、重新生成文本或计算“可能的”usage。稳定 envelope 冲突沿用 sink replay conflict，关闭失败并保持终态围栏。

### 10. SSE/CLI 保持纯读取层

新 delta/completed 是 public CanonicalEvent，因此现有 SSE `Last-Event-ID` 和 CLI `--after-seq` reader 无需 provider-aware 代码。默认公共过滤自然显示 delta/completed 并隐藏内部 started/usage。reader cancellation 只结束读取 task，不拥有 invocation 或 provider 资源。验证重点是 transport 不新增调用依赖、重连只按 committed seq 续读。

## Affected Surfaces

本 change 的单一写 owner 精确冻结为以下路径。实现若证明还需修改清单外的生产、测试、脚本或 CI 文件，先更新本节、tasks 与 change matrix，再重新 strict 和 fresh review，不得以“相关文件”在实现期扩面。

- Provider/router：`packages/agent-harness/src/agent_harness/models/providers.py`、`packages/agent-harness/src/agent_harness/models/router.py`、`packages/agent-harness/src/agent_harness/models/_router_current.py`、`packages/agent-harness/src/agent_harness/models/_router_snapshot.py`、`packages/agent-harness/src/agent_harness/models/__init__.py`。当前配置规划与冻结快照恢复都只允许精确的 `text_completion` / `text_stream` capability，并保持现有非流式路由行为不变。
- Invocation/可信入口/结算：`packages/agent-harness/src/agent_harness/models/invocation.py`、新建 `packages/agent-harness/src/agent_harness/models/_invocation_streaming.py`、`packages/agent-harness/src/agent_harness/models/_streaming_contracts.py`、`packages/agent-harness/src/agent_harness/models/_streaming_consumption.py`、`packages/agent-harness/src/agent_harness/models/_streaming_events.py`、`packages/agent-harness/src/agent_harness/models/_streaming_settlement.py`、`packages/agent-harness/src/agent_harness/models/_settlement_contracts.py`、`packages/agent-harness/src/agent_harness/models/_invocation_evidence.py`、`packages/agent-harness/src/agent_harness/models/_invocation_settlement.py`、`packages/agent-harness/src/agent_harness/models/_settlement_publication.py`、`packages/agent-harness/src/agent_harness/models/_settlement_validation.py`、`packages/agent-harness/src/agent_harness/models/_settlement_evidence_validation.py`、`packages/agent-harness/src/agent_harness/models/usage_events.py`、`packages/agent-harness/src/agent_harness/runtime/executor.py`。结算、重放和恢复的 route evidence validator 必须逐值接受 `text_stream`，取消 outcome 也必须保持封闭稳定形状；`_settlement_contracts.py` 只为所有调用路径增加封闭的 provider/runtime failure domain，不进入公开 artifact。stream 成功路径复用 publication 私有 seam，把 completed intent、usage result、共享预算和尾部释放纳入同一 UoW，再按 completed → usage 公开。未知 capability、字段漂移或 completed/failure 混合继续关闭失败。
- 分片与安全：新建 `packages/agent-harness/src/agent_harness/models/streaming.py`，并复用 `packages/agent-harness/src/agent_harness/security/redaction.py` 的稳定规则；若红测要求修改后者，必须先回到本清单重审。
- Vendor adapter：`packages/agent-harness/src/agent_harness/adapters/models/fake.py`、`packages/agent-harness/src/agent_harness/adapters/models/pydantic_ai.py`、私有流生命周期协作者 `packages/agent-harness/src/agent_harness/adapters/models/_pydantic_ai_streaming.py`、`packages/agent-harness/src/agent_harness/adapters/models/_pydantic_ai_client.py`；SDK 只存在于该边界，拆分不增加公共导出。
- Event/storage/recovery：`packages/agent-harness/src/agent_harness/events/capacity.py`、`packages/agent-harness/src/agent_harness/events/bus.py`、`packages/agent-harness/src/agent_harness/events/types.py`、`packages/agent-harness/src/agent_harness/events/local_capacity.py`、`packages/agent-harness/src/agent_harness/events/sinks/postgresql.py` 与私有 stream 校验协作者 `packages/agent-harness/src/agent_harness/events/sinks/_postgresql_streaming.py`、`packages/agent-harness/src/agent_harness/storage/event_capacity_repositories.py`、`packages/agent-harness/src/agent_harness/storage/evidence_repositories.py`、`packages/agent-harness/src/agent_harness/storage/usage_evidence_repositories.py`、同 UoW 私有 mixin `packages/agent-harness/src/agent_harness/storage/usage_attempt_review_repository.py`、`packages/agent-harness/src/agent_harness/storage/_shared_budget_replay_repository.py`、新建 `packages/agent-harness/src/agent_harness/storage/stream_evidence_repositories.py`、`packages/agent-harness/src/agent_harness/runtime/_orchestrator_base.py`。新 stream repository 作为 `EvidenceOutboxRepository` 的职责 mixin 绑定既有同一 `AsyncSession`，继续经 UoW 已装配的 `evidence_outbox` 访问，因此本设计不修改 `storage/adapters/sqlalchemy.py` 或增加第二个 repository 属性；attempt-review mixin 也只复用同一 `_session`，不改变 UoW 属性。local/PostgreSQL 两个 sink 必须在写事件并递减 outstanding 前原子核对 stream event id、group、sequence、event type 与 payload identity，不能只按 event id 取 reserved count。stopped/unknown partial 必须在同一 UoW 固化 usage attempt review 与预算 needs-review result；重放 validator 只接受两边相同的封闭 review，不能据此发布 final 或再次调用 provider。
- Composition/config：`packages/agent-harness/src/agent_harness/config/schemas.py`、`packages/agent-harness/src/agent_harness/config/settings.py`、`packages/agent-harness/src/agent_harness/runtime/services.py`、`templates/service-app/app/runtime.py`；fake 仍为默认 provider。
- Transport 回归：`tests/contracts/test_sse_http_openapi_contracts.py`、`tests/contracts/test_cli_event_stream_contracts.py`、`tests/contracts/test_sse_event_reader_local_contracts.py`、`tests/contracts/test_sse_event_reader_postgresql_contracts.py`、`tests/integration/test_sse_http_streaming_contracts.py`；生产 SSE/CLI reader 路径不修改，测试只证明新事件与 reader ownership 契约。
- 新增聚焦测试：`tests/contracts/model_streaming_sdk_event_test_helpers.py`、`tests/contracts/controlled_model_streaming_context_typecheck.py`、`tests/contracts/test_controlled_model_streaming_provider_contracts.py`、`tests/contracts/test_controlled_model_streaming_routing_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_success_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_interruption_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_cancellation_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_started_cancellation_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_composition_close_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_replay_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_guardrail_contracts.py`、`tests/contracts/test_controlled_model_streaming_security_contracts.py`、`tests/contracts/test_controlled_model_streaming_capacity_contracts.py`、`tests/contracts/test_controlled_model_streaming_capacity_config_contracts.py`、`tests/contracts/test_controlled_model_streaming_recovery_contracts.py`、`tests/contracts/test_controlled_model_streaming_postgresql_contracts.py`、`tests/contracts/test_controlled_model_streaming_transport_contracts.py`、`tests/contracts/test_controlled_model_streaming_live_smoke_contracts.py`、`tests/contracts/test_controlled_model_streaming_approval_contracts.py`、`tests/integration/test_controlled_model_streaming_live_smoke.py`，以及复用并扩展 `tests/contracts/controlled_real_model_policy_approval_test_support.py`。SDK event helper 只提供本地 shape doubles 并 monkeypatch adapter 内部类型，测试层不得直接导入 vendor 包；静态类型夹具由仓库 Pyright 分析，只经 adapter protocol 验证正确/错误 local double，不直接导入 vendor SDK；审批支持文件必须继续通过真实 `build_execution_context()`/orchestrator composition，不能伪造 grant 或绕过容量账本。composition-close 合同必须经 public bound stream 分别覆盖 permit wait、client acquisition 与已创建真实 Pydantic SDK context/活动 pull：调用 service/router/provider 关闭链后，前两者必须 cancelled/not-started 耐久收口，后者必须 pull 已结束、context 已退出、permit 已释放并以 unknown 围栏收口；所有阶段都要求 close 返回时 invocation 已完成且 client factory 最后关闭。另以并发 public service close 锁定 router 的共享完成事实，第二个 close 不得早于第一个 provider/client close 返回，失败或调用方取消也不得伪装成功。拆分后的测试节点名和行为语义保持不变；started 后 telemetry 取消与 composition close 分属独立小文件，不把取消/partial 文件重新撑过职责门槛。
- 全量恢复夹具修正：`tests/contracts/model_usage_recovery_test_support.py`、`tests/contracts/test_model_usage_approval_outbox_recovery_contracts.py`、`tests/contracts/test_model_usage_local_crash_recovery_contracts.py`。这三处只把旧夹具的 `completed` settlement 补为与现有封闭 validator 一致的 `ModelResponse`，不得放宽生产校验或改变既有恢复断言。
- Live/CI/验收：新建 `scripts/live_model_stream_contract.py`、`scripts/live_model_stream_probe.py`、`scripts/live_model_stream_execution.py` 与薄 CLI `scripts/smoke_live_model_stream.py`；修改 `Makefile`、`.github/workflows/ci.yml`、`.gitlab-ci.yml`、`compliance/ci-jobs.toml`、`scripts/ci_evidence.py`、`tests/contracts/test_ci_pipeline_contracts.py`、`docs/acceptance-matrix.md`。结果 schema/分类、同进程时延探针、受控 composition 与 CLI 分责；setup/runtime/probe/cleanup 的本地异常必须形成安全 artifact，committed/client 时钟建立确定 happens-before。新增 `smoke-live-model-stream` / `ci-smoke-live-model-stream` 独立 target、manifest job 与 artifact，不复用非流式 live artifact 身份；默认 CI job 明确 skip 且零网络。GitHub/GitLab 的 `acceptance-validate` 必须把 stream smoke job 加入 `needs`，下载其 `model-stream-live-smoke/v1` 安全 artifact，并由 pipeline contract 对两边 job 集合、依赖与下载证据路径逐值校验。
- 契约与计划：`Product-Spec.md`、`Product-Spec-CHANGELOG.md`、`API-Contract.md`、`DEV-PLAN.md`、`openspec/changes/controlled-model-streaming/**`、`docs/plans/architecture-evolution-plan.md`、`docs/plans/architecture-evolution-change-matrix.md`；实现完成后按实际证据更新 AC、阶段和 handoff 状态，真实 provider 前置未完整时继续使用 `hosted-unverified`。`Product-Spec-CHANGELOG.md` 只同步本 change 影响的 AC-087 public/internal reader 可见性说明，不把 Phase 18.2 规划纳入实现范围。
- 数据：复用现有表与 migration 0014/0016 之后的列和状态约束；无 schema migration。若 public-seam 红测证明数据库约束无法表达上述占位/取消语义，必须先回到本设计并重新审查，不能在实现中偷偷加迁移。
- 依赖：不升级 Pydantic AI；增加锁定 2.5.0 行为的 adapter contract tests。

## Testing Seams 与验收 producer 映射

| 验收 | 生产 producer | public red/green tests | CI producer |
|---|---|---|---|
| AC-085 | `packages/agent-harness/src/agent_harness/models/providers.py`、`packages/agent-harness/src/agent_harness/models/router.py`、`packages/agent-harness/src/agent_harness/models/_router_current.py`、`packages/agent-harness/src/agent_harness/models/_router_snapshot.py`、`packages/agent-harness/src/agent_harness/models/invocation.py`、`packages/agent-harness/src/agent_harness/models/_invocation_streaming.py`、`packages/agent-harness/src/agent_harness/models/_streaming_contracts.py`、`packages/agent-harness/src/agent_harness/models/_streaming_consumption.py`、`packages/agent-harness/src/agent_harness/models/_streaming_events.py`、`packages/agent-harness/src/agent_harness/models/_streaming_settlement.py`、`packages/agent-harness/src/agent_harness/models/streaming.py`、`packages/agent-harness/src/agent_harness/events/capacity.py`、`packages/agent-harness/src/agent_harness/events/local_capacity.py`、`packages/agent-harness/src/agent_harness/events/sinks/postgresql.py`、`packages/agent-harness/src/agent_harness/storage/stream_evidence_repositories.py` | `tests/contracts/test_controlled_model_streaming_provider_contracts.py`、`tests/contracts/test_controlled_model_streaming_routing_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_success_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_interruption_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_replay_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_guardrail_contracts.py`、`tests/contracts/test_controlled_model_streaming_capacity_contracts.py`、`tests/contracts/test_controlled_model_streaming_postgresql_contracts.py` | `Makefile` 的 `ci-unit-contract` / `ci-integration`；`.github/workflows/ci.yml` 与 `.gitlab-ci.yml` 同名 required jobs；`docs/acceptance-matrix.md` 绑定上述节点 |
| AC-086 | `packages/agent-harness/src/agent_harness/models/router.py`、`packages/agent-harness/src/agent_harness/models/_router_snapshot.py`、`packages/agent-harness/src/agent_harness/models/invocation.py`、`packages/agent-harness/src/agent_harness/models/_invocation_streaming.py`、`packages/agent-harness/src/agent_harness/models/_streaming_contracts.py`、`packages/agent-harness/src/agent_harness/models/_streaming_consumption.py`、`packages/agent-harness/src/agent_harness/models/_streaming_events.py`、`packages/agent-harness/src/agent_harness/models/_streaming_settlement.py`、`packages/agent-harness/src/agent_harness/models/_settlement_contracts.py`、`packages/agent-harness/src/agent_harness/models/_invocation_evidence.py`、`packages/agent-harness/src/agent_harness/models/_invocation_settlement.py`、`packages/agent-harness/src/agent_harness/models/_settlement_publication.py`、`packages/agent-harness/src/agent_harness/models/_settlement_evidence_validation.py`、`packages/agent-harness/src/agent_harness/adapters/models/pydantic_ai.py`、`packages/agent-harness/src/agent_harness/adapters/models/_pydantic_ai_streaming.py`、`packages/agent-harness/src/agent_harness/runtime/_orchestrator_base.py`、`packages/agent-harness/src/agent_harness/events/local_capacity.py`、`packages/agent-harness/src/agent_harness/events/sinks/postgresql.py`、`packages/agent-harness/src/agent_harness/storage/event_capacity_repositories.py`、`packages/agent-harness/src/agent_harness/storage/evidence_repositories.py`、`packages/agent-harness/src/agent_harness/storage/usage_evidence_repositories.py`、`packages/agent-harness/src/agent_harness/storage/_shared_budget_replay_repository.py`、`packages/agent-harness/src/agent_harness/storage/stream_evidence_repositories.py` | `tests/contracts/test_controlled_model_streaming_routing_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_success_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_interruption_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_cancellation_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_started_cancellation_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_composition_close_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_replay_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_guardrail_contracts.py`、`tests/contracts/test_controlled_model_streaming_recovery_contracts.py`、`tests/contracts/test_controlled_model_streaming_postgresql_contracts.py`、`tests/contracts/test_controlled_model_streaming_approval_contracts.py`、`tests/contracts/controlled_real_model_policy_approval_test_support.py` | `Makefile` 的 `ci-unit-contract` / `ci-integration`；`.github/workflows/ci.yml` 与 `.gitlab-ci.yml` 同名 required jobs；`docs/acceptance-matrix.md` 绑定 started/telemetry 取消、真实 Pydantic client/permit 自然 deadline、composition close 与 unknown/recovery 节点 |
| AC-087 | 既有 SSE/CLI committed reader（生产路径不改）与 `packages/agent-harness/src/agent_harness/models/_invocation_streaming.py`、`packages/agent-harness/src/agent_harness/models/_streaming_contracts.py`、`packages/agent-harness/src/agent_harness/models/_streaming_consumption.py`、`packages/agent-harness/src/agent_harness/models/_streaming_events.py`、`packages/agent-harness/src/agent_harness/models/_streaming_settlement.py` 的新 CanonicalEvent producer | `tests/contracts/test_controlled_model_streaming_transport_contracts.py`、`tests/contracts/test_sse_http_openapi_contracts.py`、`tests/contracts/test_cli_event_stream_contracts.py`、`tests/contracts/test_sse_event_reader_local_contracts.py`、`tests/contracts/test_sse_event_reader_postgresql_contracts.py`、`tests/integration/test_sse_http_streaming_contracts.py` | `Makefile` 的 `ci-unit-contract` / `ci-integration`；`.github/workflows/ci.yml` 与 `.gitlab-ci.yml` 同名 required jobs；`docs/acceptance-matrix.md` 绑定 reader isolation/replay 节点 |
| AC-088 | `packages/agent-harness/src/agent_harness/models/streaming.py`、`packages/agent-harness/src/agent_harness/models/_invocation_streaming.py`、`packages/agent-harness/src/agent_harness/models/_streaming_contracts.py`、`packages/agent-harness/src/agent_harness/models/_streaming_consumption.py`、`packages/agent-harness/src/agent_harness/models/_streaming_events.py`、`packages/agent-harness/src/agent_harness/models/_streaming_settlement.py`、`packages/agent-harness/src/agent_harness/adapters/models/fake.py`、`packages/agent-harness/src/agent_harness/adapters/models/pydantic_ai.py`、`packages/agent-harness/src/agent_harness/adapters/models/_pydantic_ai_client.py`、`scripts/live_model_stream_contract.py`、`scripts/live_model_stream_probe.py`、`scripts/live_model_stream_execution.py`、`scripts/smoke_live_model_stream.py` | `tests/contracts/test_controlled_model_streaming_security_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_success_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_interruption_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_replay_contracts.py`、`tests/contracts/test_controlled_model_streaming_runtime_guardrail_contracts.py`、`tests/contracts/test_controlled_model_streaming_live_smoke_contracts.py`、`tests/integration/test_controlled_model_streaming_live_smoke.py`、`tests/contracts/test_ci_pipeline_contracts.py` | 默认 `Makefile` 的 `ci-unit-contract` 只跑 fake clock；独立 `ci-smoke-live-model-stream` 由 `compliance/ci-jobs.toml`、`.github/workflows/ci.yml`、`.gitlab-ci.yml` 共同冻结并产出 `model-stream-live-smoke/v1`；双 CI `acceptance-validate` 显式依赖并下载该 artifact，前置不足为 skipped、不得联网 |

另由 `test_controlled_model_streaming_runtime_success_contracts.py`、`test_controlled_model_streaming_runtime_interruption_contracts.py`、`test_controlled_model_streaming_runtime_replay_contracts.py`、`test_controlled_model_streaming_runtime_guardrail_contracts.py` 与 `test_controlled_model_streaming_approval_contracts.py` 通过 `build_execution_context()` 取得 `BoundModelInvocationService`，覆盖普通 `stream`、匹配 grant 的 `stream_approved`、审批缺失/过期/重放/九个字段不匹配零副作用、稳定 identity、partial/unknown 的 usage/共享预算同事务 needs-review 与 exact replay 零二次调用；这是一条必须先红的生产入口合同，不允许只直调底层 service。

`test_controlled_model_streaming_routing_contracts.py` 必须同时覆盖当前配置规划、冻结快照恢复、结算证据校验与恢复重放：合法 `text_stream` route identity 逐值往返，`text_completion` 回归不变，未知 capability 或篡改 route evidence 关闭失败，且所有副作用前拒绝都保持 provider call count 为零。

live smoke 产出 `model-stream-live-smoke/v1`，并在同一受控进程内协调 local runtime invocation 与事件 client。`provider_first_delta_ms`、`committed_first_delta_ms`、`client_first_delta_ms` 共用首次 provider 迭代前的 monotonic origin；另以 SSE request 为独立 origin 记录 `existing_event_first_frame_ms`，不跨进程比较 monotonic clock。默认 fake clock contract 验证字段为非负 integer milliseconds、成功时 provider <= committed <= client、已有事件首 frame `<1000ms`，以及 hosted-unverified/failed/external-blocked 的封闭 reason code 和四前置状态机。本地 terminal/capacity/shared-budget/publication/policy/guardrail 失败固定为 `failed/contract_failure` 与退出 1；invocation error 通过不进入 artifact 的封闭 `failure_domain=provider|runtime` 传递来源，已观察 response 或 delta 时保留 `provider_called=true`，只有 provider domain 的稳定 provider/network 故障可映射 external-blocked。artifact 只含状态、是否调用、安全 reason code 和时延，不含 prompt、输出文本、endpoint path、header、response identity、failure domain 或原始异常。

## Risks / Trade-offs

- [64 × 4096 bytes 的公共输出硬上限可能拒绝超长响应] → 这是副作用前容量与内存可证明性的代价；调用方应调低模型输出上界或使用非流式 artifact 路径，不能动态扩容。
- [Pydantic AI context 清理不能证明远端停止] → 已开始后的不确定退出默认归类 unknown，保留预算、lease、容量和终态围栏。
- [有状态脱敏增加首字节延迟] → 只保留触发前缀/敏感候选，普通文本按目标 chunk 及时推进；full-result guardrail 明确禁用 speculative delta。
- [65 个预建 outbox 行增加每次调用写放大] → 以固定小上限换取稳定身份、恢复和容量证明；批量 insert，未用行批量 cancel。
- [复用通用 outbox 表可能误伤其他 operation validator] → 新操作种类使用专用 repository 方法，所有共享枚举、terminal pending 与 aggregation 点由合同测试盘点。
- [锁定 SDK 内部事件类发生版本漂移] → 不升级依赖；contract test 对 2.5.0 的 zero-buffer、文本事件过滤和唯一 final 语义建立门禁，升级必须重新提 change。
- [部分 delta 已公开但最终 unknown] → 产品契约允许保留已提交前缀；completed/usage/terminal 缺席明确表示结果未闭合，不能伪造回滚公开文本。

## Migration Plan

1. 先以 public-seam red tests 固定 provider、invocation、事件容量、安全与 transport 行为。
2. 增加新协议和 `MODEL_STREAM` 应用层注册，不修改既有 schema 或 `MODEL_USAGE=2`。
3. 实现 local/SQLite 后，在真实 PostgreSQL migration head 上验证占位、锁、outstanding/high-water 与恢复。
4. 默认 runtime 继续 fake；真实 Pydantic AI 流式能力只有显式 provider 配置和流式 opt-in 才可触达。
5. 回滚时停止启用 `text_stream` route；既有一次性调用不受影响。已经 unknown 的 stream 记录不可删除或自动释放，仍按原版本恢复/人工处理。

## Open Questions

本次进入实现前无未决产品或架构问题。64/65、4096 bytes、默认 1024 bytes、敏感候选默认 512 bytes、双预留、公开 delta/completed、内部 started/usage、无 schema migration 与 unknown 默认均在本 change 冻结；若红测证明其中任一前提不成立，必须先修改契约并重新经过严格校验和独立审查。
