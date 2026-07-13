## Source Links

- Product-Spec.md: REQ-014 的 SSE/CLI stream adapter、AC-038、AC-066 与 SSE NFR；RUN-006/CLI-EVT-001 编号与 transport 细节来自 API-Contract.md
- DEV-PLAN.md: Phase 13.9 `sse-event-streaming`
- API-Contract.md: RUN-003 JSON seam、RUN-006 SSE transport 与 `Last-Event-ID`
- Design artifact: `docs/architecture/agent-harness-runtime-trust-boundaries.*`、`docs/architecture/pydantic-ai-agent-architecture.*`

## Why

当前仓库只有 CanonicalEvent JSON 查询、SSE frame formatter 和 run 完成后的 CLI 摘要，没有可连接、续读、鉴权和终止的 HTTP transport，也没有逐条输出 CanonicalEvent 的 CLI stream adapter。Product P0 要求 HTTP 客户端能以 `Last-Event-ID -> seq` 恢复事件流，CLI 能通过同一授权 reader 逐条消费 canonical bytes，并提供首 frame 性能证据。

## What Changes

- 新增 RUN-006 `GET /api/v1/runs/{run_id}/events/stream`，返回 `text/event-stream`。
- 新增 `CLI-EVT-001` `agent-harness events stream <run_id>`；CLI 用专属 `--after-seq` 作为 exclusive cursor，并逐条输出 `canonical_event_bytes()` NDJSON，不使用 SSE framing。
- 复用 RUN-003 的 CanonicalEvent、租户隔离、public/internal 可见性和 policy 规则。
- `Last-Event-ID` 是唯一续读输入，映射为已消费 seq；RUN-006 不接受 `after_seq`。
- 按 seq 严格递增发送事件；三种 run terminal 必须为 public，EventBus/sink 拒绝 non-public terminal，默认 reader 发送 terminal 后结束；空闲连接发送不携带业务数据的心跳并在断连时停止读取。
- 非法、隐藏或不属于当前授权视图的 header 在握手前返回同一 422 `ApiErrorEnvelope`，不形成 event oracle；握手后异常发送脱敏 `event: stream.error` frame 后关闭。
- EventSink 以 `100` event / 公共 `canonical_event_bytes()` 计算的 `1048576` bytes 受限分页，generator 同时只持有一个 page并等待 ASGI send，不为慢客户端预取；P0 不增加 event cleanup/TTL/retention job。
- 读取复用 `0014` outbox 的 run/operation capacity reservation，容量基数使用 `highest_persisted_seq` 并与预约消费同事务推进；正常 CanonicalEvent envelope 按同一 canonical serializer 的写入上限为 `65536` bytes。legacy/direct-write 单条超过 `65536` bytes 时发送一个 `event: stream.error` frame，且 `data.code=stream.event_state_invalid`，随后 fail closed并关闭，不返回空页忙循环；`1048576` bytes 只约束已通过单条合法性校验的 page 累计值。
- 增加首 frame 延迟固定证据和 OpenAPI/contract/integration/smoke 验收。

## Non-Goals

- 不实现 WebSocket、双向控制、跨 run 聚合 stream 或外部 event broker gateway。
- 不改变 CanonicalEvent schema，不允许 SSE 绕过 RUN-003 的权限和脱敏边界。
- 不实施 Phase 14/15，不发布、不归档。

## Capabilities

### New Capabilities

- `sse-event-streaming`: 定义 RUN-006 与 CLI-EVT-001 transport、续读、可见性、终止、背压和性能证据。

### Modified Capabilities

- `service-app-shell`: 把 RUN-006 纳入当前 P0 route 与 OpenAPI 精确性契约。
- `canonical-events-artifacts`: 复用 `0014` event capacity reservation与 envelope 硬上限，增加有界 reader/legacy invalid-state 读取语义，不改变公开 CanonicalEvent 字段。

## Impact

- 核心/模板：`agent_harness/cli.py`、`app/api/routes/runs.py`、`app/api/sse.py`、app factory/EventBus/event sink 依赖。
- API：新增只读 SSE route、header 校验、握手前 error envelope 与握手后 `stream.error`。
- 测试：frame/unit、HTTP route/OpenAPI、CLI NDJSON/cursor/visibility、断线与续读 integration、容量预约消费、event/byte 分页、慢 send/stdout 无预取、legacy 超限 fail-closed、local/service smoke、首 frame 性能证据。
