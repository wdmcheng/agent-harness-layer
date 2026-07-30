## ADDED Requirements

### Requirement: 流式文本配置具有硬边界且不改变容量合同
typed config SHALL 提供 `model_stream_chunk_utf8_bytes` 与 `model_stream_sensitive_candidate_utf8_bytes`。前者范围 MUST 为 1～4096、默认 1024；后者范围 MUST 为 128～4096、默认 512。版本化注册表中的 64 条 delta 和 65 个 stream 事件容量 MUST 是不可由环境覆盖的硬合同。配置解析 MUST 在 composition 构造 provider 或 invocation 前完成，非法值关闭失败且不得调用 provider。

#### Scenario: 使用合法的小分片配置
- **WHEN** `model_stream_chunk_utf8_bytes=256` 且安全候选上限合法
- **THEN** invocation 以不超过 256 UTF-8 bytes 的目标形成公共分片
- **AND** 仍预留固定 65 个 stream 槽位且最多发布 64 条 delta

#### Scenario: 配置试图扩大硬边界
- **WHEN** 分片大小超过 4096、安全候选上限不在 128～4096，或环境试图配置 delta 数量/stream 容量
- **THEN** 配置解析关闭失败或拒绝未知字段
- **AND** 版本化容量合同不变，provider 未被调用

### Requirement: 默认离线且真实流式验证显式启用
默认配置 SHALL 继续选择 fake provider，并允许 deterministic fake stream 在无网络、无凭证环境覆盖成功、中断、unknown、慢消费和恢复。真实 Pydantic AI 流式验证 MUST 同时要求现有真实 provider opt-in 与独立的流式验证 opt-in；缺失任一条件时测试应明确 skip，而不是失败、伪造成功或读取秘密。

#### Scenario: 默认测试环境
- **WHEN** 未设置真实 provider 和流式验证 opt-in
- **THEN** 流式合同测试使用 fake provider 且不发起网络请求
- **AND** live latency 测试明确 skip

#### Scenario: 仅设置普通真实 provider opt-in
- **WHEN** 只允许一次性真实 provider 调用但未允许流式验证
- **THEN** Phase 18.1 live stream 测试仍明确 skip
- **AND** 不复用一次性结果冒充流式成功

### Requirement: 流式 smoke 输出独立时延证据
opt-in live smoke SHALL 输出 schema `model-stream-live-smoke/v1`，包含 `status`、`provider_called`、`existing_event_first_frame_ms`、`provider_first_delta_ms`、`committed_first_delta_ms`、`client_first_delta_ms` 与 nullable `reason_code`。时延只能是非 bool、非负 integer milliseconds 或 null；smoke MUST 在同一受控进程内协调 local runtime invocation 与事件 client，使 provider、committed 与 client 三项共用首次 provider 迭代前的 monotonic origin，不得跨进程比较不同 monotonic clock。`passed` MUST 具备全部时延、`provider_called=true`、`reason_code=null` 且 provider <= committed <= client；已有事件首 frame独立验证 `<1000ms`，不得解释为 provider SLA。`hosted-unverified` reason 只允许 `authorization_missing|stream_opt_in_missing|credential_missing|endpoint_untrusted`，`failed` reason 只允许 `contract_failure`，`external-blocked` reason 只允许 `network_unavailable|provider_rejected|quota_blocked|provider_timeout|provider_result_unknown`。本地 terminal、capacity、shared-budget、publication、policy、guardrail 或其他编排失败 MUST 输出 `failed`、进程 1/CI fail，并按已观察 response、delta 或稳定错误摘要如实保留 `provider_called`；尤其 `RunOrchestrator.start_run()` 的任何异常都属于独立的本地编排失败事实，即使业务 executor 同时返回 provider-domain 错误且后续 probe/cleanup 成功，也 MUST 最终输出 `failed/contract_failure`。in-process invocation failure MUST 使用封闭 `failure_domain=provider|runtime` 供 executor 区分来源，不得以成功 response 是否存在或通用错误码猜测，这些失败不得伪装为 `external-blocked`。`failure_domain` 不进入 artifact；artifact MUST NOT 包含 prompt、文本、secret、endpoint path、header、response id 或原始异常。

#### Scenario: fake clock 验证成功 artifact
- **WHEN** 默认离线 contract 以 fake clock 驱动 provider 首 delta、event commit 与 client receive 三个边界
- **THEN** artifact 逐字段记录非负 integer milliseconds，且 provider <= committed <= client
- **AND** 单一 total latency 不能替代任一字段

#### Scenario: live 前置不完整
- **WHEN** 本会话授权、stream opt-in、隔离凭据或受信 endpoint 任一缺失
- **THEN** smoke 零 provider 调用，输出 `status=hosted-unverified`、三项 provider 链时延为 null，并映射进程 0/CI skipped

#### Scenario: 获授权后外部阻塞
- **WHEN** 四项前置完整且已授权后发生网络、配额或 provider 故障
- **THEN** smoke 输出 `status=external-blocked`、进程 2/CI fail，并如实记录 `provider_called` 与已知时延
- **AND** 未知时延为 null，任何已知 provider/committed/client 值仍保持单调顺序

#### Scenario: provider 已响应后本地终态失败
- **WHEN** provider response 或 delta 已被观察，但 runtime 在 terminal、capacity、shared-budget 或 publication 边界失败
- **THEN** smoke 输出 `status=failed`、`reason_code=contract_failure`、`provider_called=true` 与进程 1/CI fail
- **AND** 不得把该本地失败降格为 `external-blocked` 或改写为零 provider 调用

#### Scenario: run 启动失败优先于 provider-domain 结果
- **WHEN** `RunOrchestrator.start_run()` 抛出本地异常，业务 executor 另行返回 provider-domain 错误，且 probe 与 cleanup 均成功
- **THEN** smoke 仍输出 `status=failed`、`reason_code=contract_failure` 与进程 1/CI fail
- **AND** `provider_called` 保留 executor 的安全调用事实，不得用 provider-domain 结果覆盖本地编排失败
