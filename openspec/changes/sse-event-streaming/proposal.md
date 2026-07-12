## Source Links

- Product-Spec.md: REQ-014、RUN-006、AC-038、AC-066 与 SSE NFR
- DEV-PLAN.md: Phase 13.9 `sse-event-streaming`
- API-Contract.md: RUN-003 JSON seam、RUN-006 SSE transport 与 `Last-Event-ID`
- Design artifact: `docs/architecture/agent-harness-runtime-trust-boundaries.*`、`docs/architecture/pydantic-ai-agent-architecture.*`

## Why

当前仓库只有 CanonicalEvent JSON 查询和 SSE frame formatter，没有可连接、续读、鉴权和终止的 HTTP transport。Product P0 要求客户端能以 `Last-Event-ID -> seq` 恢复事件流，并提供首 frame 性能证据。

## What Changes

- 新增 RUN-006 `GET /api/v1/runs/{run_id}/events/stream`，返回 `text/event-stream`。
- 复用 RUN-003 的 CanonicalEvent、租户隔离、public/internal 可见性和 policy 规则。
- `Last-Event-ID` 是唯一续读输入，映射为已消费 seq；RUN-006 不接受 `after_seq`。
- 按 seq 严格递增发送事件，terminal event 后结束；空闲连接发送不携带业务数据的心跳并在断连时停止读取。
- 非法 header 在握手前返回 422 `ApiErrorEnvelope`；握手后异常发送脱敏 `stream.error` frame 后关闭。
- 增加首 frame 延迟固定证据和 OpenAPI/contract/integration/smoke 验收。

## Non-Goals

- 不实现 WebSocket、双向控制、跨 run 聚合 stream 或外部 event broker gateway。
- 不改变 CanonicalEvent schema，不允许 SSE 绕过 RUN-003 的权限和脱敏边界。
- 不实施 Phase 14/15，不发布、不归档。

## Capabilities

### New Capabilities

- `sse-event-streaming`: 定义 RUN-006 transport、续读、可见性、终止、心跳和性能证据。

### Modified Capabilities

- `service-app-shell`: 把 RUN-006 纳入当前 P0 route 与 OpenAPI 精确性契约。

## Impact

- 模板：`app/api/routes/runs.py`、`app/api/sse.py`、app factory/event sink 依赖。
- API：新增只读 SSE route、header 校验、握手前 error envelope 与握手后 `stream.error`。
- 测试：frame/unit、route contract、断线与续读 integration、local/service smoke、首 frame 性能证据。
