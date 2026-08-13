# durable-run-queue Specification

## Purpose
定义 provider-neutral run queue DTO、Redis Streams 至少一次交付、fenced receipt、enqueue 幂等与本地测试替身的长期契约，确保 API 与 worker 跨进程传递逻辑任务时可恢复、可审计且不会重复执行。

## Requirements

### Requirement: Run queue 使用稳定 provider-neutral DTO
核心包 SHALL 暴露可 JSON 序列化的 `RunQueueMessage`、delivery receipt 与 `RunQueue` protocol。每条 message MUST 包含该逻辑 operation 首次成功 enqueue 固定的 `request_id`、`operation_id`、effective `idempotency_key`、`tenant_id`、`run_id`、`kind` 和 `schema_version=1`。初始执行的 operation id MUST 为 `run:<run_id>:execute`；`resume_approval` MUST 只增加 `approval_id`、`resolution_lease_id` refs，operation id MUST 为 `run:<run_id>:approval:<approval_id>:lease:<resolution_lease_id>`。初始执行未提供客户端 key时 effective key等于 operation id，提供时保留客户端值；approval continuation的 effective key MUST 等于其 operation id。message不得包含 ORM session、Redis client、DBOS handle、approval grant、provider SDK object、secret、完整 run input或绝对本机路径。

#### Scenario: Message 保留跨进程关联字段
- **WHEN** API 为已持久化 run 构造 queue message
- **THEN** 序列化 payload完整保留四个必需关联字段、operation id、kind和 `schema_version=1`，并可在另一进程中通过同一 DTO校验

#### Scenario: 非法边界 payload fail closed
- **WHEN** 调用方缺少必需 header、使用空标识、未知 schema version/kind、approval kind 缺少 refs 或提交无法通过 DTO 校验的 payload
- **THEN** queue seam 返回带字段路径的稳定 validation error，且不写入逻辑任务

#### Scenario: 未提供客户端幂等键时生成稳定 effective key
- **WHEN** RUN-001 创建了没有客户端 idempotency key 的唯一 run
- **THEN** producer以 `run:<run_id>:execute` 同时作为 operation id和 effective key，同一 operation的 enqueue重试复用该值，不改变 HTTP“新请求可创建新 run”的语义

#### Scenario: Approval continuation 使用独立 operation
- **WHEN** 同一 run的初始 execute operation已 enqueue，随后 APR-002取得新的 approval resolution lease
- **THEN** producer以 approval/lease派生的新 operation id和 effective key enqueue `resume_approval`；它不与初始执行或其他 lease冲突，同一 lease重试仍复用原 entry

#### Scenario: 未知 message version 不被旧 worker 消费
- **WHEN** consumer 读取 `schema_version` 不是 1 的 message
- **THEN** adapter 返回稳定 unsupported-version error、保留 entry 未 ack，并允许支持该版本的后续 worker reclaim

### Requirement: Redis Streams adapter 提供可恢复的至少一次交付
Redis adapter SHALL 使用 consumer group 提供 enqueue、阻塞 pickup、fenced ack 与 idle pending reclaim。delivery receipt MUST 包含 stream/group/message id、consumer id 和当前 delivery count；ack MUST 在 Redis 中原子核对 pending entry 的当前 owner 与 delivery count，stale receipt MUST 被拒绝。消息只有在调用方确认执行结果已持久化后才能 ack；未 ack message MUST 在超过配置的 idle lease 后可被另一 consumer reclaim，并返回新的 receipt。

#### Scenario: 新 message 被一个 consumer pickup
- **WHEN** producer enqueue 合法 message，两个同组 consumer 同时读取新消息
- **THEN** Redis 只把该 delivery 分配给一个 consumer，receipt 可用于确认同一 stream entry

#### Scenario: Worker 崩溃后 reclaim pending message
- **WHEN** consumer pickup 后未 ack 且 delivery 超过 idle lease
- **THEN** 另一个 consumer 可 reclaim 同一 stream entry，message 的 `run_id` 和关联字段保持不变，receipt owner/delivery count 更新

#### Scenario: Ack 后不再投递
- **WHEN** consumer 用匹配 receipt 成功 ack message
- **THEN** 后续新消息读取和 pending reclaim 都不再返回该 entry

#### Scenario: Reclaim 后旧 receipt 不能 ack
- **WHEN** consumer A pickup 后超时，consumer B reclaim 同一 entry，随后 A 使用旧 owner/delivery count receipt 请求 ack
- **THEN** adapter 原子拒绝 stale ack，B 的 pending ownership 保持有效；只有 B 的当前 receipt 在应用结果已持久化后可以 ack

### Requirement: Enqueue 幂等不创建第二个逻辑任务
Redis adapter MUST 以 `tenant_id` 与 `operation_id` 建立原子 enqueue去重，并保护 run id、effective `idempotency_key`、kind、approval refs与 schema version；`request_id`是该 operation首次成功 enqueue的 immutable correlation，不参与后续重试 conflict。相同 operation重复 enqueue SHALL返回原 stream id和原始 message；受保护字段变化 MUST fail closed。不同 operation（包括同一 run的 execute与一个或多个 approval lease）MUST拥有独立 entry且互不冲突。

#### Scenario: API 重试复用同一 queue message
- **WHEN** producer为同一 operation/effective key/kind重试 enqueue，但本次 HTTP request产生新的 attempt request id
- **THEN** enqueue 返回原 stream id 与首次 message 的 `request_id`，stream 中只存在一条可执行任务，新的 attempt id 只留在调用日志/audit 而不改写 queue payload

#### Scenario: 同一 operation 不同 payload 被拒绝
- **WHEN** producer复用同一 tenant/operation组合但改变 run、kind、effective key、version或 approval refs
- **THEN** adapter 返回稳定 conflict error，原 message 保持不变且不新增 stream entry

### Requirement: Queue adapter 保持本地可测试替身
核心包 SHALL 提供与 `RunQueue` 同契约的内存 fake，用于不依赖 Redis 的确定性合同测试；fake 只能作为 local/test seam，service profile 证据 MUST 使用真实 Redis。

#### Scenario: Fake 与 Redis 共享公开行为合同
- **WHEN** 同一 contract suite 对 fake 和 Redis adapter 执行 enqueue、pickup、ack、重复 enqueue 与 reclaim 场景
- **THEN** 两者返回相同 DTO/error 语义，且 service 验收明确记录真实 Redis 结果

### Requirement: Queue delivery 后从 durable run context 恢复可信来源
service worker SHALL 在既有 `RunQueue` pickup 或 reclaim 取得 run reference 后，从 PostgreSQL run 的私有 execution context 读取并分类可信 provenance。queue message DTO、pickup/reclaim 顺序、consumer ownership、delivery count、ack 与 DBOS 语义 MUST 保持既有契约；本 change MUST NOT 为 provenance 增加 inspect、fenced claim、row-lock claim 或新的 queue capability。

#### Scenario: Fresh pickup 使用持久化 provenance
- **WHEN** service worker 通过既有 pickup 取得一个由 CLI 创建的 run
- **THEN** worker 从 durable run context 分类出 `source=cli`，业务 input 与 queue message 都不包含 transport `source`

#### Scenario: Reclaim 使用同一持久化 provenance
- **WHEN** service worker 通过既有 reclaim 恢复同一个 CLI run
- **THEN** worker 恢复与首次执行相同的 typed provenance 和 authoritative nullable request id，不根据当前 worker 环境重新推断来源

#### Scenario: 非 CLI run 保持无 CLI provenance
- **WHEN** API、内部 runtime 或 delegation 创建的 run 被 pickup 或 reclaim
- **THEN** classifier 返回该记录真实的非 CLI/legacy 结果，不把 worker transport 或 queue 来源冒充为 `source=cli`

#### Scenario: Malformed private context 在执行前失败关闭
- **WHEN** 已取得的 run reference 指向旧键 `provenance`、未知 `run-input-provenance-v1` 版本/来源、非 exact 字段或 request-id 不一致的私有 execution context
- **THEN** worker 在 executor、approval continuation、provider、tool 与 terminal 写入前以稳定私有 context 错误失败，不修改 queue 协议或回退为从业务 input 猜测来源

### Requirement: Queue 公开与持久化形状保持不变
本 change SHALL 复用现有 queue DTO 和 run persistence schema。provenance 只存在于既有私有 execution-context JSON；不得加入 queue payload、公开 `RunRecord`、HTTP/OpenAPI schema 或新的数据库列。

#### Scenario: Queue DTO 不含 provenance
- **WHEN** CLI、API 或 delegation run 被投递到 service queue
- **THEN** queue payload 的字段集合与本 change 前一致，递归检查不出现 provenance 或 transport `source`；其既有非空 delivery `request_id` 不得填充或覆盖 nullable `input_provenance.execution_request_id`

#### Scenario: 既有 pickup/reclaim adapter 不要求迁移
- **WHEN** 现有 in-memory 或 Redis `RunQueue` 实现消费本 change 创建的 run
- **THEN** 它们继续实现原有 protocol 即可，不需要 `ClaimableRunQueue`、inspect candidate 或 fenced claim 方法
