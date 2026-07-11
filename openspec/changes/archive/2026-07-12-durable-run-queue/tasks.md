## 1. Queue DTO 与公共协议

- [x] 1.1 先以公开模块测试锁定 `schema_version=1`、execute/approval operation id、缺省/客户端 effective key、每 operation首次 request correlation、approval refs、稳定 JSON/hash、非法版本/字段路径和 vendor/secret边界，再实现 DTO、错误、`RunQueue` protocol与 exports。
- [x] 1.2 以同一 contract suite 锁定 enqueue/pickup/fenced ack、request-id retry、受保护字段 conflict、idle lease、reclaim 和 stale receipt 拒绝，再实现可注入时钟的内存 fake。

## 2. Redis Streams Adapter

- [x] 2.1 核验并锁定 redis-py 版本，先写真实 Redis 条件合同，再实现独立 namespace、consumer group 建立、阻塞 pickup、`XPENDING` receipt、`XAUTOCLAIM` 和未知版本不 ack。
- [x] 2.2 先以并发/中断合同锁定原子 tenant/operation dedupe + `XADD`、execute与多个 approval lease独立 entry、同 operation新 request id复用首次 message、同 operation受保护字段 conflict、A pickup -> B reclaim -> A stale ack拒绝、B ack后不可见，再实现 enqueue/ack Lua和资源关闭/清理。

## 3. 集成与回归证据

- [x] 3.1 运行 queue DTO/fake/真实 Redis 定向测试、类型/边界检查和既有 config/doctor 回归，记录 fake 不替代真实 service 证据。
- [x] 3.2 更新本 change tasks、严格校验并记录依赖/lock/license 证据，使下游 runtime change 只消费已导出的 queue seam。
