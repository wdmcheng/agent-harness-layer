## Context

RUN-003 已提供按 seq 查询 CanonicalEvent，`format_sse_event` 已能格式化单帧，但没有 HTTP streaming lifecycle。新 transport 必须复用现有 event sink、run ownership、policy 和 visibility seam，不能另建事件真相源。

## Goals / Non-Goals

**Goals:** 提供可续读、可鉴权、严格有序、可终止的 RUN-006 与 CLI-EVT-001，并形成首 frame 性能证据。

**Non-Goals:** WebSocket、双向控制、跨 run multiplex、外部 broker gateway、Phase 14/15。

## Decisions

1. **RUN-006 复用 RUN-003 reader，但只公开一个 cursor。** 内部 reader 仍使用 exclusive `after_seq` 参数，HTTP 层只接受 `Last-Event-ID` 并解析为已消费 seq；RUN-006 query 不接受 `after_seq`，避免两个续读真相源。
2. **非法或当前调用方不可见的 header 在握手前走统一 422。** `Last-Event-ID: 0` 是合法初始 cursor，不要求命中既有 seq；其他值必须命中当前已授权 run 且在本次 `include_internal` policy 下对调用方可见的既有 CanonicalEvent。非十进制、负数、溢出、隐藏 seq、当前 run 空洞、超过可见范围或只属于其他 run 的非零值统一返回 422 `validation_error` `ApiErrorEnvelope`；校验复用带 tenant/identity/visibility 的授权 reader，不以 max seq、存在性或错误差异形成 internal/其他 run 事件 oracle。
3. **短轮询 event sink 加心跳。** 当前 sink 没有 subscribe protocol，generator 以有界 poll interval 读取并输出注释心跳；这保持 provider-neutral，未来可替换实现而不改 HTTP 契约。
4. **public run terminal 后自然结束。** 三种 run terminal 必须显式为 `visibility=public`，EventBus 与 local/PostgreSQL sink 在持久化前拒绝 non-public terminal，确保默认 reader 必然能观察最终结算信号。发送 run terminal event 后关闭；若合法 cursor 已经消费 terminal marker，授权 reader 在握手后立即 EOF，不重放 terminal、不发送 heartbeat。客户端断连立即取消 generator，不继续占用数据库连接。
5. **权限先于 streaming response。** 在返回 200 前完成 run ownership 和 internal-event policy 校验，错误继续使用 JSON `ApiErrorEnvelope`。
6. **握手后错误使用受控 frame。** streaming generator 捕获读取/序列化异常，发送一个不含内部异常或 secret 的 `event: stream.error` frame 后关闭；不能再切换 HTTP status。
7. **reader 用固定上限分页并把 ASGI send 当作背压边界。** EventSink 以 exclusive `after_seq` 读取时先拒绝任何 canonical envelope 超过 `65536` bytes 的非法单条 row，再把合法 event 纳入每页最多 `100` 个、合计最多 `1048576` bytes 的 page budget；generator 同时只保留一个 page，逐 frame yield/send 完成后才推进，不预取第二页。替代方案是沿用当前 `read()` 一次返回全部未读事件；拒绝，因为慢客户端和长 run 会把未读集合整体搬进内存。
8. **P0 保留 run event，不在本 transport change 中引入 retention。** 当前 CanonicalEvent 是 durable trace/eval/audit evidence，仓库没有 event cleanup seam；本 change 不增加 TTL、清理 job 或 expired-cursor 分支。未来如需 retention，必须以独立 behavior change 定义 API、安全、审计与 cursor 过期语义，不能把曾合法 cursor 静默降级为普通不存在。
9. **写端消费 `0014` durable capacity reservation。** `canonical_events.seq` 与公开 cursor 共用 `1..2147483647`；run 创建时预约 terminal，provider/tool/approval/delegation operation 在副作用前预约其最大 prerequisite event 数。`highest_persisted_seq + outstanding + terminal` 在 run 锁/CAS 内不得越界，预约消费、event 插入和 high-water mark 推进同事务；seq 空洞不得按 row count 低估。容量不足在外部副作用前以 `event.sequence_exhausted` 拒绝，未知结果保持预约并阻止 terminal。非法历史容量状态以 `event.sequence_state_invalid` 拒绝新写入。SQLite/local 和 PostgreSQL 使用同一语义。
10. **正常 envelope 写入上限让 byte page 可推进。** `model-usage-evidence` 提供唯一 `canonical_event_bytes()`，对 `to_payload()` 使用 UTF-8、非 ASCII 转义关闭、排序键、紧凑分隔符并拒绝 NaN；写入检查、legacy 校验与 SSE page 均复用它。完整 CanonicalEvent envelope 硬限制为 `65536` bytes；payload 先 artifact 化，仍超限则写前拒绝。SSE reader 若遇到历史/direct-write 单条超过 `65536` bytes 的 row，不返回空 page或跳过它，而是抛出 `event.envelope_state_invalid`；generator 发一个脱敏 `event: stream.error` 后关闭，单个连接不重复读取。`1048576` bytes 只作为多条合法 event 的 page 累计上限。
11. **CLI adapter 复用 reader，但不是 SSE。** `agent-harness events stream <run_id>` 以 `--after-seq` 暴露内部 exclusive cursor，逐条把 `canonical_event_bytes()` 解码为 NDJSON；默认只读 public，`--include-internal` 复用同一额外权限。合法 cursor 已消费 terminal 时空输出成功退出；等待新 event 时不打印 heartbeat；terminal 后退出。stdout 背压、Ctrl-C 和错误都停止读取，不能预取第二页或把日志/合成错误事件混入 stdout。HTTP 仍只接受 `Last-Event-ID`，CLI option 不得进入 RUN-006 query。

## Affected Surfaces

`agent_harness/cli.py`、`app/api/routes/runs.py`、`app/api/sse.py`、`events/serialization.py::canonical_event_bytes()`、EventBus、EventSink reader/local/PostgreSQL sink、CLI/OpenAPI/API Contract tests、local/service smoke。无 schema migration。

## Testing Seams

frame formatter、唯一 HTTP header cursor parser（含合法初始值 `0`）、CLI `--after-seq` parser、async generator、TestClient/CLI runner streaming、HTTP/CLI 可见/隐藏/其他 run cursor 的统一过滤、tenant/policy、握手后 stream.error、CLI stderr、三种 public terminal 与 non-public terminal 拒绝、terminal 发送后关闭、cursor 已消费 terminal 时立即 EOF、heartbeat/disconnect/Ctrl-C、NDJSON canonical bytes、公共 serializer 的 Unicode/键顺序/NaN 与 `65536/65537/1048576/1048577` 精确边界、`100` event/`1048576` bytes 合法 page、单条 legacy 超限 fail closed、慢 ASGI send/stdout 无预取、P0 无 retention cleanup、SQLite/PostgreSQL high-water capacity reservation 与非法历史状态、首 frame 固定计时器。

## Risks / Trade-offs

- [Risk] polling 放大存储压力或慢客户端堆积内存 → 固定 `100` event/`1048576` bytes page、最多一个 in-flight page、等待 ASGI send、poll interval、断连取消和 terminal close。
- [Risk] proxy buffering 推迟首 frame → `Cache-Control: no-cache`、`X-Accel-Buffering: no` 与 transport contract test。
- [Risk] internal event 泄漏 → 默认 public filter；`include_internal=true` 复用 RUN-003 policy。
- [Risk] cursor 命中结果泄漏 hidden/internal seq → 只对本次调用方可见事件做 membership 校验；隐藏、空洞、不存在与其他 run 统一 422。

## Migration Plan

先增加 contract/unit red tests，再接共享 reader、HTTP generator/route 与 CLI adapter，最后加入 local/service smoke 与性能证据。回滚只移除 RUN-006/CLI-EVT-001，不影响 RUN-003 或 CanonicalEvent 数据。

## Open Questions

无阻塞问题；外部 push broker 与 event retention 都属于未来独立行为边界。
