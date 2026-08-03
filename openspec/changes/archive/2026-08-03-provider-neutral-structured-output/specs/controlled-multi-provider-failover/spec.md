## ADDED Requirements

### Requirement: Phase19结构化调用不进入跨provider route chain
`structured_output` capability SHALL 只允许legacy单route。Agent原始policy只要显式声明任意非空`fallback_routes`就保持Phase18.2 route-chain identity；无论原始列表含一个还是多个route，也无论request是否缩权到一个candidate，Router/invocation MUST 在usage claim、reservation、attempt identity、permit、client和provider副作用前以`model.structured_route_not_allowed`拒绝。Harness不得为结构化调用删减、重排、试探、降级或自动推进Phase18.2 route chain；fake不得作为真实结构化调用的隐式尾项。

#### Scenario: 显式route-chain结构化请求零调用拒绝
- **WHEN** Agent policy显式声明一个或多个`fallback_routes`，包括request缩权后只剩一个candidate的情况，并调用bound structured seam
- **THEN** 所有provider调用数都为零，usage claim与route-chain state不被创建或改写，显式chain不降级为legacy，调用返回稳定route-not-allowed

#### Scenario: Text route-chain保持原语义
- **WHEN** 同一Agent执行既有`text_completion`或`text_stream` route-chain
- **THEN** Phase18.2候选顺序、not-started proof、retry/fallback、budget transfer和recovery SHALL 保持不变

#### Scenario: Structured repair不跨candidate
- **WHEN** 未声明`fallback_routes`的legacy单route structured调用首个结果invalid且允许repair
- **THEN** 所有repair requests SHALL 使用同一冻结deployment/provider/model；任何切换候选尝试都fail closed
