## 1. 红灯合同与 Cursor

- [ ] 1.1 为 RUN-006 OpenAPI、`text/event-stream`、frame shape、默认可见性和稳定错误增加 red contract tests
- [ ] 1.2 实现并测试唯一 `Last-Event-ID` 解析与当前 run seq 归属校验，覆盖缺失、合法、非十进制、负数、溢出、超过 max seq、run 内空洞和属于其他 run 的 seq；断言非法 cursor 与任意 `after_seq` query 在握手前返回 422，绝不成为第二 cursor或泄露其他 run

## 2. Streaming 生命周期

- [ ] 2.1 实现复用 EventSink 的 async generator，保证 seq 递增、public filter、comment heartbeat 和有界 poll
- [ ] 2.2 实现 terminal close 与 request disconnect 取消，证明断连后没有后台读取或继续输出
- [ ] 2.3 在建立 stream 前复用 run ownership、tenant 和 internal-event policy，并保持错误为 JSON `ApiErrorEnvelope`
- [ ] 2.4 实现握手后读取/序列化异常到单个脱敏 `stream.error` frame 后关闭，证明不泄漏内部异常、provider payload 或 secret

## 3. Route 与 OpenAPI

- [ ] 3.1 注册 RUN-006 route 和 streaming headers，保持 RUN-003 行为及 CanonicalEvent schema 不变
- [ ] 3.2 增加缺失/多余 status、参数、media type 的双向 OpenAPI drift test

## 4. 验收证据

- [ ] 4.1 增加固定数据/样本/计时边界的首 frame P95 测试并证明小于 1000ms
- [ ] 4.2 运行定向 unit/contract/integration、local smoke 与真实 PostgreSQL service smoke，分开报告离线和 service 证据
- [ ] 4.3 完成该 capability 的 3 个 fresh code-reviewer Stage 1/2 PASS，保持 change 为 ready-to-archive 且不归档
