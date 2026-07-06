## ADDED Requirements

### Requirement: CanonicalEvent 统一 run event envelope
package SHALL 暴露 `CanonicalEvent` envelope，包含 `event_id`、`event_type`、`event_version`、`seq`、`timestamp`、`tenant_id`、`user_id`、`agent_id`、`run_id`、`parent_run_id`、`trace_id`、`span_id`、`visibility`、`payload`、`payload_ref`、`raw_event_ref` 和 terminal marker。

#### Scenario: Envelope 字段可跨 adapter 传递
- **WHEN** event bus 发布任意 CanonicalEvent
- **THEN** persisted event 使用稳定 envelope 字段名，不暴露 provider-specific event object 或 ORM model

#### Scenario: 同一 run 只能出现一个 terminal event
- **WHEN** event bus 已为某个 `run_id` 发布 terminal event
- **THEN** 后续对同一 `run_id` 发布 terminal event 会失败或被拒绝

#### Scenario: 同一 run 内 seq 单调递增
- **WHEN** event bus 连续为同一 `run_id` 发布事件
- **THEN** persisted events 的 `seq` 从 1 开始单调递增，且断线后可按 last `seq` 继续读取

### Requirement: P0 event type catalog 与 Product-Spec 对齐
package SHALL 暴露 Product-Spec P0 catalog 中的 CanonicalEvent type，包括 run、model、guardrail、reasoning、tool、retrieval、context assembly、policy、approval、checkpoint 和 artifact 事件。

#### Scenario: Guardrail blocked 使用规范事件名
- **WHEN** input guardrail 拦截外部输入
- **THEN** event type 使用 `input.guardrail.blocked`，而不是 provider 或实现自定义的 denied/allowed 命名

### Requirement: Local jsonl sink 永远可作为证据 fallback
package SHALL 提供 local jsonl event sink，在没有外部 observability provider 时仍能持久化 trace/eval/audit 证据。

#### Scenario: 未配置外部 provider 时写入 local evidence
- **WHEN** settings observability kind 为 `local-jsonl`
- **THEN** event sink 将 CanonicalEvent 以 JSON Lines 写入 configured path，并可按 `run_id` / `seq` 读取

### Requirement: Artifact store 承载大 payload
package SHALL 提供 artifact store，把超过 inline 阈值或标记为大 payload 的内容写入 artifact，并在事件正文中只保留 `payload_ref` 和 checksum。

#### Scenario: 大 payload 不进入事件正文
- **WHEN** publisher 发送超过 inline 阈值的 payload
- **THEN** event payload 为空或为摘要，事件包含 `payload_ref` 和 checksum，artifact store 可按 ref 读取原始 payload

### Requirement: Guardrail 和 context assembly 事件不泄漏 secret
guardrail/context assembly event payload MUST 只包含摘要、`source_ref`、`trust_level`、truncation metadata 和 decision，不写完整大 payload 或 secret。

#### Scenario: Secret-like 字段被 redaction
- **WHEN** guardrail/context event payload 包含 api key、password、token 或 secret-like 字段
- **THEN** persisted event 不包含原始 secret value

### Requirement: OTel mapping facade 不依赖 provider SDK
package SHALL 暴露 CanonicalEvent 到 OTel span/metric/event 的 mapping facade，但 core event modules MUST NOT import Logfire、Phoenix、Langfuse 或 OTel exporter SDK。

#### Scenario: Mapping facade 可用 fake event 测试
- **WHEN** tests 使用 fake CanonicalEvent 调用 OTel mapping facade
- **THEN** 返回 provider-neutral mapping DTO，不需要真实 observability provider 或 API key

## MODIFIED Requirements

## REMOVED Requirements

## RENAMED Requirements
