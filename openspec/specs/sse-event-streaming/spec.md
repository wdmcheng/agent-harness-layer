# sse-event-streaming Specification

## Purpose
定义 RUN-006 SSE、`Last-Event-ID` 恢复、CLI 事件流、统一有界 reader 与首 frame 性能边界，确保 local 与 service profile 使用同一 `CanonicalEvent` 真相源。
## Requirements
### Requirement: RUN-006 以 SSE 发送 CanonicalEvent
service app SHALL 提供 `GET /api/v1/runs/{run_id}/events/stream`，成功响应使用 `text/event-stream`。每个业务 frame MUST 由一个对当前调用方可见的 `CanonicalEvent` 生成，`id` 等于十进制 `seq`，`event` 等于稳定 `event_type`，`data` 是脱敏后的可见 event envelope。

#### Scenario: 按 seq 发送公开事件
- **WHEN** 已授权调用方连接存在事件的 run stream
- **THEN** API 按 seq 严格递增发送 SSE frame，默认过滤 internal 事件且不重复已消费 seq

#### Scenario: Internal 事件需要额外权限
- **WHEN** 调用方请求 `include_internal=true`
- **THEN** 系统复用 RUN-003 的 internal-event policy；未授权时在建立 stream 前返回稳定 403 envelope

### Requirement: Last-Event-ID 是唯一公开续读输入
RUN-006 SHALL 把有效 `Last-Event-ID` 解释为内部 exclusive event seq，公开十进制整数范围固定为 `0..2147483647`，与 PostgreSQL `canonical_events.seq` 的 `Integer` 上限一致；OpenAPI header schema MUST 声明相同 minimum/maximum。除 0 以外，该值 MUST 对应当前已授权 run 且在本次 `include_internal` policy 下对调用方可见的既有 CanonicalEvent seq；HTTP query MUST NOT 接受 `after_seq`。非十进制、负数、超过 `2147483647`、超过可见范围、run 内不存在、命中隐藏 event或只属于其他 run 的 header MUST 在建立 stream 前返回同一 422 `validation_error` `ApiErrorEnvelope`，且错误不得泄露 internal/其他 run 的 seq 状态。

#### Scenario: Last-Event-ID 续读
- **WHEN** 客户端以 `Last-Event-ID: 7` 重连
- **THEN** 首个业务 frame 的 seq 大于 7，已消费事件不重放

#### Scenario: after_seq 不是 RUN-006 参数
- **WHEN** 客户端只提供 `after_seq` query 或把它与 `Last-Event-ID` 同时提交
- **THEN** API 在发送 SSE headers 前返回 422 `validation_error` `ApiErrorEnvelope`，不接受或忽略该第二 cursor

#### Scenario: 非法 Last-Event-ID 握手前失败
- **WHEN** `Last-Event-ID` 非十进制、为负数或大于 `2147483647`
- **THEN** API 在发送 SSE headers 前返回 422 `validation_error` `ApiErrorEnvelope`

#### Scenario: Last-Event-ID 对当前调用方不可见
- **WHEN** `Last-Event-ID` 超过可见范围、命中当前 run 的隐藏 seq、命中 run 内不存在的 seq 或只存在于其他 run
- **THEN** API 在发送 SSE headers 前返回相同的 422 `validation_error` `ApiErrorEnvelope`，不建立 stream且不泄露 internal/其他 run 的事件信息；只有通过 `include_internal=true` policy 的调用方才可使用对应 internal seq

### Requirement: Stream 生命周期可终止且无业务空帧
RUN-006 SHALL 在空闲时只发送 SSE comment heartbeat，不伪造 CanonicalEvent；`run.completed`、`run.failed`、`run.cancelled` 的 `visibility` MUST 为 `public`，因此默认 public reader MUST 能观察并发送 run terminal event，随后结束连接。若合法 cursor 已经消费当前 run 的 terminal marker，stream MUST 在握手成功后立即 EOF，不重放 terminal、不发送 heartbeat。客户端断连 MUST 停止后续读取和 heartbeat。

上游 EventBus/local/PostgreSQL sink MUST 在持久化、seq 消耗和 fan-out 前拒绝 `terminal=true` 且 `visibility!=public`，并保证 terminal 是该 run 的最后一条 CanonicalEvent、拒绝任何后续业务事件，使默认 RUN-003、RUN-006 与 CLI reader 都以同一 public terminal 收口且 EOF 不会遗漏晚到 usage 或其他 evidence。

#### Scenario: Terminal event 关闭 stream
- **WHEN** generator 发送 run 对当前调用方可见的 terminal event
- **THEN** 该 frame 是最后一个业务 frame，response 随后结束

#### Scenario: Non-public terminal 在写入边界拒绝
- **WHEN** runtime、outbox 或恢复路径尝试写入 `terminal=true` 且 `visibility!=public` 的 run terminal
- **THEN** EventBus 与 local/PostgreSQL sink 在持久化、seq 消耗和 fan-out 前拒绝；RUN-003、RUN-006 与 CLI 不得进入缺少可见 terminal 的无限轮询状态

#### Scenario: Cursor 已消费 terminal 时立即结束
- **WHEN** 合法 `Last-Event-ID` 等于当前 run 已持久化的 terminal marker seq，因而 exclusive reader 没有新 event
- **THEN** server 完成授权和握手后立即 EOF，不重放 terminal frame，也不进入 heartbeat 循环

#### Scenario: 空闲连接发送 comment heartbeat
- **WHEN** poll interval 内没有新事件且 run 尚未 terminal
- **THEN** server 可发送不含 `id/event/data` 的 comment heartbeat，且不会推进 cursor

#### Scenario: 客户端断连取消读取
- **WHEN** request disconnect signal 变为 true
- **THEN** generator 停止 poll 和输出，不保留后台读取任务

#### Scenario: 握手后错误发送脱敏终止帧
- **WHEN** response 已建立后 event sink 读取或 frame 序列化失败
- **THEN** server 发送一个 `event: stream.error` frame，data 只含稳定 code/request/trace 摘要且不含内部异常或 secret，随后关闭连接

### Requirement: SSE reader 有界且不隐式清理 durable event
RUN-006 使用的 EventSink reader SHALL 按 exclusive `after_seq` 受限分页。每条 row MUST 先通过公共 `canonical_event_bytes() <= 65536` 的单条合法性校验，再纳入默认每页最多 `100` 个 CanonicalEvent、合法 envelope 合计最多 `1048576` bytes 的 page budget；该 serializer 对 `CanonicalEvent.to_payload()` 使用 UTF-8、`ensure_ascii=false`、排序键、紧凑分隔符与 `allow_nan=false`，JSONL 换行和 SSE frame 开销不计入。达到 event 或 page bytes 任一上限后 MUST 以最后已发送 seq 继续下一页。Generator 同时 MUST 最多持有一个 page，逐 frame 等待 ASGI send 完成后才继续，不得在慢客户端尚未消费当前 frame/page 时预取下一页；disconnect 或 send cancellation MUST 立即停止后续读取和 heartbeat。

正常 event 已受 `65536` bytes envelope 写入上限保护。若 reader 遇到历史或 direct-write 的单条 envelope 超过 `65536` bytes，MUST 抛出稳定 `event.envelope_state_invalid` 而不是返回空 page、计入 `1048576` bytes page budget 或跳过 seq；握手后的 RUN-006 MUST 只发送一个 `event: stream.error` frame，且 `data.code=stream.event_state_invalid`，随后关闭，本连接不得再次读取同一 row形成忙循环。

P0 SHALL NOT 新增 CanonicalEvent cleanup、TTL 或 retention job。只要 run 仍存在，其 event evidence MUST 保留，曾经合法的非零 `Last-Event-ID` 不得因本 transport change 的后台行为变成 expired cursor。未来 retention 属于独立行为变更，必须先定义 expired-cursor API、安全和 evidence 语义。

#### Scenario: 慢客户端不触发无界预取
- **WHEN** transport 的 ASGI send 在一个业务 frame 上阻塞
- **THEN** generator 不读取下一页且内存中最多保留当前受限 page；send 被取消或客户端断开后不再调用 EventSink reader

#### Scenario: Event 与 byte 上限都能推进 cursor
- **WHEN** 未读事件超过 `100` 个或单页序列化 envelope 合计将超过 `1048576` bytes
- **THEN** reader 只返回上限内的严格递增 page，generator 发完后以最后已发送 seq 续读，不丢失、不重复 event

#### Scenario: Byte page 使用统一 canonical serializer
- **WHEN** envelope 包含中文、转义字符、不同键插入顺序、恰好 byte 边界或 NaN
- **THEN** local/PostgreSQL reader 与 frame formatter 复用相同 canonical bytes；键顺序不改变计数，单条 `65536` bytes 合法、`65537` bytes fail closed，合法 events 合计 `1048576` bytes 可组成 page、`1048577` bytes 在完整 event 边界续到下一页，NaN 在写入或 legacy 校验阶段 fail closed

#### Scenario: 单条 legacy 超限 event fail closed
- **WHEN** 下一条 legacy/direct-write CanonicalEvent 的序列化 envelope 单独超过 `65536` bytes
- **THEN** reader 以内部 `event.envelope_state_invalid` 失败；已建立的 stream 发送一个脱敏 `event: stream.error` frame 且 `data.code=stream.event_state_invalid`，随后关闭且不再次读取该 row，不返回空 page、不跳过 seq

#### Scenario: P0 不产生过期 cursor
- **WHEN** 客户端以过去已可见的非零 `Last-Event-ID` 重连且对应 run 仍存在
- **THEN** 本 transport change 没有后台清理会删除该 event；membership 校验仍可确定该 cursor，未来 retention 实现不得在本 change 内暗中加入

### Requirement: CLI stream adapter 复用 canonical reader
系统 SHALL 提供 `agent-harness events stream <run_id>`。CLI MUST 复用 RUN-003/RUN-006 的 tenant、identity、event visibility、exclusive reader、page limit、single-envelope validation、canonical serializer 和 terminal 语义，不得读取第二套 event 状态或经由 SSE formatter。`--after-seq` 是 CLI 专属 `0..2147483647` exclusive cursor，默认 `0`；非零值 MUST 命中当前身份与 `--include-internal` 权限下可见的既有 event，且该 option MUST NOT 进入 RUN-006 query。stdout MUST 每个 event 恰好输出一行 `canonical_event_bytes(event).decode('utf-8')`，不得混入 heartbeat、日志、状态提示或伪造错误 event。

#### Scenario: CLI 按 canonical NDJSON 逐条续读
- **WHEN** 调用方执行 `agent-harness events stream <run_id> --after-seq <n>` 且存在后续可见 event
- **THEN** stdout 严格按 seq 递增逐行输出 `seq>n` 的 canonical NDJSON，与 RUN-006 对同一身份可见的 event envelope 逐值一致；terminal 输出后退出

#### Scenario: CLI cursor 与可见性不形成 oracle
- **WHEN** `--after-seq` 非法、越界、命中隐藏/其他 run/空洞 event，或未授权调用方请求 `--include-internal`
- **THEN** CLI 以相同稳定脱敏错误写 stderr 并非零退出，stdout 为空，不泄漏 event 是否存在；HTTP RUN-006 仍不接受 `after_seq` query

#### Scenario: CLI 空闲与中断不伪造 event
- **WHEN** run 尚未 terminal 且暂时没有新 event，stdout 阻塞，或调用方 Ctrl-C
- **THEN** CLI 有界等待且不输出 heartbeat/提示/合成 CanonicalEvent，不预取第二页；中断立即停止 reader且不写业务 evidence

#### Scenario: CLI 已消费 terminal 时成功空退出
- **WHEN** 合法 `--after-seq` 已等于当前 run 对调用方可见的 terminal seq
- **THEN** CLI 成功且 stdout 为空地退出，不重放 terminal、不等待 heartbeat

### Requirement: 首 frame 性能有固定证据
local fake profile 的 RUN-006 首个 frame 延迟 SHALL 以固定数据、固定事件数和明确计时边界验证；P95 MUST 小于 Product Spec 的 1 秒上限。该证据 MUST 与功能测试分开报告，不能由 formatter unit test 替代。

#### Scenario: 固定负载首 frame 达标
- **WHEN** 性能测试在预置事件的 local fake run 上重复连接 RUN-006
- **THEN** 报告样本数、计时边界与 P95，且 P95 小于 1000ms

### Requirement: 模型增量只从已提交事件日志续读
SSE `Last-Event-ID` 与 CLI `--after-seq` SHALL 通过既有 run-scoped committed-event reader 读取 `model.output.delta` 和 `model.output.completed`。两种传输 MUST 使用 CanonicalEvent `seq` 作为唯一续读位置，不得保存或接受 provider cursor，不得因重连重新调用 provider。默认公开读取器 MUST 返回 public delta、completed 和 run terminal，并继续隐藏内部 started 与 usage；获得既有内部权限的读取路径可以查看内部事件。

#### Scenario: SSE 在 delta 中途重连
- **WHEN** 客户端已经收到 seq=N 的 delta 后用 `Last-Event-ID: N` 重连
- **THEN** SSE 只返回 seq>N 的已提交事件且保持原顺序
- **AND** provider 调用次数不增加，已收 delta 不重复

#### Scenario: CLI 从 completed 前续读
- **WHEN** CLI 以 `--after-seq N` 读取包含后续 delta、completed 与 terminal 的运行
- **THEN** CLI 与 SSE 使用同一 reader 语义返回 seq>N 的已提交事件
- **AND** 不使用 HTTP 外的 provider-specific 恢复路径

### Requirement: 读取端断开不控制供应商执行
SSE 或 CLI 消费者的取消、超时、慢读取与断开 SHALL 只结束该 reader。传输层 MUST NOT 持有 provider stream 对象、向 invocation 发送取消、释放 provider lease、修改 stream outbox 或触发重新执行。调用执行与事件读取通过持久化 CanonicalEvent 解耦。

#### Scenario: SSE 客户端提前断开
- **WHEN** SSE 客户端在收到首条 delta 后断开
- **THEN** 该 SSE reader 被关闭，provider invocation 继续由原执行 owner 管理
- **AND** 后续安全 delta 和 completed 仍可持久化并由新 reader 续读

#### Scenario: 慢 reader 不形成 provider 内存背压
- **WHEN** 某个 SSE 或 CLI reader 长时间不消费
- **THEN** 只受既有分页和轮询边界限制该 reader 的资源
- **AND** provider invocation 不为该 reader 建立无界队列或保留未提交 SDK 事件
