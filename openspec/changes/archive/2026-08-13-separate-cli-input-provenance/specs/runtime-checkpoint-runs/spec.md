## ADDED Requirements

### Requirement: CLI 可信来源与业务 input 分离
CLI run composition SHALL 构造封闭 typed provenance `source=cli`，并通过显式私有 submission seam 交给 runtime。该类型 MUST 只定义于内部下划线模块，不得从 `agent_harness.runtime` 导出；公开 `RunOrchestrator.start_run` 的参数集合 MUST 保持不变，普通公开 caller 不得构造或传入 CLI provenance。CLI MUST NOT 向业务 input 注入、删除或特殊解释 `source`；调用方显式提交的同名字段仍由 Agent 业务 schema 决定。provenance MUST NOT 进入 prompt、provider request、公开 run input、HTTP/OpenAPI schema 或 delegation input/hash。

#### Scenario: 严格业务 DTO 不接收 transport 字段
- **WHEN** CLI 调用只提交严格 Agent 所需的业务字段
- **THEN** Agent 收到的 input 只含调用方字段，不因 `source=cli` 产生 extra-field 错误

#### Scenario: 业务 source 字段保留业务语义
- **WHEN** Agent schema 明确定义业务字段 `source` 且调用方提交该字段
- **THEN** runtime 原样保留业务值；可信 CLI provenance 仍通过独立 typed 参数传播，不覆盖、删除或读取该业务字段

#### Scenario: Provider 与 delegation 不观察 provenance
- **WHEN** CLI run 进入 model invocation 或产生 delegation
- **THEN** provider request、delegation input 与规范化 hash 与等价非 CLI 业务输入保持相同，递归检查不包含 provenance

#### Scenario: 公开 runtime seam 不暴露 provenance
- **WHEN** 普通 module caller 只通过 `agent_harness.runtime` 与公开 `RunOrchestrator.start_run` 创建 run
- **THEN** public export 与参数集合与本 change 前逐值一致，caller 不能传入或伪造 `source=cli` provenance

### Requirement: 私有 execution context 封闭保存 provenance 与 nullable request id
runtime SHALL 在既有 private execution-context JSON 中使用可选键 `input_provenance` 保存 CLI typed provenance 与 authoritative nullable execution request id。该值 MUST 是 exact `{"schema_version":"run-input-provenance-v1","source":"cli","execution_request_id":<non-empty-string-or-null>}`，字段集合必须恰为 `schema_version/source/execution_request_id`；`execution_request_id` MUST 与同一 context 顶层 nullable `request_id` 逐值相同。内部 `RunInputProvenance` DTO 只承载封闭的 `source=cli`。缺少 `input_provenance` MUST 被分类为合法 legacy/非 CLI，并从既有顶层 `request_id` 恢复 authoritative nullable 值；旧键 `provenance`、未知版本/来源、额外或缺失字段、错误类型、空字符串 ID 或与顶层 `request_id` 冲突 MUST 以 `execution_context.provenance_invalid` 失败关闭。classifier 不得使用任意 metadata mapping，也不得从业务 input、queue message 的 delivery request id、approval resolution request id 或当前组件推断/回填来源与 execution request id。CLI 未提供 execution request id 时 MUST 保持 JSON `null`，不得生成替代值。

#### Scenario: CLI 首次创建并无损读取
- **WHEN** CLI 创建 run 且没有 request id
- **THEN** durable private context 保存顶层 `request_id=null` 与 exact `input_provenance={"schema_version":"run-input-provenance-v1","source":"cli","execution_request_id":null}`，读取和分类后逐值相同

#### Scenario: CLI 私有 submission seam 保留已有 request id
- **WHEN** CLI composition 通过私有 submission seam 以显式 request id 和 CLI provenance 创建 run
- **THEN** private context 顶层 `request_id` 与 `input_provenance.execution_request_id` 原样保存同一非空值，后续恢复使用同一值

普通 module/public caller 即使提供显式 request id，也 MUST 继续通过公开 `RunOrchestrator.start_run` 创建无 CLI provenance 的 run。

#### Scenario: 非 CLI 与 legacy context 不被误分类
- **WHEN** classifier 读取没有 CLI provenance 的合法 API/internal/delegation context 或既有 legacy 记录
- **THEN** `input_provenance` 缺失，classifier 不产生 `source=cli`，从既有顶层 `request_id` 恢复 authoritative nullable 值且不改写持久化记录

#### Scenario: 未知字段或非法组合失败关闭
- **WHEN** private context 含旧键 `provenance`、未知版本/来源、额外或缺失字段、错误类型、空字符串 ID，或 envelope 与顶层 `request_id` 不一致
- **THEN** classifier 返回稳定 `execution_context.provenance_invalid`，不把值投影到业务 input、公开 DTO 或 provider request

### Requirement: 幂等、terminal 与 approval resume 区分 execution 与当前入口 correlation
local/service 的首次创建、幂等重放、terminal recovery 与 approval resume SHALL 通过同一 private classifier/repository seam 取得 provenance 与 authoritative nullable execution request id。重建 executor/continuation context 时 MUST 使用该 classified execution request id，不得使用 queue delivery、当前 worker、approval 组件或恢复入口的 request id 替代。approval continuation SHALL 从既有 resolution lease 取得当前 resume request id，并通过不改变公开 `RunOrchestrator.resume_run` 参数集合的私有 seam 传递；APR-002 resolution operation、`run.resumed` 与本次恢复新生成的 terminal event MUST 使用该当前 resolution request id，遵守既有主规格。provenance 与原 execution request id 不得改写该公开/transport correlation。

#### Scenario: 幂等重放保持首次 provenance
- **WHEN** 同一幂等 operation 重放 CLI run
- **THEN** runtime 复用首次 durable provenance 与 authoritative request id，不重新注入业务字段或生成第二来源

#### Scenario: Terminal recovery 保持来源并使用当前恢复 request id
- **WHEN** run 在 terminal evidence 写入前中断并恢复
- **THEN** executor recovery 使用已分类 private provenance；新 terminal event 使用本次恢复入口 request id，公开 event 与 `RunRecord` 字段集合不因 provenance 扩展

#### Scenario: Approval resume 的 executor 使用 authoritative execution request id
- **WHEN** CLI run 的 approval continuation 被 local 或 service worker 恢复，classified private context 的 execution request id 与当前 resume request id 不同，或前者为 `None`
- **THEN** 重建的 executor/continuation context 使用 classified authoritative nullable execution request id；APR-002 resolution operation、`run.resumed` 与新 terminal event 使用当前 resume request id，二者逐值独立且不互相替代

#### Scenario: Approval resume 不改变既有 grant 语义
- **WHEN** approval grant 合法、过期、伪造或绑定不匹配
- **THEN**既有 grant/lease/fencing 与 handler at-most-once 语义保持不变；provenance 只提供可信来源和 authoritative request id，不扩大授权

### Requirement: Guardrail 与 audit 消费可信 provenance
runtime SHALL 把 typed provenance 作为独立受信上下文提供给适用的 input guardrail 与 audit seam。它们不得从业务 input 的 `source` 字段推断 transport 来源；输出必须保持既有脱敏和公开 schema。

#### Scenario: Guardrail 与 audit 识别 CLI 来源
- **WHEN** CLI run 进入 input guardrail 与 audit
- **THEN** 两者从 typed provenance 识别 `source=cli`，同时观察到的业务 input 不含自动注入的 transport 字段

#### Scenario: 非 CLI input 不被标记为 CLI
- **WHEN** 相同业务 input 由 API、internal runtime 或 delegation 提交
- **THEN** guardrail 与 audit 不因字段名或内容相同而推断 `source=cli`
