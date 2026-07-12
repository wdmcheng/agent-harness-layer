## Context

RUN-003 已提供按 seq 查询 CanonicalEvent，`format_sse_event` 已能格式化单帧，但没有 HTTP streaming lifecycle。新 transport 必须复用现有 event sink、run ownership、policy 和 visibility seam，不能另建事件真相源。

## Goals / Non-Goals

**Goals:** 提供可续读、可鉴权、严格有序、可终止的 RUN-006，并形成首 frame 性能证据。

**Non-Goals:** WebSocket、双向控制、跨 run multiplex、外部 broker gateway、Phase 14/15。

## Decisions

1. **RUN-006 复用 RUN-003 reader，但只公开一个 cursor。** 内部 reader 仍使用 exclusive `after_seq` 参数，HTTP 层只接受 `Last-Event-ID` 并解析为已消费 seq；RUN-006 query 不接受 `after_seq`，避免两个续读真相源。
2. **非法或不属于当前 run 的 header 在握手前走统一 422。** 非十进制、负数、溢出、超过当前 max seq 或不是当前 run 已存在 seq 的 `Last-Event-ID` 返回 422 `validation_error` `ApiErrorEnvelope`；校验复用已授权 run 的 event reader，不新增 400 错误码，也不通过错误差异泄露其他 run 的 seq。
3. **短轮询 event sink 加心跳。** 当前 sink 没有 subscribe protocol，generator 以有界 poll interval 读取并输出注释心跳；这保持 provider-neutral，未来可替换实现而不改 HTTP 契约。
4. **terminal 后自然结束。** 发送可见 terminal event 后关闭；客户端断连立即取消 generator，不继续占用数据库连接。
5. **权限先于 streaming response。** 在返回 200 前完成 run ownership 和 internal-event policy 校验，错误继续使用 JSON `ApiErrorEnvelope`。
6. **握手后错误使用受控 frame。** streaming generator 捕获读取/序列化异常，发送一个不含内部异常或 secret 的 `event: stream.error` frame 后关闭；不能再切换 HTTP status。

## Affected Surfaces

`app/api/routes/runs.py`、`app/api/sse.py`、EventSink reader、OpenAPI/API Contract tests、local/service smoke。无 schema migration。

## Testing Seams

frame formatter、唯一 header cursor parser、async generator、TestClient streaming、可见性/tenant/policy、握手后 stream.error、terminal/heartbeat/disconnect、SQLite/PostgreSQL sink、首 frame 固定计时器。

## Risks / Trade-offs

- [Risk] polling 放大存储压力 → 有界 batch、poll interval、断连取消和 terminal close。
- [Risk] proxy buffering 推迟首 frame → `Cache-Control: no-cache`、`X-Accel-Buffering: no` 与 transport contract test。
- [Risk] internal event 泄漏 → 默认 public filter；`include_internal=true` 复用 RUN-003 policy。

## Migration Plan

先增加 contract/unit red tests，再接 generator 和 route，最后加入 local/service smoke 与性能证据。回滚只移除 RUN-006，不影响 RUN-003 或 CanonicalEvent 数据。

## Open Questions

无阻塞问题；外部 push broker 属于未来部署边界。
