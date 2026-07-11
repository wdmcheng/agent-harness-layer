## Purpose

定义 EVL-004 的 tenant-scoped HTTP/CLI 接口、持久化幂等、人工 acceptance、policy/audit 与 OpenAPI 契约。

## Requirements

### Requirement: EVL-004 暴露受认证且 tenant-scoped 的 HTTP API
service-app SHALL 提供 `POST /api/v1/evals/experiments`、`GET /api/v1/evals/experiments/{experiment_id}`、`GET /api/v1/evals/experiments/{experiment_id}/comparison`、`POST /api/v1/evals/experiments/{experiment_id}/accept`。所有 operations MUST 声明 HTTPBearer，并从认证 `IdentityContext` 获取 tenant/reviewer；body 不得覆盖身份。响应 MUST 包含 `request_id`，跨 tenant 与不存在资源统一返回 404 且不得泄漏存在性。

#### Scenario: 未认证 create 无 side effect
- **WHEN** 调用方不带有效 Bearer token 创建 experiment
- **THEN** API 返回 401 `ApiErrorEnvelope`，且不创建 split、experiment、eval run、acceptance、audit 或 provider side effect

#### Scenario: 跨 tenant read 不泄漏资源
- **WHEN** tenant A 读取 tenant B 的 experiment 或 comparison
- **THEN** API 返回 404 `ApiErrorEnvelope`，响应不包含 tenant B 的 agent、dataset、score、case count、harness version 或 evidence refs

#### Scenario: Create/read 返回稳定 experiment DTO
- **WHEN** 已认证调用方以有效 approved tags、split strategy 和 baseline/candidate harness metadata 创建并读取 experiment
- **THEN** 响应包含 request/experiment/status/agent/dataset/tags/subset counts、harness/eval run refs、local refs 和 provider degraded summary，不包含完整 case payload或 provider raw response

### Requirement: EVL-004 create 与 accept 保持持久化幂等
create SHALL 要求非空、非纯空白 `Idempotency-Key` header，并以 tenant、key 和规范化 body hash 做持久化幂等；相同 key+body MUST 返回同一 `experiment_id`，相同 key+不同 body MUST 返回 409 `eval.experiment.idempotency_conflict`。Split、experiment 和首个 execution claim MUST 在同一 transaction 提交；活跃执行的重放 MUST 不调用 evaluator/provider，无法证明副作用结果的续租失败、过期 claim、中断或 terminal 写失败 MUST 返回持久化 `needs_review` 且不得自动重跑。每个 experiment SHALL 只有一条不可变 review decision；同一 reviewer 重试相同规范化 decision body MUST 返回同一 decision record，不得重复写 audit。其他 reviewer 或不同 decision/reason/version/followup ref MUST 返回 409 `eval.experiment.decision_conflict`。

#### Scenario: Create 安全重试
- **WHEN** tenant 使用相同 `Idempotency-Key` 和语义相同 body 重试 create
- **THEN** API 返回同一 experiment 与原始持久化结果，不新增第二个 experiment、eval run 或 provider call

#### Scenario: 不确定执行的安全重试
- **WHEN** tenant 在原 evaluator 仍运行、heartbeat 续租失败、claim 已过期、进程中断或 terminal 结果写失败后，以相同 key/body 重试 create
- **THEN** API 返回同一 experiment 的 `running` 或持久化 `needs_review`，不自动重跑 evaluator/provider，不创建 orphan split，并保留人工排障所需的本地关联

#### Scenario: Idempotency key body 冲突
- **WHEN** tenant 使用已存在的 `Idempotency-Key` 提交不同 agent、dataset、split 或 harness body
- **THEN** API 返回 409 `ApiErrorEnvelope`，现有 experiment 不变且没有新 side effect

#### Scenario: 合法上界 evidence 的稳定读取
- **WHEN** evaluator 返回单列表合法上界 refs，或 baseline/candidate 顶层与 per-case failure refs 合并后超过公共数量或大小上限
- **THEN** create、read、comparison、幂等 replay 与 CLI 返回同一有界数据库真相引用，不持久化失败或随后无法通过公共 DTO 校验的 terminal 记录

#### Scenario: Idempotency key 缺失或空白
- **WHEN** create 请求缺少 `Idempotency-Key` header，或 header 为空/纯空白
- **THEN** API 返回 422 `ApiErrorEnvelope`，OpenAPI 将该 header 标记为 required，且不创建 split、experiment、eval run、decision、audit 或 provider side effect

#### Scenario: Accept 安全重试
- **WHEN** reviewer 对同一 experiment、decision、reason 和 accepted harness version 重试 accept
- **THEN** 系统返回同一 acceptance response/audit ref，accepted record 和有效 audit 各只有一条

#### Scenario: 其他 reviewer 或不同 decision body 冲突
- **WHEN** experiment 已有 review decision，另一个 reviewer 或相同 reviewer 使用不同 decision、reason、version 或 followup ref 提交 accept
- **THEN** API 返回 409 `eval.experiment.decision_conflict`，既有 decision/audit 不变且不产生新 side effect

### Requirement: Comparison API 只返回完整且脱敏的证据
comparison endpoint SHALL 返回 per-tag `baseline_score`、`candidate_score`、`delta`、`holdout_delta`、regressions、new/fixed failures、`acceptance_recommendation`、非空封闭 `recommendation_reason_codes` 和脱敏 evidence refs；reason codes MUST 使用 `eval-harness-experiments` 规定的完整字面值集合。candidate 缺失、holdout 为空、关键 regression/evidence 不完整 MUST 返回稳定 409 error 或 `needs_review`，不得返回 422 或伪造可接受结论。provider failure MUST 以 degraded summary 返回并保留 local comparison。

#### Scenario: Provider degraded 仍可读取 comparison
- **WHEN** provider fan-out 失败但 local comparison 已完成
- **THEN** GET comparison 返回 200、完整 local comparison refs 与脱敏 degraded summary，且不包含 provider exception 原文或 raw response

#### Scenario: Candidate 缺失返回冲突
- **WHEN** 调用方读取只有 baseline 的 experiment comparison
- **THEN** API 返回 409 `eval.experiment.candidate_missing`，不返回空 delta 或 acceptance recommendation

### Requirement: 人工 acceptance 绑定 policy、audit 与完整门禁
accept endpoint SHALL 要求非空 `decision`、`reason`、`accepted_harness_version`（accepted 时）和可选 `followup_issue_ref`。系统 MUST 从认证 identity 记录 reviewer，执行 `eval.harness.accept` policy check，并验证 comparison：accepted version 与该 comparison 的 candidate version 完全一致、目标标签改善或命名 failure 已修复、holdout 未超过允许退化、关键 regression 通过、new failures 有 evidence。只有 `decision="accepted"`、policy allow 且门禁完整时，才能原子写唯一 review decision、accepted production binding 与 audit；`decision="rejected"` 写唯一幂等 review decision 与 audit，但 `accepted_harness_version` MUST 为空且不得产生 production binding。任何路径都不得修改 harness 输入文件或生产配置。

#### Scenario: 满足门禁后人工接受
- **WHEN** 已授权 reviewer 对 recommendation=accept 且证据完整的 candidate 提交 accepted decision
- **THEN** 系统原子写入唯一 accepted record 和 audit，包含 reviewer、reason、policy decision、harness version、comparison/evidence refs，并返回同一 `request_id`

#### Scenario: Policy deny 阻止接受
- **WHEN** comparison 门禁通过但 `eval.harness.accept` policy 返回 deny
- **THEN** API 返回 403 `ApiErrorEnvelope`，不写 accepted record、accepted audit 或生产配置 side effect

#### Scenario: Policy require_approval 不隐式创建嵌套审批
- **WHEN** comparison 门禁通过但 `eval.harness.accept` policy 返回 `require_approval`
- **THEN** HTTP/CLI 返回 409 `eval.experiment.approval_required`，允许保留 policy decision audit，但不创建 approval、review decision、accepted production binding、accepted audit 或配置 side effect

#### Scenario: Accepted version 必须匹配已比较 candidate
- **WHEN** reviewer 提交的 `accepted_harness_version` 与 experiment comparison 的 `candidate_harness_version` 不同
- **THEN** API 返回 409 `eval.experiment.accepted_version_mismatch`，不写 review decision、accepted production binding、accepted audit 或配置 side effect

#### Scenario: Holdout regression 阻止接受
- **WHEN** reviewer 尝试接受 holdout 明显退化或关键 regression 失败的 candidate
- **THEN** API 返回 409 `eval.experiment.acceptance_gate_failed`，列出脱敏 reason codes/evidence refs，不写 accepted record或修改 harness 输入

#### Scenario: 人工拒绝不产生 accepted record
- **WHEN** reviewer 提交 `decision="rejected"` 和非空 reason
- **THEN** 系统写入唯一不可变 review decision 与 audit 并返回 rejected summary，accepted version 为空且不产生 accepted production binding

### Requirement: Eval experiment CLI 与 HTTP 行为等价
核心 CLI SHALL 提供 `agent-harness eval experiment create`、`show`、`compare`、`accept`，并通过同一 service/DTO、identity、policy、audit 和 persistence seam 实现 EVL-004。CLI 成功输出 MUST 为 JSON-compatible stable payload；认证/权限、validation、not-found、conflict 和 provider degraded 语义 MUST 与 HTTP 等价，失败使用非零退出码且不输出 secret。

#### Scenario: CLI create/compare/accept 完成闭环
- **WHEN** local reviewer 依次执行 create、show、compare 和 accept，并提供有效 split/harness/decision 输入
- **THEN** CLI 输出与 HTTP 同字段的 experiment/comparison/acceptance DTO，并持久化同一类 record/evidence/audit

#### Scenario: CLI 门禁失败非零退出
- **WHEN** CLI accept 的 comparison 不完整、policy deny 或 holdout regression 超阈值
- **THEN** CLI 非零退出，输出稳定 error code/request id/脱敏 message，不写 accepted record

### Requirement: OpenAPI 和错误 envelope 完整描述 EVL-004
运行时 OpenAPI SHALL 为四个 EVL-004 operations 声明 `EvalExperimentCreateRequest`、`EvalExperimentResponse`、`EvalExperimentComparisonResponse`、`EvalExperimentAcceptanceRequest`、`EvalExperimentAcceptanceResponse` 和 HTTPBearer security。错误集合 MUST 按 endpoint 精确声明：create 为 401/403/404/409/422/500，read 为 401/403/404/500，comparison 为 401/403/404/409/500，accept 为 401/403/404/409/422/500，全部使用 `ApiErrorEnvelope`。create 必填 header MUST 在 operation 参数中标为 `required: true`；各 schema 的 required fields MUST 与 EVL-004 请求/响应表一致。Create 新建 MUST 返回 201、幂等 replay MUST 返回 200，两个 GET 与 accept MUST 返回 200；response 只保证 `Content-Type: application/json`，request correlation 使用 body `request_id`。comparison response MUST 为 `recommendation_reason_codes` 声明 `minItems: 1` 和封闭字面值枚举；accept response MUST 暴露 reviewer-derived identity summary、decision、production binding、policy decision、audit ref 与 acceptance evidence 字段；422 MUST 使用项目统一 handler，不返回 FastAPI 默认 detail 形状。

#### Scenario: OpenAPI 局部漂移检查通过
- **WHEN** contract test 读取运行时 `/openapi.json`
- **THEN** 四个 paths、required idempotency header、operation security、五个稳定 `$ref` schemas/required fields（含 comparison reason codes）、201/200 成功码、响应头说明和逐 endpoint 适用 `ApiErrorEnvelope` 集合与 `API-Contract.md` EVL-004 一致

#### Scenario: Validation error 使用统一 envelope
- **WHEN** create 或 accept body 缺少必填字段或比例非法
- **THEN** API 返回 422 `ApiErrorEnvelope`，包含 `request_id` 和脱敏字段诊断，不产生 experiment/acceptance/audit/provider side effect
