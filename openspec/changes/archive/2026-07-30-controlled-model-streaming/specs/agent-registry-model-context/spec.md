## ADDED Requirements

### Requirement: 业务执行器必须通过可信绑定入口选择文本流
生产 Agent executor SHALL 只通过 `build_execution_context()` 注入的 `BoundModelInvocationService` 选择流式文本，不得直接取得未绑定的 `ModelInvocationService`，也不得从业务输入接收 tenant、run、agent、trace 或 `usage_call_id`。绑定 façade MUST 同时提供异步 `stream(request, operation_key=...) -> ModelResponse` 与 `stream_approved(request, operation_key=..., grant=...) -> ModelResponse`：普通入口以受信运行上下文和调用方提供的语义 `operation_key` 生成稳定 `usage_call_id`；审批入口 MUST 复用既有 grant 全绑定、单次 lease 与 current hard-gate 重检，并把唯一 identity 固定到 `approved:{grant.approval_id}`，不得通过改变 `operation_key` 扩成第二次 provider 调用。返回值只在 durable completed/usage 闭合后给出最终 `ModelResponse`，增量不从该返回值或 iterator 暴露；SSE/CLI 仍只读取 committed events。

#### Scenario: 运行上下文暴露可信普通流式入口
- **WHEN** runtime 以 `build_execution_context()` 绑定 model invocation service，业务 executor 从 context 取得该服务并调用 `stream(request, operation_key="answer")`
- **THEN** façade 使用可信 tenant、run、agent、request、trace 与语义槽位生成稳定 `usage_call_id`
- **AND** 业务 executor 无法覆盖上述身份，也无法取得底层未绑定 stream seam

#### Scenario: 审批续跑只能消费唯一流式调用槽位
- **WHEN** soft policy 要求审批且 continuation 携带匹配的 durable approval grant
- **THEN** `stream_approved` 复用既有审批绑定、单次 lease 与当前 hard-gate 重检
- **AND** `usage_call_id` 的语义槽位固定使用 `approved:{approval_id}`，调用方传入的 `operation_key` 只用于可读关联，不能制造额外 provider 调用

#### Scenario: 未批准或不匹配的流式审批零副作用
- **WHEN** 普通 `stream` 命中 `require_approval`，或 `stream_approved` 收到缺失、过期、已消费或字段不匹配的 grant
- **THEN** 调用在 stream/usage 容量、started、client send 与 provider 迭代前以既有 policy/approval 稳定错误停止
- **AND** 不发布 delta/completed，不允许调用方绕到底层 stream seam

### Requirement: 路由按供应商中立能力协商文本流
模型 route SHALL 使用受信任 capability `text_stream` 显式声明增量文本能力。router MUST 通过独立的 `prepare_stream` seam 取得 `PreparedModelStreamCall`，并保持 prepare 阶段无网络副作用；只有 invocation 在容量、预算、outbox 和 started 证据均成功后调用 send/iterate，才允许第一次供应商副作用。既有 `text_completion` 与 `complete` 行为不得改变。

#### Scenario: 流式 prepare 不产生网络副作用
- **WHEN** invocation 为支持 `text_stream` 的 route 调用 `prepare_stream`
- **THEN** router 可以取得并持有 provider permit/client lease，但不发送网络请求、不消费响应流
- **AND** invocation 完成全部前置持久化后才开始迭代 provider stream

#### Scenario: 一次性调用保持兼容
- **WHEN** 调用使用既有 `text_completion` capability
- **THEN** router 继续使用既有 `prepare`/`complete` 协议
- **AND** 新流式协议不会改变 fake、测试 double 或一次性 Pydantic AI 调用结果

### Requirement: 流式 provider 关闭结果必须可分类
`PreparedModelStreamCall` SHALL 提供确定性的本地资源关闭，并返回 provider-neutral `ModelStreamCloseResult`。结果 exact shape 为 `state=not_started|stopped|unknown` 与 nullable `ModelStreamUsage`；usage 包含 `finality=partial|complete`、nullable token/cost、受校验 cost status 与非负 latency，不得含 SDK 类型。`not_started` 禁止 usage；`stopped` 只有在适配器能够证明远端不会继续产生副作用时才允许，且可携带 partial/complete usage；`unknown` 只允许 null/partial usage。调用方取得 iterator 不等于 provider 已开始；若 deadline 在 SDK stream context 创建前耗尽，adapter MUST 仍返回 `not_started`。一旦 context 已创建，普通 context 退出、task cancellation、socket 关闭或本地超时本身不得被当作停止证明。适配器 MUST 在退出时清理本地后台任务和 client lease，但不得因此伪造远端已停止。

#### Scenario: 未开始即关闭
- **WHEN** 调用方已请求首次迭代，但 deadline 在 SDK stream context 创建前耗尽，或 provider stream 尚未开始迭代就被关闭
- **THEN** seam 返回 `not_started` 并释放本地资源
- **AND** 若双预留事务尚未提交则随 UoW 回滚；若 durable started 已发布，则系统保留 started、取消 stream 占位并通过 not-started cancelled usage final 闭合容量和预算，不撤销已持久化 evidence

#### Scenario: 本地取消无法证明远端停止
- **WHEN** 已开始的 provider stream 因 task cancellation 或连接异常退出，且供应商没有停止确认
- **THEN** seam 返回 `unknown`
- **AND** invocation 保留未决结算与终态围栏

#### Scenario: 已证明停止并返回完整 usage
- **WHEN** provider 明确证明远端停止且返回完整、可信的 input/output 与当前启用 cost 维度
- **THEN** seam 返回 `state=stopped`、`usage.finality=complete` 的 provider-neutral close result
- **AND** invocation 可从该 DTO 生成中断 usage evidence，不读取 SDK object

#### Scenario: unknown 仅携带已观察 usage
- **WHEN** adapter 已观察部分 token/cost 但无法证明远端停止
- **THEN** seam 只可返回 `state=unknown`、`usage.finality=partial`
- **AND** 该 usage 只进入 attempt 审计，不授权结算、退款、lease 释放或 terminal

### Requirement: Pydantic AI 锁定版本使用原始事件流
Pydantic AI adapter SHALL 使用项目锁定版本的 `Agent.run_stream_events` 原始事件流，并消费到唯一最终结果事件。适配器 MUST 只把 `TextPart` 的 start/delta 追加内容转为文本增量；tool、reasoning、structured 和其他事件保持私有。适配器 MUST 验证最终事件存在且只出现一次，并从最终 `AgentRunResult` 提取输出与 provider usage。SDK usage 在一次 adapter 生命周期内 MUST 最多读取一次并缓存 provider-neutral 转换结果；读取抛异常，或 bool、负数、非整数等值无法通过公共 usage 合同时，调用结果与关闭结果 MUST 稳定归类为 `unknown`，本地 `aclose()` 不得再次读取同一 SDK usage 或把该异常逃逸到 invocation 之外。不得使用跳过结果校验的捷径，也不得依赖供应商原生 cursor 作为恢复身份。

#### Scenario: 原始事件流正常结束
- **WHEN** 锁定 Pydantic AI 返回若干文本 part 事件并以一个 `AgentRunResultEvent` 结束
- **THEN** 适配器按追加顺序输出文本片段，并产生一个包含最终输出和 usage 的 `ModelResponse`
- **AND** 调用完成前事件流被消费到最终结果

#### Scenario: 最终结果事件缺失或重复
- **WHEN** 原始事件流结束时没有最终结果，或出现多个最终结果
- **THEN** 适配器关闭失败并将副作用状态按可证明事实分类
- **AND** 不合成最终响应、不发布 completed 或零 usage

#### Scenario: SDK usage 无法安全读取或转换
- **WHEN** 唯一最终事件存在，但 SDK usage accessor 抛异常，或返回 bool、负数、非整数等非法值
- **THEN** adapter 对 result 与 close seam 复用同一次读取事实，返回稳定 `model.provider_side_effect_unknown` 与 `state=unknown`
- **AND** `aclose()` 不再次读取 SDK usage、不抛出原始异常，invocation 将 usage、共享预算与 owner ledger 耐久提升为 needs-review
