## 1. 公开 seam 红灯与依赖边界

- [x] 1.1 从`build_execution_context()`取得`BoundModelInvocationService.complete_structured`，建立schema success公共seam红灯并保存修复前命令、退出码和失败断言。
- [x] 1.2 建立schema identity稳定/漂移、nested extra、remote/recursive/unresolved ref与Registry原子失败红灯。
- [x] 1.3 建立两个不同`provider_id`的structured doubles，通过冻结`ModelStructuredProvider.prepare_structured`、`PreparedStructuredModelCall.send_structured/aclose`、核心公开`StructuredProviderPrepareError|StructuredProviderCallError`与`StructuredProviderCandidate` exact seam覆盖candidate字符串/object、identity/provider/model漂移、sole attempt usage nullable、candidate重复顶层计量拒绝、invalid、extra、capability/protocol unsupported统一映射、显式route-chain双候选及请求缩权后单候选仍拒绝、repair-policy越权、repair success/exhaustion、核心repair×transport双层推进、每对ordinal fresh prepared、single-request send、send后零retry、claim后retryable/nonretryable/未知prepare失败、not-started proof/retry耗尽、send前/中/后取消、close-before-send/after-candidate/after-error、确定provider失败、unknown与SDK类型不越界；另在`tests/contracts/test_provider_neutral_structured_policy_contracts.py`从公开bound seam建立Policy DENY零调用、REQUIRE_APPROVAL零claim/零send、绑定usage/operation/request/schema/repair的exact arguments golden vector、durable grant恢复，以及usage/operation/request/schema/repair/grant/lease、record/checkpoint、extra/missing/type drift单项与组合篡改拒绝红灯，单独证明只同步改写两份continuation identity也在provider前关闭失败。
- [x] 1.4 建立direct/allocation预算、cost关闭但catalog/source identity保留、价格/source半pair公开拒绝、exact/conflict replay、failed exact replay、durable started与send边界的nullable-count unknown/recovery、mark commit ack未知保留reservation/owner围栏及durable payload篡改红灯，分别标明SQLite/PostgreSQL节点。
- [x] 1.5 建立text complete/approved、stream/approved、Phase18.2 route-chain和`ModelResponse.output_text`兼容红灯/基线，证明新增断言来自公开行为而非私有函数。

## 2. Schema identity与Registry catalog生产实现

- [x] 2.1 在核心新增provider-neutral schema identity/definition/request/final result、adapter→core唯一`StructuredProviderCandidate`、带exact cleanup status的`StructuredModelAttemptEvidence`/structured attempt/not-started proof DTO、`StructuredProviderPrepareError|StructuredProviderCallError`、canonical JSON bytes与稳定错误，并由`models/__init__.py`作为公共模型出口显式导出；candidate exact字段、递归JSON值、sole attempt计量真相源、异常字段/适用边界和SDK拒绝规则按design冻结，所有维护说明以中文为主，text attempt序列化形状与adapter私有错误兼容入口保持不变。
- [x] 2.2 将`jsonschema>=4.26.0,<4.27`声明为直接runtime依赖，刷新lock metadata并核对现有license清单、依赖身份和build边界。
- [x] 2.3 实现根object、本地非递归ref内联、递归`additionalProperties=false`、schema位置感知封闭allowlist、拒绝format/contains/条件/unevaluated系列、remote/recursive/unresolved拒绝与Draft 2020-12检查的唯一schema compiler/validator；把compiler、validator与稳定issue投影从超限`structured.py`行为等价迁入私有`structured_schema.py`，保持`models/__init__.py`公共出口、canonical bytes、错误语义和调用方不变，并让两个文件最终均不超过500有效代码行。
- [x] 2.4 扩展schema loader一次返回受控BaseModel schema定义，并让任一非法sibling在导入/构造catalog时整体回滚。
- [x] 2.5 扩展`registry/{__init__,descriptor,_loader,registry}.py`：Agent descriptor保留旧ref并公开`output-schema-identity-v1`，AgentRegistry持有只读definition索引并提供按Agent exact解析seam。
- [x] 2.6 运行Registry聚焦合同，确认身份稳定、严格extra、全量原子失败与既有agents list/CLI/API输出兼容；精确更新所有直接构造`AgentDescriptor`或断言公开descriptor exact shape的冻结owner，不得用default/null/伪digest绕过必填`output_schema_identity`。

## 3. Typed config、route与联合reservation生产实现

- [x] 3.1 扩展deployment capability为`structured_output`并增加非bool `0..2` repair上限，完成load/reject/reload合同。
- [x] 3.2 扩展`models/router.py`及current/snapshot contracts，将schema、repair、transport attempt与`provider_request_limit`冻结进route plan、route evidence和semantic replay identity。
- [x] 3.3 以checked arithmetic实现`transport_attempt_limit * (1+repair_limit)`的token/cost总reservation；结构化每attempt固定按`max_prompt_utf8_bytes+catalog.input_envelope_token_bound`预约，并在planning零调用构造initial及所有允许repair ordinal的最大code集合完整`structured-provider-prompt-v1`检查cap。Price values与price source identity分别成对校验，cost关闭仍允许完整catalog/source identity；覆盖direct和delegation allocation，任一半pair、超限或预算不足均从公开structured seam在provider/claim前拒绝。
- [x] 3.4 结构化planning只要发现Agent policy显式声明任意非空`fallback_routes`，即使request缩权后只剩一个candidate，也在usage claim/reservation/attempt/permit/client前返回`model.structured_route_not_allowed`；不改写、不启动route-chain，也不降级为legacy单route。
- [x] 3.5 运行current/snapshot、route identity、shared-budget direct/allocation与Phase18.2兼容聚焦合同。

## 4. Bound invocation与provider adapter生产实现

- [x] 4.1 按单一职责把structured控制器收敛为`models/_invocation_structured.py`入口/preflight/Policy编排、`_invocation_structured_support.py`纯planning/attempt/recovery辅助、`_invocation_structured_execution.py` transport×repair执行、`_invocation_structured_result.py` result/evidence/replay/final settlement投影；`storage/_structured_usage_evidence_repository.py`只绑定started replay seed。共享兼容owner中，`_invocation_chain_base.py`只同步可选structured replay结算签名，`_invocation_streaming.py`只同步共享finalization签名并直接复用既有event helper，`_router_snapshot_chain.py`只把冻结的`max_structured_repair_attempts`带入既有chain snapshot；三者不得实现structured chain或structured streaming。交付manifest内全部Python生产文件均不超过500有效代码行，且不得改变claim/reservation/permit/client/durable-started/send/cleanup/settlement顺序、UoW或公共出口。向ModelInvocationService注入只读output schema resolver，并让`invocation.py`只以`BoundModelInvocationService.complete_structured`绑定可信run/agent/schema/operation identity后窄委托。补齐既有`model.invoke` Policy/HITL前置：DENY零调用，REQUIRE_APPROVAL冻结原usage/operation/schema/repair与arguments hash；在`models/_invocation_approval_identity.py`实现版本化exact arguments/continuation、canonical hash、record/checkpoint/grant/current bound input交叉校验及golden vector，`complete_structured_approved`只从durable continuation恢复并验证active grant/lease，只绕过一次soft gate且重算hard bounds。不得把控制器塞入既有complete/stream façade，不做Phase21通用重构。
- [x] 4.2 新增runtime-checkable provider-neutral structured provider/prepared协议，逐字实现design冻结的`prepare_structured(request, *, plan, schema)`、`send_structured(*, provider_prompt, repair_ordinal, transport_ordinal)`和`aclose()` async签名、唯一`StructuredProviderCandidate`返回及核心公开prepare/call异常；每对ordinal fresh prepare/close，send最多调用一次并只执行一次外部request，candidate/call error只携带一个`attempt=1`的provider-local计量事实；协调器在usage claim/reservation/client前把deployment capability或protocol缺失统一映射为`model.structured_capability_unsupported`，Router通用错误不逸出，普通text provider协议与adapter私有错误入口保持不变。
- [x] 4.3 实现Fake structured provider的显式有限脚本，只服务测试/local配置，不成为真实provider隐式后备。
- [x] 4.4 在`adapters/models/pydantic_ai.py`、`adapters/models/_pydantic_ai_client.py`与私有`_pydantic_ai_structured.py`实现普通`Agent.run(retries=0)`及single-attempt transport的结构化生成边界；私有helper独占SDK structured事件归一化与单次prepared call。Prepare/call失败只抛核心vendor-neutral异常，candidate/错误sole attempt归一化全部计量；禁止ToolOutput/工具执行、任意SDK对象`str()`和SDK/client/adapter内部隐藏repair或transport retry；既有text adapter retry与私有错误兼容入口不变。
- [x] 4.5 在`models/_invocation_structured_execution.py`实现核心拥有的transport attempt与repair轮次双层控制，并由`_invocation_structured.py`入口编排器窄调用：每对ordinal fresh prepare/close、single-request send、provider-local attempt映射为连续global attempt、同provider/model；只有retryable prepare错误在send前推进，任何send后事实都停止transport retry；initial/repair都以`structured-provider-prompt-v1` exact canonical JSON作为唯一provider字符串，repair仅携带上一轮稳定codes；严格执行单一deadline/backoff/次数并冻结send前/中/后取消及close-before-send/after-candidate/after-error的failed/needs-review优先级。Not-started proof只排除provider request；durable mark commit ack未知即使send为零也必须needs-review并保留owner reservation。
- [x] 4.6 核心validator在success settlement前复核schema/value/extra/canonical text，生成valid result或稳定invalid/extra/exhausted错误。
- [x] 4.7 运行两个provider doubles、Pydantic AI窄protocol、success/invalid/repair/exhaustion/unsupported、Policy DENY/REQUIRE_APPROVAL/批准恢复/篡改负路径，以及显式route-chain双候选和缩权后单候选都拒绝的聚焦合同；有效代码行机械断言必须解析living plan完整生产changed-file manifest并检查其中全部Python文件，禁止手写阶段文件白名单。另以AST负向断言锁定`IdentityRuntime`、`AgentModelPolicy`、`SettlementStart`、`PreparedStructuredModelCall`和参数完整的`StructuredPromptBuilder`，禁止这些可信协作者退化为对象级`Any`或`Callable[..., str]`。

## 5. Usage、evidence、replay与recovery生产实现

- [x] 5.1 扩展ModelResponse nullable structured result并保持text response exact兼容；实现`structured-canonical-json-v1`以及`structured-output-operation-v1`、`structured-output-prompt-v1`、`structured-output-replay-v1` exact preimage，锁定nullable/unknown规则、终态union和spec golden vector，使valid canonical`output_text/value`与replay identity逐值绑定。
- [x] 5.2 扩展`models/usage.py`的started/final usage decision structured摘要校验与构造，只从每个sole local/global attempt取得token/cost/latency并把全部transport/repair attempts计入budget charge；拒绝candidate重复计量、send后retry和cleanup/terminal矛盾，同时保持embedding/text decision兼容。
- [x] 5.3 扩展`models/_settlement_contracts.py`、`models/_settlement_evidence_models.py`、私有`models/_structured_settlement_evidence_models.py`、`models/_settlement_validation.py`、`models/_settlement_evidence_validation.py`与publication validator；私有structured evidence models独占started/final投影，实现`structured-output-evidence-v1`及带`not_applicable|completed|failed|unknown` cleanup status的`structured-output-attempt-v1`/核心发送前proof exact摘要。只消费validator直接项并形成稳定issues，锁定终态，交叉验证response/evidence的schema/prompt/replay/status/error/value/count/双ordinal/trigger/codes/proof/attempt/charge/cleanup；验证candidate无重复计量、每个send边界仅一个local attempt、任何send后路径不再retry、prepared/cleanup/terminal组合唯一，send后cancel/timeout/unknown或cleanup failed/unknown只能needs-review，并拒绝未知字段和矛盾终态。
- [x] 5.4 扩展semantic request、usage replay seed与exact/conflict replay；structured approval arguments hash必须绑定原usage/operation/request/schema/repair，continuation必须exact耐久冻结并恢复原identity；usage/operation/request/schema/repair/grant/lease任一漂移及record/checkpoint identity同步篡改均在provider前拒绝。旧durable text结果不得由current schema补齐，新structured结果不得被旧binary形状伪装。
- [x] 5.5 扩展started/cleanup/provider result及durable mark commit ack unknown路径，使usage claim、direct/allocation与owner ledger一致进入needs-review且不重发、repair、退款或发布valid结果；mark ack未知仍保存可证明的零provider request，但actual token/cost保持null。
- [x] 5.6 在现有schema上完成SQLite structured success/exhausted/failed/unknown/exact/conflict、双ordinal/trigger/validation codes/not-started proof篡改与recovery合同；若现有列不足，暂停并修订契约而不是偷加migration。
- [x] 5.7 在真实PostgreSQL测试容器或仓库既有DSN入口逐值运行同组durability合同，记录节点计数并在结束后清理临时资源。

## 6. Eval、示例与维护文档

- [x] 6.1 新增结构化eval case/metric，从公共bound seam验证valid得分、invalid/unknown/needs-review不记成功，`make eval`继续零网络。
- [x] 6.2 公共structured seam稳定后，在`templates/service-app/agents/examples/dev_assistant/{schemas,agent}.py`把宽松`dict[str, object]`迁移为仅覆盖现有read/write/shell完成载荷的严格`DevAssistantToolResult`；在`templates/service-app/agents/examples/rag_assistant/{schemas,agent}.py`把`assembly_truncation: dict[str, int]`迁移为两个互斥exact object：无字段的`RagAssemblyTruncationEmpty`固定为`{}`，只允许`status=no_source`且`assembly_id/model_provider=null`、citations/source refs为空，并由未创建Context Assembly的本地无检索结果分支构造；`RagAssemblyTruncation`只含`input_count/retained_count/truncated_count/dropped_count/used_tokens/fragment_count`六个必填非bool非负整数，只允许`status=completed`且`assembly_id/model_provider`非空，并从既有`ContextAssemblyResult.truncation_summary`同名字段构造。输出DTO必须拒绝部分六字段、状态与变体混搭及额外字段，不得用六个零计数伪装无来源结果。示例schema只作为Registry输入，不定义或导出SDK核心类型。用`tests/contracts/{test_agent_registry_schema_contracts,test_dev_approval_flows_contracts,test_example_agent_flows_contracts,test_example_eval_migration_contracts,test_example_agent_policy_provider_contracts,test_retrieval_doctor_example_contracts}.py`与`templates/service-app/tests/test_app_surface.py`证明全量Registry原子加载、Dev Assistant既有输出引用/离线工具流程、RAG无来源精确`{}`、有来源六个组裁计数、citation/trust/assembly/model/trace与eval均不退化，并覆盖部分/mixed状态负路径；不新增工具执行能力或改写Context Assembly耐久schema。Ticket triage的最终输出含调用后trace字段，本阶段不得迁移或创建第二套schema控制面。
- [x] 6.3 更新`docs/acceptance-matrix.md`为REQ-028/AC-096～103分别列出精确生产producer、CI job、测试节点和evidence artifact，REQ-028/AC-097必须包含Policy DENY、REQUIRE_APPROVAL与批准恢复节点；生产与测试文件分栏盘点。
- [x] 6.4 同步API Contract、DEV-PLAN、Product-Spec勾选/CHANGELOG、living plan Progress/Discoveries/Decision/Handoff，以及`docs/{adapter-contracts,adapter-contracts.zh-CN,extension-guide,extension-guide.zh-CN,building-an-agent,building-an-agent.zh-CN}.md`，不做等价重写。
- [x] 6.5 完成维护语言自检、vendor/Pydantic AI类型边界检查、全部已注册output schema的递归`additionalProperties=false`盘点和changed-file manifest对账，确认只有已冻结的Dev/RAG兼容迁移，且未实现structured streaming/fallback/tool/Phase21非目标。

## 7. 聚焦与全仓验证

- [x] 7.1 重跑公开seam red命令取得green，记录同一节点修复前后退出码、通过数与关键断言。
- [x] 7.2 运行Phase19 contract/integration/eval聚焦集合及SQLite/PostgreSQL持久化集合，保存逐命令退出状态和节点计数。
- [x] 7.3 运行`make quality`、`make test`、`make eval`、`make smoke-local`、`make smoke-service`（触及service composition时）、`make build`、`make license-check`与`make acceptance-validate-check`；任何跳过/失败按真实状态记录。
- [x] 7.4 运行`openspec validate provider-neutral-structured-output --type change --strict`、`openspec validate --all --strict`与`git diff --check`，确保所有artifacts中文为主且可解析。
- [x] 7.5 逐项对照AC-096～103、MOD-005与Phase19交付清单，分开统计生产、测试、eval和文档文件；未建立的live前置只做零调用预检并标`hosted-unverified`。

## 8. Ready-to-archive状态收口

- [x] 8.1 在最终内容冻结前把本文件所有真实完成项勾选，记录task总数/完成数、OpenSpec status和每项剩余风险；不得提前勾选审查或未运行门禁。
- [x] 8.2 计算并记录最终HEAD、完整changed-file manifest与冻结聚合SHA-256；任何实质改动使旧冻结身份和实现审查票失效。
- [x] 8.3 将最终验证命令、退出码、关键计数、schema/终态/AC到生产与测试映射、契约审查和实现审查分离结论写入living plan Handoff Snapshot。
- [x] 8.4 核对`openspec list --json`仍只有本active change、未sync/archive，Git无commit/amend/push/release/deploy，真实provider调用为零，最终状态只写`ready-to-archive`。
