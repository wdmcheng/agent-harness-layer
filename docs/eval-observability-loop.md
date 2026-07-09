# Eval 与 Observability 闭环

本文是 trace-to-eval 闭环的临时控制文档。Phase 14 会把它扩展成完整维护者指南；在那之前，本文件只负责讲清当前基础、下一阶段门禁和后续实现边界。

## 当前基础

Phase 11 已经建立基础链路：

```text
failed / low-score trace
  -> draft eval case
  -> 人工 review
  -> approved dataset
  -> eval run
  -> score sink
  -> local/jsonl 和可选 provider evidence
```

基础规则不变：自动 detector 只能写 draft，approved eval case 必须经过人工 review。`make eval` 只运行 approved cases；当 approved dataset 为空时，必须返回稳定的 no-approved-cases 结果，不能伪造分数。

## Phase 12.5 计划闭环

Phase 12 必须先交付四个可运行的 P0 示例 agent。它们给 eval 系统提供真实行为分布。之后 Phase 12.5 再把基础链路升级成实验闭环：

```text
approved eval cases
  -> 行为标签
  -> optimization / holdout / regression subsets
  -> baseline harness run
  -> candidate harness run
  -> 按标签对比
  -> regression / holdout review
  -> 人工 acceptance decision
```

这个闭环必须防止过拟合。optimization run 可以使用 optimization set，但 holdout result 和 regression subset 是验收门禁。总分上涨不等于可以接受 harness 变更。

## 行为标签

初始标签至少覆盖：

- `tool_selection`
- `retrieval_quality`
- `followup_quality`
- `policy_approval`
- `context_trust_boundary`

标签是 case metadata，不是文件名注释。dataset split 和 experiment 路径必须能查询、过滤和汇总这些标签。

## Harness Version 输入

`harness_version` metadata 应覆盖所有会改变 agent 行为的输入：

- prompt 和 instruction 文本
- tool description
- agent config 默认值
- retrieval config
- policy 默认值
- model profile 或 adapter settings

accepted harness record 只保存 metadata 和 evidence refs。它不得自动改写 prompt 文件、tool description 或生产配置。

## Acceptance Gate

接受 candidate harness 必须满足：

- 目标标签分数提升，或本次变更明确修复了命名 failure mode
- holdout result 没有不可接受的 regression
- 已修复的 regression cases 仍然保持通过
- 新失败项必须列出 evidence refs
- reviewer identity、reason、policy decision 和 audit ref 都被记录

provider failure 必须降级到 local evidence。它不能删除 experiment record，也不能隐藏 comparison output。

## 实现入口

Phase 12.5 的控制文档是：

- `Product-Spec.md` REQ-016
- `API-Contract.md` EVL-004
- `DEV-PLAN.md` Phase 12.5

本阶段的 OpenSpec work 应使用聚焦 change，例如 `eval-experiment-hillclimb-loop`。不要把 Phase 13 API/worker split 或 Phase 15 release automation 塞进同一个 change。
