## ADDED Requirements

### Requirement: RUN-001 使用可选 caller trace 或服务端生成 trace
RUN-001 SHALL 接受可选 `X-Trace-Id`。合法 caller value 进入统一 runtime trace normalizer；缺失时服务端生成 canonical trace。空白、超长、非法字符或已绑定其他 root run 的 value MUST 在业务副作用前返回统一 `ApiErrorEnvelope`；公开 body 不回显内部 trace 生成细节。

#### Scenario: 缺失 header 仍建立 trace
- **WHEN** 已认证调用方不带 `X-Trace-Id` 创建 run
- **THEN** RUN-001 按当前 local/service success status 返回，后续 RUN-003 event evidence 可读取非空 canonical trace

#### Scenario: 非法 header 被拒绝
- **WHEN** 调用方提供空白、超长、非法字符或已绑定其他 root run 的 `X-Trace-Id`
- **THEN** API 返回 422 validation_error 或 409 trace conflict 的 `ApiErrorEnvelope`，且不创建 run、queue message 或 event
