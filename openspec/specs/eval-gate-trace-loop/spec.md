## Purpose

定义从运行 trace 生成、审核并执行 eval case 的闭环，以及 score evidence、受控 CLI/API 和数据保护边界。

## Requirements

### Requirement: EvalCaseFactory 从 trace 生成 draft case
系统 SHALL 暴露 `EvalCaseFactory` 和 failed/low-score detector，用于把 failed run trace、低分 trace 或人工标记转换为 draft eval case。自动生成流程 MUST 只能写入 draft review queue；生成的 case MUST 保留 `tenant_id`、`agent_id`、`run_id`、`trace_id`、source refs、artifact refs、触发原因和脱敏摘要。

#### Scenario: Failed run trace 生成 draft case
- **WHEN** 调用方对 failed run trace 执行 `agent-harness eval draft`
- **THEN** 系统生成 `status="draft"` 的 eval case，包含 tenant、agent、run、trace 关联和 source/artifact refs，不写入 approved dataset

#### Scenario: Low-score trace 生成 draft case
- **WHEN** detector 收到低于阈值的 score signal
- **THEN** 系统生成带 `trigger="low_score"` 和 score metadata 的 draft case，且不会自动 approve

#### Scenario: Secret 进入 draft 前被拦截或脱敏
- **WHEN** trace payload、tool output、retrieval chunk 或 model output 中包含 secret fixture
- **THEN** draft case 不包含原始 secret；系统要么写入脱敏摘要，要么拒绝生成并返回可审计的 validation error

### Requirement: ReviewDatasetAdapter 分离 draft 与 approved dataset
系统 SHALL 提供 review queue / `ReviewDatasetAdapter`，确保 `eval-cases/drafts` 和 `eval-cases/approved` 分离。approved dataset 写入 MUST 只能由人工审核动作触发，审核动作 MUST 写入 audit log，并保留 reviewer、reason、case id、tenant、agent、trace 和 source refs。

#### Scenario: 人工 approve 后进入 approved dataset
- **WHEN** reviewer 通过 CLI 或 API approve 一个 draft case
- **THEN** case 进入 approved dataset，状态变为 approved，并产生 `eval.case.approved` evidence 和 audit log

#### Scenario: 自动 detector 不能写 approved
- **WHEN** 自动 detector 生成 draft case
- **THEN** 系统只写 draft queue；任何绕过人工审核直接写 approved dataset 的请求都被拒绝并保留 audit evidence

#### Scenario: Approved 写入失败不丢 draft
- **WHEN** approved dataset 写入或 audit 写入失败
- **THEN** draft case 保持可审阅状态，错误响应不包含 secret，并可通过 list 接口看到失败摘要

### Requirement: EvalRunner 只执行 approved cases
系统 SHALL 提供 `EvalRunner`，只读取 approved dataset 执行 eval。每次 eval run MUST 记录 `eval_run_id`、dataset、case ids、tenant、agent、status、score summary 和 per-case score refs。`make eval` MUST 只运行 approved cases，并在无真实 provider key 时可用 fake/local profile 产生确定性结果。

#### Scenario: make eval 只跑 approved cases
- **WHEN** `make eval` 执行时 draft 和 approved cases 同时存在
- **THEN** runner 只消费 approved cases，并在结果中列出 skipped draft count 或 draft 不参与的摘要

#### Scenario: Eval run 产出 score 记录
- **WHEN** approved dataset 执行完成
- **THEN** 系统写入 eval run、per-case score 和 summary，状态为 completed 或 failed，并可通过 CLI/API 查询

#### Scenario: Empty approved dataset 稳定返回
- **WHEN** approved dataset 为空
- **THEN** `make eval` 稳定返回 no approved cases 状态，不执行 draft case，也不伪造 score

### Requirement: ScoreSink 写入 local evidence 并降级 provider failure
系统 SHALL 提供 `ScoreSink` interface 和 local JSONL sink。score 写入 MUST 先写 local evidence，再尝试通过 Phase 10 `TelemetryFacade` 或 provider adapter contract 写回观测 provider。provider 写回失败 MUST 只产生脱敏 degraded summary，不得影响 local score evidence 或 eval run terminal 状态。

#### Scenario: Local score evidence 始终写入
- **WHEN** eval runner 提交 score
- **THEN** local JSONL sink 写入包含 eval/run/case/metric/value/source refs 的 score record

#### Scenario: Provider failure 不丢 local evidence
- **WHEN** Logfire、Phoenix 或 Langfuse adapter 写回失败
- **THEN** ScoreSink 返回 degraded provider status，local score evidence 仍可读取，错误摘要不包含 secret

#### Scenario: Provider adapter 只接收脱敏 score DTO
- **WHEN** ScoreSink fan-out 到 provider adapter
- **THEN** adapter 输入是 provider-neutral、已脱敏的 score DTO，不包含 provider SDK object、原始 trace payload 或未脱敏异常

### Requirement: Eval CLI 与 API 暴露受控入口
系统 SHALL 提供 eval draft、approve、list、run 和 score 查询的 CLI/API 入口。API 契约 MUST 覆盖 `EVL-001` draft cases、`EVL-002` approved dataset、`EVL-003` eval runs，并保持认证、policy、audit、secret redaction 和 provider failure 降级语义。

#### Scenario: Draft API 未认证不产生 side effect
- **WHEN** 未认证调用 draft 创建或 approve endpoint
- **THEN** API 返回认证错误，且不创建 eval case、approved dataset、eval run、score 或 audit side effect

#### Scenario: Approve API 需要人工审核身份
- **WHEN** reviewer 通过 API approve draft case
- **THEN** 请求必须携带有效身份和 reason；响应返回 approved case summary，不返回完整 secret payload

#### Scenario: Eval run API 暴露 score sink 状态
- **WHEN** 调用方创建或读取 eval run
- **THEN** 响应包含 eval run status、score summary、local evidence refs 和 provider degraded status，不暴露 provider 原始响应

### Requirement: Eval evidence 遵守身份、artifact 和数据保护规则
系统 SHALL 让 eval case、eval run、eval score、audit log、artifact ref 和 provider payload 全部保留 tenant / agent / run / trace 关联。大 payload MUST 使用 artifact/ref；secret MUST 在进入 trace、eval case、audit、local/jsonl、API body/error 或 provider payload 前脱敏或被阻止。

#### Scenario: 大 payload 使用 artifact ref
- **WHEN** trace 到 draft case 的输入或期望输出超过 inline 阈值
- **THEN** eval case 只保存 artifact/ref、checksum 和摘要，不把完整 payload 放进 API response 或 JSONL score

#### Scenario: Eval evidence 带完整身份关联
- **WHEN** eval case 被 draft、approve、run 或 score sink 消费
- **THEN** 相关记录都包含 `tenant_id`，并在 run 相关场景包含 `agent_id`、`run_id` 或 `trace_id`
