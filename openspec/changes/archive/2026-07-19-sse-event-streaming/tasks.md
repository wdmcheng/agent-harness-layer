## 1. 红灯合同与 Cursor

- [x] 1.1 为 RUN-006 OpenAPI、`text/event-stream`、frame shape、默认可见性和稳定错误增加 red contract tests，固定 `Last-Event-ID` header schema 的 `minimum=0`、`maximum=2147483647`
- [x] 1.2 实现并测试唯一 `Last-Event-ID` 解析与当前调用方可见 seq membership 校验，覆盖缺失、合法初始值 `0`、合法可见 seq、等于 terminal seq 的立即 EOF、`terminal_seq + 1`、未授权 internal seq、已授权 `include_internal=true` seq、非十进制、负数、大于 `2147483647`、超过可见范围、run 内空洞和属于其他 run 的 seq；断言 `terminal_seq + 1`、不可见/非法 cursor 与任意 `after_seq` query 在握手前返回同一 422，绝不成为第二 cursor或泄露 internal/其他 run
- [x] 1.3 增加 local/PostgreSQL EventBus/sink red contracts：消费 `0014` run/operation capacity reservation，证明 `highest_persisted_seq + outstanding + terminal <= 2147483647`，且预约消费、event 插入和 high-water mark 推进同事务；容量不足的 operation 在 provider/tool/approval/delegation 副作用前以 `event.sequence_exhausted` 拒绝，未知结果保持预约并阻止 terminal，非法历史容量状态以 `event.sequence_state_invalid` 零变更拒绝。覆盖 `{1, 2147483646}` 稀疏高 seq、并发预约/分配、terminal-last、无部分写入与 cursor 最大值

## 2. Streaming 生命周期

- [x] 2.1 扩展 local/PostgreSQL EventSink reader 的 exclusive `after_seq` 受限分页：先以公共 `canonical_event_bytes()` 拒绝单条超过 `65536` bytes 的非法 row，再把合法 event 纳入默认每页最多 `100` 个、合计最多 `1048576` bytes 的 page budget；实现 async generator 的 seq 递增、public filter、comment heartbeat 和有界 poll，同时最多保留一个 page。覆盖中文/转义字符、键插入顺序、NaN 拒绝以及 `65536/65537/1048576/1048577` 精确边界
- [x] 2.2 实现 run terminal close、合法 cursor 已消费 terminal marker 时握手后立即 EOF，以及 request disconnect 取消；固定 `run.completed|run.failed|run.cancelled` 必须为 public，证明 EventBus 与 local/PostgreSQL sink 对 non-public terminal 零持久化/零 seq 消耗，默认 reader 必然发送 terminal 后关闭，已消费 terminal 不重放、不 heartbeat，断连后没有后台读取或继续输出
- [x] 2.3 在建立 stream 前复用 run ownership、tenant 和 internal-event policy，并保持错误为 JSON `ApiErrorEnvelope`
- [x] 2.4 实现握手后读取/序列化异常到单个脱敏 `event: stream.error` frame 后关闭，证明不泄漏内部异常、provider payload 或 secret
- [x] 2.5 用可控 ASGI send 与 reader spy 证明慢客户端未消费当前 frame/page 时不预取下一页，send cancellation/disconnect 后零后台读取；覆盖 event/byte 两种分页边界、不丢失、不重复
- [x] 2.5a 覆盖单条 legacy/direct-write envelope 超过 `65536` bytes：reader 抛 `event.envelope_state_invalid`，stream 只发送一个 `event: stream.error` 且 `data.code=stream.event_state_invalid` 后关闭，不返回空 page、不跳过 seq、同一连接不重复读取；`1048576` 仅作为合法 page 累计上限
- [x] 2.6 固定 P0 无 CanonicalEvent cleanup/TTL/retention job；contract test 证明 run 存续时历史可见 cursor 仍可 membership 校验，并保护 future retention 必须另建 change 定义 expired-cursor 语义

## 3. HTTP Route、CLI 与 OpenAPI

- [x] 3.1 注册 RUN-006 route 和 streaming headers，保持 RUN-003 行为及 CanonicalEvent schema 不变
- [x] 3.2 增加缺失/多余 status、参数、media type 的双向 OpenAPI drift test
- [x] 3.3 增加 RUN-003、RUN-006 与 `agent-harness events stream <run_id>` 联合 red contracts，固定三条入口都能观察 public terminal 并在 terminal 后收口；固定 CLI 专属 `--after-seq`、默认/public 与授权 internal visibility、canonical NDJSON stdout、stderr/exit、terminal/已消费 terminal、慢 stdout/Ctrl-C 和 SQLite/PostgreSQL 行为，并证明该 option 不进入 RUN-006 query
- [x] 3.4 实现 CLI-EVT-001，只复用授权 EventSink reader、page/canonical serializer/terminal seam；空闲不输出 heartbeat，terminal 后退出，错误或中断不预取、不写业务 evidence、不把提示混入 stdout

## 4. 验收证据

- [x] 4.1 增加固定数据/样本/计时边界的首 frame P95 测试并证明小于 1000ms
- [x] 4.2 运行定向 unit/contract/integration、local smoke 与真实 PostgreSQL service smoke，分开报告离线和 service 证据
- [x] 4.3 完成该 capability 的 3 个 fresh code-reviewer Stage 1/2 PASS，保持 change 为 ready-to-archive 且不归档
