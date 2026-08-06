## 1. 受控循环外壳

- [x] 1.1 先在绑定运行时公开接缝补充失败测试，覆盖`final_text`直接结束、单次工具意图后继续、多次工具意图后结束、提供方试图自行执行工具，以及tool-intent provider返回`final_structured`时按跨capability协议违规结算已发生model usage、进入稳定失败、不返回structured success且Registry/handler零调用；再实现 `BoundModelToolLoopService`，完成时证明只有runtime拥有循环推进权且只有`final_text`可成功结束。
- [x] 1.2 先为固定步骤顺序补充观察性测试，断言每轮依次执行模型判别、Registry 解析、Policy 决策、HITL 分支、工具执行、结果守卫、上下文组装与下一轮模型请求；再实现编排，完成时提供调用序列和失败短路证据。
- [x] 1.3 先为绑定一致性补充负向测试，覆盖 tenant、run、agent、user、provider、model、tool 与策略版本漂移；再实现不可变绑定校验，完成时证明漂移会在任何外部副作用前失败关闭。
- [x] 1.4 先为 Agent exact `model_tool_loop` 补充 Registry/scaffold/descriptor red contracts，覆盖required-iff任一route支持`tool_intent`、五字段全部必填、无默认值、额外字段、bool/非有限数/固定范围、token/cost根预算交叉约束及只读摘要；再由 Registry loader/descriptor owner实现配置投影，完成时证明失败发生在executor/client导入前，普通fake scaffold不携带该对象。

## 2. Policy、HITL 与执行门禁

- [x] 2.1 先补充 Policy 允许、拒绝和需要审批三类测试，断言非法意图与 Policy 拒绝均不会产生 `tool.call.started` 或调用 handler；再将现有 Policy 接缝接入循环，完成时保存决策证据和零执行证明。
- [x] 2.2 先补充 HITL 暂停与恢复测试，覆盖审批请求创建、绑定摘要、过期、拒绝、撤销、租约竞争和 approval-id 至多一次；再复用 `ApprovalService`、grant/lease 与 `call_approved()`，完成时证明无第二套审批状态机。
- [x] 2.3 先补充普通和审批执行路径的一致性测试，证明两条路径都经过 Registry 防御性校验、Policy/HITL 门禁和同一结果守卫；再收敛共享编排，完成时用参数化测试覆盖两种入口。

## 3. 不可信结果与循环上限

- [x] 3.1 先补充工具成功、超时、取消、输出超限、schema 不符和敏感字段清理测试；再实现统一结果守卫，完成时证明原始工具输出不会直接进入下一轮模型消息。
- [x] 3.2 先补充上下文投影测试，断言守卫后的结果以 `untrusted` `ContextFragment`、稳定 `source_ref` 与 artifact 引用进入 `ContextAssembler`，并受现有截断和追踪规则约束；再实现投影，完成时保存组装追踪断言。
- [x] 3.3 先补充 `ModelToolLoopLimitOverrides` exact DTO 及最大回合数、总 token、总成本、单次输出和 duration/deadline 边界测试，覆盖DTO缺省、五项null继承、合法逐项缩小、缺失/额外/bool/NaN/Infinity/负数/扩大值、caller自报时间和approval/reload/resume不重置；再从绑定 descriptor 与受信时钟冻结边界快照并逐轮累计，完成时证明任一非法值零claim/client/provider/tool副作用，任一上限命中确定性终止且不再发起模型或工具调用。

## 4. 事件、回归与架构验证

- [x] 4.1 先补充 CanonicalEvent 生产测试，覆盖模型回合、解析、策略、审批、工具开始/完成/失败、上下文组装和终止；再接入事件生产者，完成时证明事件顺序、关联标识和失败短路与实际副作用一致。
- [x] 4.2 运行普通回答、结构化输出、流式、现有工具、审批、取消和预算路径回归，并执行类型检查与架构依赖守卫；完成时保存命令、退出码与测试计数，且不进行真实提供方或真实工具调用。
- [x] 4.3 更新受影响的公开 API 与维护文档，核对 `REQ-029`、`AC-106`、`AC-107`、`AC-108`、`MOD-006` 和本 change 场景逐项可追踪；完成时执行文档契约测试并保存通过证据。
