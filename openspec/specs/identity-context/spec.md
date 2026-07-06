# identity-context Specification

## Purpose
定义本地默认身份、租户/session/permission 上下文以及 policy/guardrail 共享 decision vocabulary，确保 P0 在无外部 auth backend 时仍能产生可序列化、可审计的 actor context，并为后续认证和策略引擎保留稳定输入边界。
## Requirements
### Requirement: IdentityContext 永远携带 tenant 和 session
package SHALL 暴露 `IdentityContext`，包含 `tenant_id`、`user_id`、`session_id`、`roles`、`permissions` 和 `auth_method`；未配置多租户认证时使用默认 local tenant/user 行为。

#### Scenario: 默认 local identity 被注入
- **WHEN** local settings 在没有多租户 auth 配置的情况下加载
- **THEN** 默认 identity 使用 `tenant_id="default"`、`user_id="local-user"`、显式 session id、roles、permissions 和 auth method

#### Scenario: Identity 可序列化进下游 payload
- **WHEN** identity context 被嵌入 run、trace、eval、audit 或 context DTO
- **THEN** 序列化 payload 保留 tenant、user、session、role、permission 和 auth method 字段

### Requirement: PermissionContext 从 identity 派生
package SHALL 暴露 `PermissionContext`，用于 policy check，并从 `IdentityContext` 派生 actor 和 session 字段，不耦合具体 authentication backend。

#### Scenario: Policy check 只依赖 permission context
- **WHEN** 后续 policy engine 校验 resource action
- **THEN** 它的输入可以用 `PermissionContext` 表示，不需要 import API key、bearer token、OIDC 或 database auth implementation

### Requirement: Policy 和 guardrail decision 共用显式值
package SHALL 暴露 `allow`、`deny`、`require_approval` decision values，使 input guardrail、budget check 和 dangerous action check 使用同一套公开词汇。

#### Scenario: Shell action 可以要求审批
- **WHEN** policy 或 guardrail 把 shell action 标记为危险动作
- **THEN** decision 可以表示为 `require_approval`，并携带 reason 和 approval metadata placeholder

#### Scenario: Denied input 不暗示创建半截 run
- **WHEN** input 在 run start 前被 deny
- **THEN** decision payload 可序列化，且不要求 runtime run id 或 checkpoint object 已存在
