## ADDED Requirements

### Requirement: RUN-006 以 SSE 发送 CanonicalEvent
service app SHALL 提供 `GET /api/v1/runs/{run_id}/events/stream`，成功响应使用 `text/event-stream`。每个业务 frame MUST 由一个对当前调用方可见的 `CanonicalEvent` 生成，`id` 等于十进制 `seq`，`event` 等于稳定 `event_type`，`data` 是脱敏后的可见 event envelope。

#### Scenario: 按 seq 发送公开事件
- **WHEN** 已授权调用方连接存在事件的 run stream
- **THEN** API 按 seq 严格递增发送 SSE frame，默认过滤 internal 事件且不重复已消费 seq

#### Scenario: Internal 事件需要额外权限
- **WHEN** 调用方请求 `include_internal=true`
- **THEN** 系统复用 RUN-003 的 internal-event policy；未授权时在建立 stream 前返回稳定 403 envelope

### Requirement: Last-Event-ID 是唯一公开续读输入
RUN-006 SHALL 把有效 `Last-Event-ID` 解释为内部 exclusive event seq。除 0 以外，该值 MUST 对应当前已授权 run 的既有 CanonicalEvent seq；HTTP query MUST NOT 接受 `after_seq`。非十进制、负数、溢出、超过当前 max seq、run 内不存在或只属于其他 run 的 header MUST 在建立 stream 前返回 422 `validation_error` `ApiErrorEnvelope`，且错误不得泄露其他 run 的 seq 状态。

#### Scenario: Last-Event-ID 续读
- **WHEN** 客户端以 `Last-Event-ID: 7` 重连
- **THEN** 首个业务 frame 的 seq 大于 7，已消费事件不重放

#### Scenario: after_seq 不是 RUN-006 参数
- **WHEN** 客户端只提供 `after_seq` query 或把它与 `Last-Event-ID` 同时提交
- **THEN** API 在发送 SSE headers 前返回 422 `validation_error` `ApiErrorEnvelope`，不接受或忽略该第二 cursor

#### Scenario: 非法 Last-Event-ID 握手前失败
- **WHEN** `Last-Event-ID` 非十进制、为负数或超出受支持整数范围
- **THEN** API 在发送 SSE headers 前返回 422 `validation_error` `ApiErrorEnvelope`

#### Scenario: Last-Event-ID 不属于当前 run
- **WHEN** `Last-Event-ID` 超过当前 run max seq、命中 run 内不存在的 seq 或只存在于其他 run
- **THEN** API 在发送 SSE headers 前返回相同的 422 `validation_error` `ApiErrorEnvelope`，不建立 stream且不泄露其他 run 的事件信息

### Requirement: Stream 生命周期可终止且无业务空帧
RUN-006 SHALL 在空闲时只发送 SSE comment heartbeat，不伪造 CanonicalEvent；发送调用方可见的 terminal event 后 MUST 结束连接。客户端断连 MUST 停止后续读取和 heartbeat。

#### Scenario: Terminal event 关闭 stream
- **WHEN** generator 发送 run 对当前调用方可见的 terminal event
- **THEN** 该 frame 是最后一个业务 frame，response 随后结束

#### Scenario: 空闲连接发送 comment heartbeat
- **WHEN** poll interval 内没有新事件且 run 尚未 terminal
- **THEN** server 可发送不含 `id/event/data` 的 comment heartbeat，且不会推进 cursor

#### Scenario: 客户端断连取消读取
- **WHEN** request disconnect signal 变为 true
- **THEN** generator 停止 poll 和输出，不保留后台读取任务

#### Scenario: 握手后错误发送脱敏终止帧
- **WHEN** response 已建立后 event sink 读取或 frame 序列化失败
- **THEN** server 发送一个 `event: stream.error` frame，data 只含稳定 code/request/trace 摘要且不含内部异常或 secret，随后关闭连接

### Requirement: 首 frame 性能有固定证据
local fake profile 的 RUN-006 首个 frame 延迟 SHALL 以固定数据、固定事件数和明确计时边界验证；P95 MUST 小于 Product Spec 的 1 秒上限。该证据 MUST 与功能测试分开报告，不能由 formatter unit test 替代。

#### Scenario: 固定负载首 frame 达标
- **WHEN** 性能测试在预置事件的 local fake run 上重复连接 RUN-006
- **THEN** 报告样本数、计时边界与 P95，且 P95 小于 1000ms
