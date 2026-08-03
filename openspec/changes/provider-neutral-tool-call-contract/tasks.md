## 1. 公共契约与适配边界

- [ ] 1.1 先在公开模型客户端接缝补充失败测试，证明 `ModelTurnResult` 只能在最终回答与工具意图之间二选一，并覆盖未知判别值、混合载荷、缺少必要字段和适配器原始字段泄漏；再实现提供方中立的判别联合，完成时保存对应单元测试通过证据。
- [ ] 1.2 先为适配器候选与核心归一化边界补充契约测试：adapter 只返回工具名、参数、schema identity、provider/model 与单次 usage/attempt，不携带 `loop_id`、`turn_ordinal` 或 `tool_call_id`；核心验证候选后才从受信 loop/turn/catalog/arguments 生成稳定 `tool_call_id`。再实现 `ProviderToolIntentCandidate -> ToolIntent` 映射，完成时证明身份不可由 provider/调用方自报且核心层不依赖提供方专属类型。
- [ ] 1.3 先为模型使用量与证据写入补充失败测试，证明工具意图回合和最终回答回合同样产出一次且仅一次 `ModelUsageRecord` 与审计证据；再复用现有用量写入路径，完成时提供成功、适配失败和重复提交测试证据。
- [ ] 1.4 先为tool-intent model turn的既有`model.invoke` Policy/HITL补充allow、deny、require-approval waiting、matching approved continuation、绑定篡改和crash replay失败测试；再复用原approval/checkpoint/grant seam，完成时证明批准恢复沿用同一usage/operation/request/route/catalog/turn identity、provider至多调用一次，且工具执行approval、claim、handler与`tool.call.*`始终为零。

## 2. 工具目录与只读解析

- [ ] 2.1 先为 `tool-catalog-v1` 与公开 exact `ToolCatalogSelection` 补充契约测试，覆盖配置 `tool_allowlist` → descriptor `tool_policy.allowed_tools` 的逐值同序投影、顶层 `allowed_tools` 未知字段拒绝、与 Registry 已注册工具的交集、确定性排序、tool name、input schema ref/version/digest、action/resource、catalog ordinal，以及选择参数缺省为完整目录、显式空 tuple、唯一保序子集和未知/重复/重排/额外字段/塞入 `ModelRequest` 的零副作用拒绝；再实现独立 bound seam 参数与 exact 目录投影，完成时提供结构断言与 digest 稳定性证据且 `ModelRequest` 保持不变。
- [ ] 2.2 先为 `single-user-text-with-tool-catalog/v1` 与 `model-catalog/v2` 补充 red contracts：固定 canonical catalog golden vector，覆盖 singleton capability、单 route/attempt、空 fallback/classifier、repair 0、跨 capability 结果拒绝、no-tools/with-tools shape mismatch、超长 schema/catalog、checked arithmetic/预算不足、approval/replay identity 和恢复 catalog 漂移，所有发送前失败路径 provider/client/usage claim 计数为零；再实现 catalog request projection、v2 loader 与可信输入预算绑定，完成时逐值证明 route/snapshot/operation/approval/evidence 一致。
- [ ] 2.3 先为 Registry 只读解析接缝补充失败测试，证明解析未知工具、版本不匹配和参数 schema 不合法时不会调用 handler、Policy 或产生执行事件；再实现独立于执行的 `resolve` 能力，完成时以调用计数和事件断言证明零副作用。
- [ ] 2.4 先补充防御性执行测试，证明 `ToolRegistry.call()` 与 `call_approved()` 即使接收已解析工具也会重新校验身份、版本和参数；再接入共享校验器，完成时证明绕过目录或篡改参数不能执行 handler。

## 3. 回归与架构验证

- [ ] 3.1 补齐普通回答、结构化输出、流式输出、现有普通工具调用和审批工具调用的回归测试，证明新增联合类型不改变既有公开语义；完成时记录相关测试文件与通过命令。
- [ ] 3.2 运行受影响模块的静态检查、类型检查和测试，并执行架构依赖守卫，证明提供方类型未越过 adapters 边界且 Registry 解析未反向依赖 runtime；完成时保存命令、退出码与测试计数。
- [ ] 3.3 更新受影响的公开 API 与维护文档，核对 `REQ-029`、`AC-104`、`AC-105`、`MOD-006` 和本 change 的场景逐项可追踪；完成时执行文档契约测试并保存通过证据。
