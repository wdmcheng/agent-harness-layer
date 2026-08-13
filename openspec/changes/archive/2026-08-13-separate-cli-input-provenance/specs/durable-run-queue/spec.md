## ADDED Requirements

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
