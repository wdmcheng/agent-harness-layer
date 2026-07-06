## ADDED Requirements

### Requirement: 公共 DTO base 提供稳定 JSON 序列化
`agent_harness` package SHALL 暴露公共 DTO base，通过 Pydantic v2 校验，并用稳定字段名序列化跨边界 payload。

#### Scenario: DTO 使用稳定字段名序列化
- **WHEN** 调用方创建公共 DTO 并导出边界 payload
- **THEN** 输出使用确定的 JSON-compatible 值，不包含 vendor SDK object 或私有实现类型

#### Scenario: DTO 校验错误暴露字段路径
- **WHEN** 调用方通过公共 DTO seam 校验非法数据
- **THEN** 校验结果包含失败字段路径，供 CLI/API 层展示可操作诊断

### Requirement: 错误封套保留 typed error detail
package SHALL 暴露 typed application error 和 API error envelope，包含稳定 error code、message、可选 field path 和 remediation hint。

#### Scenario: 缺失配置转换为结构化错误
- **WHEN** settings load 过程中缺失必填配置
- **THEN** error envelope 包含 code、缺失字段路径和修复建议

### Requirement: 信任标记和 context reference 是边界 DTO
package SHALL 暴露 `TrustLevel`、`SourceRef`、`ContextRef`、context input/output DTO 以及 guardrail decision 契约，供后续 guardrail、context assembly、tool、MCP、retrieval 和 event payload 复用。

#### Scenario: 外部内容保留 trust metadata
- **WHEN** user、tool、MCP 或 retrieval content 被表示为 context input
- **THEN** DTO 序列化结果保留 `source_ref`、`trust_level` 和 truncation metadata

#### Scenario: Guardrail decision 可序列化进 audit
- **WHEN** guardrail 返回 allow、deny 或 require approval
- **THEN** decision 序列化结果包含 reason 和可选 audit metadata，适合后续 trace/audit event

### Requirement: 核心契约不 import vendor SDK
Core contract modules MUST NOT 在未来 adapter / integration seam 之外 import Pydantic AI、DBOS、Logfire、Phoenix、Langfuse 或其他 provider SDK。

#### Scenario: Contract tests 只依赖 fake seam
- **WHEN** contract tests 校验 DTO、error、trust 和 context 行为
- **THEN** tests 不需要真实 provider SDK import 或 API key 也能通过

## MODIFIED Requirements

## REMOVED Requirements

## RENAMED Requirements
