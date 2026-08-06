## ADDED Requirements

### Requirement: 模型单轮结果使用供应商中立判别联合
系统 SHALL 以 `ModelTurnResult` 表达一个模型轮次，`kind` MUST 只允许 `final_text`、`final_structured` 或 `tool_intent`，且三个 payload MUST 互斥。Final 分支 SHALL 复用既有 `ModelResponse` / structured result；tool 分支 SHALL 只携带核心 `ToolIntent`。Capability MUST 进一步收窄合法分支：tool-intent protocol 只允许 `final_text|tool_intent`，structured protocol 只允许 `final_structured`，既有 text protocol 只允许 `final_text`。系统 MUST NOT 根据任意 JSON 字段、字符串内容或 SDK event 外观推断判别值或跨 capability 冒充结果。

#### Scenario: 最终文本保持既有结果
- **WHEN** provider 返回普通最终文本
- **THEN** turn result 为 `final_text` 并逐值复用既有 `ModelResponse`
- **AND** structured 与 tool intent payload 均不存在

#### Scenario: 工具意图不伪装成结构化业务输出
- **WHEN** provider 提议一个工具调用
- **THEN** turn result 为 `tool_intent` 且 structured business result 不存在
- **AND** 即使 arguments 含类似业务 schema 的字段也不得改判为 `final_structured`

#### Scenario: 混合结果关闭失败
- **WHEN** candidate 同时声称 final text、structured result 或 tool intent 中的两个以上分支
- **THEN** 核心在任何工具 resolve/policy/handler 前以 `model.tool_intent_invalid` 关闭失败

#### Scenario: Capability 不接受其他分支结果
- **WHEN** tool-intent provider 返回 final structured，或 structured/text provider 返回 tool intent
- **THEN** 核心按对应 provider protocol 违规关闭失败并结算已发生的 model usage
- **AND** 不生成可执行 ToolIntent 或伪造成功业务结果

### Requirement: Adapter 只能提交未验证工具意图候选
支持 `tool_intent` 的 model adapter SHALL 只返回 exact `ProviderToolIntentCandidate`，包含固定 schema version、provider/model、tool name、JSON object arguments、被提供的 tool schema identity 和本轮唯一 attempt/usage 事实。SDK tool-call、callable、handler、client、raw response、credential 或 provider 原始异常 MUST NOT 越过 adapter。Adapter MUST NOT 注册或调用 executable tool callback。

#### Scenario: 两个 provider 使用同一公共候选
- **WHEN** 两个不同 provider id 的 doubles 提议同一合法工具与 arguments
- **THEN** 两者都只通过相同 provider-neutral candidate 进入核心
- **AND** SDK 类型与 provider-native execution count 均为零

#### Scenario: SDK 对象被拒绝
- **WHEN** adapter 返回 SDK tool-call object、裸 tuple 或带 callable 的 candidate
- **THEN** 公共 DTO 构造或核心边界关闭失败且不记录为合法 intent

#### Scenario: Provider 无法零执行观察 proposal
- **WHEN** 锁定 SDK 只能通过注册 executable handler 才能取得 tool call
- **THEN** adapter 在 model/tool 副作用前返回 capability unsupported
- **AND** 不得注册空 handler、自动执行或改用 JSON 文本猜测

### Requirement: 核心冻结工具 catalog 与意图身份
Runtime SHALL 从绑定 Agent descriptor 的有序 allowlist 与 Registry 只读 descriptors 的交集构造 `tool-catalog-v1`。每项 SHALL 绑定 name、input schema ref/version/digest、action/resource 和 ordinal；catalog canonical digest SHALL 进入 route/turn identity。核心 SHALL 从受信 tenant/run/agent/request/trace/operation key、turn ordinal、canonical arguments 和 catalog 派生 loop/usage/tool-call identities，provider 与业务输入 MUST NOT 自报或覆盖。

#### Scenario: Request 只缩小工具集合
- **WHEN** Agent catalog 为 A、B、C 而 request 选择 A、C
- **THEN** 冻结 catalog 只含 A、C 且保持原 relative order 和每项 schema/action/resource identity

#### Scenario: Catalog 扩权或漂移被拒绝
- **WHEN** request 增加未知工具、重排工具、替换 schema/action/resource 或恢复时 current catalog 已漂移
- **THEN** 在 model request、Registry resolve 和工具副作用前以 `model.tool_catalog_conflict` 关闭失败

#### Scenario: Provider 伪造 loop 身份无效
- **WHEN** candidate 携带或试图影响 loop id、turn ordinal、tool call id 或 usage call id
- **THEN** 核心拒绝额外字段或忽略 provider 外部身份并只使用受信派生值

### Requirement: 工具意图使用独立且可预算的 provider 请求形态
支持 `tool_intent` 的真实 deployment SHALL 只声明该 singleton capability，并固定 legacy 单 route、`max_attempts=1`、空 fallback models/routes 与 response classifier、`max_structured_repair_attempts=0`；其 route SHALL 只使用 `single-user-text-with-tool-catalog/v1`。核心 SHALL 从选定 `tool-catalog-v1` 生成 `provider-tool-catalog-v1` exact object，顶层只含 schema version 与有序 tools，每项只含 name、input schema ref/version/digest、逐值匹配 Registry 的 strict canonical schema definition 与非 bool 非负 ordinal。Catalog 唯一 bytes SHALL 使用 UTF-8、`ensure_ascii=false`、排序键、紧凑 separators、显式 null 并拒绝 NaN/Infinity/非 JSON 值。Adapter MUST 只映射该冻结快照，MUST NOT 从 SDK 或 current Registry 增删、重排或补值。

实现与 validator MUST 逐字节复算以下单工具 golden vector；schema canonical bytes 为 `{"additionalProperties":false,"properties":{"q":{"type":"string"}},"required":["q"],"type":"object"}`，其 SHA-256 为 `d90ec2f895920b2f26f124f6d07f6115e64e395e36ca80ecc9530c6202f5be29`。完整 provider catalog canonical bytes 固定为：

```json
{"schema_version":"provider-tool-catalog-v1","tools":[{"input_schema":{"additionalProperties":false,"properties":{"q":{"type":"string"}},"required":["q"],"type":"object"},"input_schema_digest":"d90ec2f895920b2f26f124f6d07f6115e64e395e36ca80ecc9530c6202f5be29","input_schema_ref":"search-input","input_schema_version":"v1","name":"search","ordinal":0}]}
```

该 bytes 长度 MUST 为 `352`，SHA-256 MUST 为 `31bc934ff80b541bd26efb154d97b3ba27ee3e2fdf7b1dcbacb2d6431b940d04`；任一默认空格、ASCII 转义、字段重排、schema body 或 ordinal 漂移都 MUST 改变 bytes/digest 并在 provider 前关闭失败。

#### Scenario: Canonical catalog 跨 provider 稳定
- **WHEN** 两个不同 provider id 接收相同 prompt 与选定工具 catalog
- **THEN** 核心交给 adapter 的 request shape、catalog canonical bytes、digest 与 byte count 逐值相同
- **AND** vendor 映射差异不得改变 route、预算或 evidence identity

#### Scenario: No-tools shape 不得携带工具
- **WHEN** capability 为 tool intent 但 route 仍声明 `single-user-text-no-tools/v1`，或 no-tools 请求携带非空工具定义
- **THEN** runtime 在 reservation、client 与 provider 前以 `model.tool_catalog_conflict` 零调用拒绝

#### Scenario: Catalog 超限或预算不足零调用
- **WHEN** canonical catalog bytes 超过 model catalog 冻结上限，checked 输入公式溢出，或联合 reservation 超过 hard budget
- **THEN** runtime 在 client/provider 前以稳定配置或预算错误拒绝
- **AND** provider request、usage claim 与工具副作用计数均为零

#### Scenario: 恢复时请求 catalog 漂移
- **WHEN** exact replay 或 approved continuation 的 request shape、schema bytes、catalog digest/byte count/max bound 或 trusted input bound 与耐久身份不一致
- **THEN** runtime 在 provider 前以 replay/catalog conflict 拒绝且不从 current Registry 补齐

### Requirement: 工具意图模型轮进入既有 usage 与重放生命周期
每个 tool-intent model turn SHALL 在 provider 副作用前完成 route/capability、Policy/HITL、CanonicalEvent capacity、usage/shared-budget reservation 与 durable started identity。全部实际 provider attempts、token、cost、latency、cleanup 和 unknown 状态 MUST 进入既有 provider-neutral evidence；是否随后执行工具 MUST NOT 改写本轮 model impact。

#### Scenario: 合法 intent 记录一次模型影响
- **WHEN** provider 完成一个合法工具意图轮次
- **THEN** usage evidence 记录全部实际 attempts 和可信 token/cost/latency
- **AND** 工具执行计数仍为零

#### Scenario: Intent exact replay 不重调 provider
- **WHEN** 同一稳定 model turn identity 已有耐久合法 intent
- **THEN** exact replay 返回同一 intent/evidence 且 provider 调用计数不增加

#### Scenario: Provider 结果未知不变成零影响
- **WHEN** tool intent candidate、usage、cleanup 或提交确认无法证明确定结果
- **THEN** model turn 进入 needs-review并保留 reservation
- **AND** 不生成可执行 `ToolIntent`、不把 token/cost/request count 记为零

#### Scenario: Model invoke审批恢复仍保持零工具执行
- **WHEN** tool-intent model turn 的既有 `model.invoke` PolicyCheck返回`require_approval`，随后matching grant从durable checkpoint恢复
- **THEN** waiting与approved continuation复用原usage/operation/request/route/catalog/turn identity，批准后provider至多调用一次
- **AND** 合法`ToolIntent`只作为模型结果返回，工具执行approval、tool claim、handler和`tool.call.*`计数仍为零

#### Scenario: Model invoke审批绑定冲突零provider调用
- **WHEN** approval/checkpoint/grant的usage、operation、request、route、catalog或turn identity缺失、过期或漂移
- **THEN** existing model invocation seam在provider和工具副作用前fail closed

### Requirement: 工具意图归一化保持零工具执行与既有模型行为兼容
本 capability SHALL 只产生和验证 intent。它 MUST NOT 调用 `ToolRegistry.call` / `call_approved`、handler、FileTool、ShellTool、MCP、工具网络，或创建、解析、恢复工具执行approval，也 MUST NOT 生产 `tool.call.started|completed|failed`。它 SHALL保留既有`model.invoke` Policy/HITL waiting与approved continuation；该continuation只授权原model turn，不能授权工具。既有text completion、text streaming、route-chain、structured output和人工tools CLI/runtime行为 SHALL保持兼容。

#### Scenario: 合法 intent 仍不执行
- **WHEN** 核心成功生成 `ToolIntent`
- **THEN** 所有工具 handler、文件写、shell、MCP 与外部 network 计数均为零
- **AND** 输出只可交给后续获授权的 `policy-gated-tool-loop` seam

#### Scenario: 既有模型路径不退化
- **WHEN** 运行 text、stream、route-chain、structured 和 fake eval 回归
- **THEN** 结果形状、调用顺序、usage/replay 与错误语义逐值保持既有合同

### Requirement: 公开工具选择只能保序缩小绑定目录
系统 SHALL 提供 exact `ToolCatalogSelection` DTO，唯一字段 `tool_names` MUST 为 string tuple；该 DTO SHALL 作为 `BoundModelInvocationService.complete_tool_intent(request: ModelRequest, *, operation_key: str, tool_selection: ToolCatalogSelection | None = None)` 的独立关键字参数，不得扩展 `ModelRequest` 或接收任意字典。`tool_selection=None` MUST 表示使用绑定 Agent descriptor 的完整 `tool_policy.allowed_tools`；存在 DTO 且 `tool_names=()` MUST 表示本轮提供空 catalog；非空 tuple MUST 为该绑定列表的唯一、保序子序列。未知、重复、重排、额外字段或非 string 值 MUST 在 usage claim、reservation、client、provider 与工具副作用前以 `model.tool_catalog_conflict` 关闭失败。选择只决定本轮 provider catalog，不得更改 Agent 授权、Registry descriptor、schema、action/resource 或 ordinal；snapshot、approval 与 replay MUST 绑定选定结果。

#### Scenario: 缺省选择使用完整目录
- **WHEN** caller 未提供 `tool_selection`
- **THEN** bound seam 使用绑定 Agent descriptor 的完整有序工具目录
- **AND** request identity、预算与耐久 snapshot 绑定完整 provider catalog bytes

#### Scenario: 空选择产生合法空目录
- **WHEN** caller 提供 `ToolCatalogSelection(tool_names=())`
- **THEN** 本轮 provider catalog 为空且 tool-intent protocol仍只允许返回最终文本或协议违规
- **AND** 不把空列表解释为缺省完整目录

#### Scenario: 合法子集保持 descriptor 顺序
- **WHEN** caller 提供绑定目录的唯一保序非空子序列
- **THEN** 核心只投影这些工具并保留其选择顺序形成连续 provider ordinal
- **AND** 原 Agent 授权与 Registry catalog 不被改写

#### Scenario: 非法选择在模型副作用前拒绝
- **WHEN** `tool_names` 含未知、重复、乱序、非 string 值，DTO 含额外字段，或 caller 试图把选择塞进 `ModelRequest`
- **THEN** 公共边界以 `model.tool_catalog_conflict` 零 claim、零 client、零 provider、零工具副作用拒绝
