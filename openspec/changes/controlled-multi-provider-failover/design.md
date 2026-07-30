## Context

Phase 18 已提供 typed deployment、冻结单 route、同 deployment 的 model fallback、共享预算、可重放 usage outbox、受控 Pydantic AI adapter 与默认离线 fake；Phase 18.1 又冻结了 `text_stream` 的 delta、关闭证明、unknown/needs-review 与首 delta 后不可重放围栏。当前缺口不是“多放几个 provider 实例”，而是把一次调用的不可变身份从单个 `ModelRoutePlan` 扩展为有序 route chain，并让每次候选切换与预算影响成为 SQLite/PostgreSQL 都可恢复的耐久事实。

现有 direct/allocation shared-budget claim 的 identity、`reserved_tokens`/`reserved_cost` 和 `side_effect_state` 只表达单 route。若直接释放旧 claim 再创建新 claim，会出现释放后崩溃、重复预约、两个 stable key 或 unknown 被退款等窗口；若只在内存改 plan，恢复会从当前配置重新选候选。因此本变更需要一个新的持久化 route-chain 状态，但继续复用原 claim、ledger、usage outbox 和 stream outbox，不创建第二套预算账本。

供应商 SDK 继续只存在于 adapter 内。`ControlledOpenAIClientFactory` 已按 `deployment_id + endpoint_policy_digest + model_catalog_digest` 缓存 lease，transport 又在 socket send 前重建 credential header；本变更保留这条边界，并把 Router/provider 的查找从“只有 provider kind 身份”收紧为“候选 deployment 选择 provider kind、factory 再按完整 route identity 取得独立 lease/Bulkhead”。

## Goals / Non-Goals

**Goals:**

- 在任何预算、client、DNS/HTTP 或 provider 副作用前冻结有序、可摘要、可恢复的 `(deployment_id, model_id)` route chain。
- 让 Agent policy 决定最大候选集，请求只能按原顺序选择一个非空子序列；每个候选重新解析自己的 endpoint、credential、catalog、capability、timeout/retry、Bulkhead 与价格。
- 只在当前候选具有 `client_not_started`，或端点绑定 classifier、显式状态白名单与零生成/计量事实共同给出 `trusted_business_not_started` 时，以一个 UoW 原子完成候选收敛、reservation transfer 和下一候选激活；发送后的含糊 timeout/response、unknown、response identity、usage、text 或 delta 永久停止跨 provider failover。
- 让普通 completion 与 streaming 共用 chain identity、候选状态、attempt evidence、预算/恢复围栏；首 delta 后永久禁止后续 provider。
- 保持默认 fake/local/test/eval/smoke-local 零网络；live smoke 前置不足时零调用并准确输出 `hosted-unverified`。

**Non-Goals:**

- 不做动态发现、健康权重、负载均衡、热重载控制面、自动成本套利、多区域运维或跨 provider 对账。
- 不做 structured/tool-call/reasoning delta 的跨候选拼装、repair 或 fallback。
- 不新增 HTTP endpoint、provider cursor 或业务请求可控的 endpoint/credential/SDK 字段。
- 不把 fake 追加到真实 chain，不读取/输出真实 secret，不执行 sync/archive/push/release/deploy，不启动 Phase 19；契约严格 `1+2` 通过后只允许用户已授权的一次本地契约提交。

## Decisions

### 1. 用显式 route ref 和不可变 chain DTO 替代跨 deployment 的 model ID 推断

新增 `ModelRouteRef(deployment_id, model_id)`、`ModelRouteCandidate(ordinal, route)` 与 `ModelRouteChainPlan`。`ModelRouteChainPlan` 为 frozen DTO，公开不可变 identity 的 exact shape 为 `schema_version="model-route-chain-v1"`、`chain_id: str`（64 位小写十六进制）、`capability: "text_completion"|"text_stream"`、`candidate_count: int`（非 bool，1～8）和 `candidates: tuple[ModelRouteCandidateIdentity, ...]`。每项 candidate exact fields 为 `ordinal: int`（从 1 连续）、`deployment_id/provider/model: str`（非空）、`route_digest/endpoint_policy_digest/model_catalog_digest/retry_policy_digest/bulkhead_policy_digest: str`（64 位小写十六进制）、`credential_ref: str|null`、`model_catalog_ref/model_catalog_version: str`、`reserved_token_bound: int`（非 bool、非负）与 `reserved_cost_bound: finite float|null`（非负）；拒绝 unknown fields、重复 ordinal、bool 冒充 int、负数、NaN/Inf 和 count/list 不一致。每个 candidate 的私有 `route` 仍是完整 `ModelRoutePlan`，因此现有 endpoint、catalog、公式、retry 和 Bulkhead 校验继续逐候选执行。

`AgentModelPolicy.fallback_routes` 是显式启用 chain mode 的非空有序 route refs。为保持已归档 Phase 18/18.1 与模板兼容，缺少该字段的旧 descriptor 继续走既有单 route `plan()` 和同 deployment planning fallback，不获得运行时 chain/failover 语义。显式 chain 仍保留既有必填字段，但只作为首候选的确定性兼容投影：`provider` 等于首 deployment 的 provider kind，`deployment_id/default_model` 等于首 ref，`allowed_models` 按 route 顺序去重投影首 deployment 中已列 models，`fallback_models=[]`；任何不一致在 loader 前置拒绝。这些字段不授权后继，public summary 同时保留完整有序 refs。请求新增可选 `route_refs`，且只有 Agent 显式声明 `fallback_routes` 时合法；省略表示使用 Agent 冻结顺序，提供时必须是该顺序的非空子序列，不允许重复、插入或重排。旧 `deployment_id/provider/model` 字段仅能进一步收窄到一个兼容 ref，不能与 `route_refs` 形成第二套选择权。

`chain_id` 的唯一 preimage 是下列 `model-route-chain-id-v1` exact object；不得从展示摘要、对象 repr、未列字段或 map 插入顺序推导：

```text
{
  schema_version: "model-route-chain-id-v1",
  capability: "text_completion|text_stream",
  candidate_count: <1..8 non-bool int>,
  agent_model_policy: {
    deployment_id: <原始 Agent descriptor 的 fallback_routes[0].deployment_id>,
    provider: <该原始首 deployment 的 provider kind>,
    allowed_models: [<原始首 deployment 中按原始 fallback_routes 顺序去重的 model>...],
    default_model: <原始 Agent descriptor 的 fallback_routes[0].model_id>,
    fallback_models: [],
    fallback_routes: [{deployment_id: <nonempty str>, model_id: <nonempty str>}...]
  },
  request_bounds: {
    prompt_utf8_bytes: <nonnegative non-bool int>,
    max_output_tokens: <positive non-bool int>
  },
  candidates: [
    {
      ordinal, deployment_id, provider, model,
      route_digest, endpoint_policy_digest, model_catalog_digest,
      retry_policy_digest, bulkhead_policy_digest,
      credential_ref: <str>|null,
      model_catalog_ref, model_catalog_version,
      reserved_token_bound: <nonnegative non-bool int>,
      reserved_cost_bound: <canonical decimal str>|null
    }...
  ]
}
```

`agent_model_policy` 必须逐值复制请求缩权前、loader 已验证的原始 Agent descriptor：兼容投影永远来自原始 `fallback_routes[0]`，完整 `fallback_routes` 也永远是 Agent 最大授权集；request 不得把该对象改写成其所选子序列的首项。`fallback_routes`、`allowed_models` 和 `candidates` 保持上述冻结顺序，其他数组一律不允许出现；request `route_refs` 的缩权结果由 `candidates[]` 唯一表达，不在 preimage 建立第二份列表。例如 Agent 为 `[A,B]`、request 为 `[B]` 时，`agent_model_policy` 仍保存 A 的兼容投影与 `[A,B]`，而 `candidate_count=1/candidates=[B]`。Canonical bytes 唯一使用 UTF-8、`ensure_ascii=false`、`sort_keys=true`、紧凑 separators `(',', ':')` 与 `allow_nan=false`；null 必须编码为 JSON null，整数必须编码为 JSON number且拒绝 bool。`reserved_cost_bound` 从受信 Decimal 生成无 exponent、无无意义尾零、无负零的十进制字符串，cost disabled 才允许 null；所有 string 使用 loader 已验证的 canonical 值，不再做环境相关正规化。对这些 bytes 取 SHA-256，结果就是 64 位小写十六进制 `chain_id`。摘要不含完整 URL、credential value、header、SDK object、prompt 内容或当前余额。

当前配置规划与 snapshot 恢复必须复用同一 serializer，并以同一组 golden vectors 证明逐字相同；其中必须包含 Agent `[A,B]`、request `[B]` 的逐字节向量和“把兼容投影错误改写为 B 必须摘要不匹配”的负例。恢复还必须重算并匹配已保存 `chain_id`。候选删除、插入、重排，capability、Agent policy、prompt byte 上限、输出上限或任一 candidate 字段变化都必须改变摘要；unknown field、nullable 表示变化、float/Decimal 直接 JSON 编码及 current-config 补值都关闭失败。选择显式 DTO 而不是让 `ModelRoutePlan` 递归持有 fallback，是为了保持单候选执行 seam 可复用，并让测试能分别验证 planning 与 invocation。

### 2. 规划冻结所有候选，运行时只推进 ordinal

Router 增加 `plan_chain()` / `plan_chain_from_snapshot()`；既有 `plan()` 只保持 legacy 单 route 返回类型，显式 chain 的 public `route()`/invocation 必须走 `plan_chain()`，不能把首候选投影降级成单 route。当前配置与 `budget-tree-v2` 恢复都必须构造相同 chain：先验证请求为 Agent route refs 的有序子序列，再为每个候选调用现有 route 公式，逐项验证 referenced deployment/provider/catalog/credential/capability/价格并冻结 candidate ordinal。恢复只读取 durable snapshot 中已冻结的 route、policy 与 chain，不读取 reload 后新增/重排的 deployment、credential 或价格。

静态 capability、catalog、输入上限、公式或 frozen hard-budget 不合格候选记为封闭 `static_ineligible`，零 client/permit/reservation/provider 副作用后可继续。候选在 soft review threshold 明确选择有限 fallback，或在 owner lock/CAS 内以自身冻结上界替换当前 impact 时余额不足，分别记为 `budget_ineligible/reason=soft_budget|balance`；两者同样零 reservation、permit、client、provider attempt。这里的 `soft_budget` 只表示既有候选级 soft-threshold 的“选择 frozen fallback”结果，不等于 `PolicyEngine` 的 `deny` 或 `require_approval`，不能绕过审批；`balance` 只表示该候选在当次原子 reservation 判定中的 current-owner 余额不足，不把余额或 limit 公开。全链安全耗尽返回 `model.route_chain_exhausted`，公开 cause 仅为枚举 `capability|catalog|input_bound|hard_budget|soft_budget|balance|not_started_failure` 与 ordinal，不含 endpoint、credential、余额、价格或 raw provider error。全局 policy deny、审批 grant 无效和 identity 冲突仍按既有稳定错误终止，不能借换 provider 绕过授权。

### 3. 一笔 usage/预算 stable key 持有整条 chain，migration `0017` 保存推进事实

继续使用同一 `usage_call_id`、一笔 direct/allocation claim、一笔 `MODEL_USAGE` outbox，以及 streaming 时关联的一笔 `MODEL_STREAM` group。Legacy单route与embedding identity继续逐字使用`budget-operation-v1`，字段和canonical hash完全不变；显式route chain唯一使用`budget-operation-v2`。v2在v1受信上下文字段之外强制增加参与hash的`route_chain_digest`与`route_candidate_count`，且既有provider/model、price source与trusted bound兼容位固定投影冻结chain的ordinal 1 candidate，不随request后的预算选择、active/selected ordinal或恢复时余额变化改写；完整Agent/request授权、所有candidate identity/price/bound已由chain digest与tenant-scoped semantic request fingerprint共同绑定。v1不得携带v2字段，v2不得省略digest/count或降格为v1；相同stable key跨版本或任一字段不一致均在读取current balance前`budget.operation_conflict`。这样既保持旧hash逐字不变，又让route-chain exact replay不依赖重新评估哪一候选会被激活。

新增 migration `0017_model_route_chain_state`，为 `budget_operation_claims` 与 `delegation_budget_allocations` 增加 nullable `route_chain_state_json`。只有 `usage_kind=model` 的新 chain 调用可写；旧 row 保持 null。它是可变推进状态而不是 chain identity，exact shape 为：

```text
{
  schema_version: "model-route-chain-state-v1",
  chain_id: <sha256>,
  candidate_count: <positive int>,
  usage_call_id: <64-char lowercase sha256>,
  operation_identity_digest: <sha256>,
  active_ordinal: <1..candidate_count>|null,
  waiting_approval_ordinal: <1..candidate_count>|null,
  selected_ordinal: <1..candidate_count>|null,
  evidence_route_ordinal: <1..candidate_count>,
  delta_fenced: <bool>,
  attempt_lifecycle: [
    {
      attempt: <positive global int>,
      candidate_ordinal: <1..candidate_count>,
      attempt_identity_digest: <sha256>,
      lifecycle_state: "started|not_started_proven|unknown|settled",
      side_effect_state: "not_started|started|unknown|result_committed",
      request_sent: <bool>, http_response_observed: <bool>, http_status: <100..599>|null,
      response_identity_observed: <bool>, usage_observed: <bool>, text_observed: <bool>, delta_observed: <bool>,
      completion_observed: <bool>|null,
      not_started_proof_digest: <sha256>|null
    }
  ],
  current_reservation: {
    candidate_ordinal: <1..candidate_count>|null,
    token_bound: <nonnegative int>,
    cost_bound: <nonnegative finite float>|null
  },
  candidates: [
    {
      ordinal, deployment_id, provider, model,
      route_digest,
      state: "pending|static_ineligible|budget_ineligible|waiting_approval|active|not_started|completed|cancelled|unknown|denied",
      side_effect_state: "not_started|started|unknown|result_committed",
      reason: null|"static_ineligible|soft_budget|balance|client_not_started|trusted_business_not_started|approval_required|policy_denied|invocation_cancelled",
      request_sent: <bool>, http_response_observed: <bool>, http_status: <100..599>|null,
      response_identity_observed: <bool>, usage_observed: <bool>, text_observed: <bool>, delta_observed: <bool>,
      completion_observed: <bool>|null,
      not_started_proofs: [
        {
          attempt: <positive global int>,
          reason: "client_not_started|trusted_business_not_started",
          side_effect_state: "not_started|started",
          request_sent: <bool>, http_response_observed: <bool>, http_status: <100..599>|null,
          response_identity_observed: <bool>, usage_observed: <bool>, text_observed: <bool>, delta_observed: <bool>,
          completion_observed: <bool>|null,
          endpoint_policy_digest: <sha256>,
          classifier_ref: <string>|null, classifier_version: <string>|null,
          proof_digest: <sha256>
        }
      ],
      approval_request_binding_digest: <sha256>|null,
      approval_grant_binding_digest: <sha256>|null
    }
  ],
  transitions: [
    {
      sequence: <positive continuous int>,
      from_ordinal: <ordinal>|null, to_ordinal: <ordinal>|null,
      state: "activated|transferred|waiting_approval|approved|terminated",
      reason: "initial|client_not_started|trusted_business_not_started|approval_required|approval_granted|balance|policy_denied|route_exhausted",
      released_token_bound: <nonnegative int>, released_cost_bound: <nonnegative finite float>|null,
      reserved_token_bound: <nonnegative int>, reserved_cost_bound: <nonnegative finite float>|null
    }
  ]
}
```

`transitions[]` 除连续 sequence 外只允许下表组合；表中的 `current_bound`/`target_bound` 必须逐值等于事务前 current reservation/目标 candidate 冻结 reservation，`zero_bound` 精确为 `token=0/cost=null`。任何其他 state/reason、from/to、released/reserved 组合都关闭失败：

| state / reason | `from_ordinal` | `to_ordinal` | released bound | reserved bound | 同一 UoW 后置状态 |
|---|---:|---:|---|---|---|
| `activated/initial` | null | 首个 eligible ordinal | `zero_bound` | `target_bound` | target=`active`，active ordinal=target |
| `transferred/client_not_started\|trusted_business_not_started` | current ordinal | 首个 eligible successor | `current_bound` | `target_bound` | current=`not_started`，successor=`active` |
| `waiting_approval/approval_required` | active current ordinal、获批后balance不足的零impact anchor ordinal，或尚未激活时null | waiting ordinal | source为active current时`current_bound`，否则`zero_bound` | `zero_bound` | target=`waiting_approval`，waiting ordinal=target |
| `approved/approval_granted` | waiting ordinal | 同一 waiting ordinal | `zero_bound` | `target_bound` | candidate从`waiting_approval`直接变为`active`，active ordinal=target，waiting清空 |
| `transferred/balance` | 获批后balance不足的 ordinal | 首个 eligible successor | `zero_bound` | `target_bound` | from=`budget_ineligible/balance`，successor=`active` |
| `terminated/policy_denied` | active current ordinal或获批后balance不足的零impact anchor ordinal | denied successor ordinal | source为active current时`current_bound`，否则`zero_bound` | `zero_bound` | active current按可信actual-zero收敛（若存在），successor=`denied`，selected/active/waiting均空 |
| `terminated/route_exhausted` | current或获批后balance不足的 ordinal | null | current有reservation时`current_bound`，否则`zero_bound` | `zero_bound` | selected/active/waiting均空且提交exhausted terminal |

Transition source anchor固定为：尚未建立任何reservation/waiting时为null；已有active reservation时为该active ordinal；获批目标balance不足后为该`budget_ineligible/balance`零impact ordinal。前导或中间`static_ineligible|budget_ineligible`普通skip只写candidate state，不改变source anchor。因而初始扫描跨过任意前导skip后，allow仍写`activated/initial`的null→target，require-approval写`waiting_approval/approval_required`的null→target；deny不创建transition，只以denied candidate与failure收口。Active current或获批balance anchor跨普通skip后，allow、require-approval、deny、全耗尽分别只能使用表中的transferred、waiting、terminated-policy-denied、terminated-route-exhausted tuple。

首候选或任何尚未激活路径的 policy deny 不创建 coordination transition；初始扫描全部为 static/budget ineligible 时 transitions 也为空。`approved` 只属于 transition state，不是 candidate state或中间可恢复状态；成功 activation 在单一 shared-budget UoW 中直接完成 waiting→active，只追加一条 `approved/approval_granted`，不得再追加 `activated/initial`。获批目标 balance不足则直接 waiting→`budget_ineligible/balance`，保留 request/grant binding与既有 waiting transition，但禁止追加 `approved|activated`；后继allow只追加`transferred/balance`，后继require-approval只追加零released/reserved的`waiting_approval/approval_required`，后继deny只追加零released/reserved的`terminated/policy_denied`，无可继续候选只追加零释放的`terminated/route_exhausted`。同参 commit-ack replay必须返回原 sequence与完全相同的 transition数组，不得补写或改写 transition。

`usage_call_id` 在首次可信 bound entry、任何 policy或 coordination row前精确复用既有 `stable_usage_call_id(context, operation_key)` 的 `usage-v1` canonical。`operation_identity_digest` 对 `model-route-chain-operation-v1`、tenant id、run id、agent id、request id或空串、trace id、原始 operation key按此顺序以 `U+001F`连接后取 SHA-256。两者都固定为 64位小写十六进制；原始 key只进入私有 checkpoint，不进入该 JSON，恢复时必须从 checkpoint重算并逐值匹配。

该 DTO 拒绝 unknown fields、非法 enum、bool 冒充数字、负数、NaN/Inf、非连续 ordinal/transition、重复 ordinal/attempt 以及不可能的组合。`active_ordinal`、`waiting_approval_ordinal`、`selected_ordinal` 三者最多一个非空：`active` 只能对应 active ordinal 和非零/合法 current reservation；`waiting_approval` 只能对应 waiting ordinal、零 current reservation、非空 request binding digest 和空 grant binding digest；经审批激活的 active candidate 必须同时有两份 binding digest；未进入审批的 candidate 两者都为空。`completed` 只允许 selected ordinal；`cancelled` 与 `unknown` 都不允许 selected ordinal，其中 cancelled同时要求active/waiting为空且current reservation为canonical空，unknown保留最后 reservation。逐候选 `side_effect_state` 与 request/response 观察事实是该 ordinal 全部实际 attempt 的单调高水位；claim 的既有 `side_effect_state` 是整条调用的单调高水位，任一候选 started 后永久保持 started，直到可信 terminal 才进入 result_committed。`pending`、`static_ineligible`、`budget_ineligible`、`waiting_approval`、provider 前 `denied` 的 `side_effect_state` 必须为 not_started且 proof list为空；active 可为 not_started/started/unknown；completed与cancelled必须为result_committed，unknown必须为unknown。Cancelled唯一允许`reason=invocation_cancelled`，不追加新的coordination transition。

`attempt_lifecycle[]` 是判断“下一全局 attempt 是否尚未开始”的唯一耐久来源，不得用 candidate/claim 聚合高水位或 `not_started_proofs[]` 的缺席反推。记录按全链全局 attempt 从 1 连续且不可删除、覆盖、重排；`attempt_identity_digest` 是 `model-route-attempt-identity-v1` exact object经核心delta定义的`model-route-canonical-json-v1`所得 bytes 的 SHA-256，exact keys为schema version、chain id、usage call id、operation identity digest、candidate ordinal、global attempt、route digest、endpoint policy digest与retry policy digest。所有nullable字段保留、unknown/缺失字段关闭失败，并与核心delta的`d5591241…` golden vector逐字节一致。identity fields 一经创建永远不可变，相同 attempt 的同参 commit-ack replay返回原记录，任一 digest/ordinal/route/retry冲突均 `budget.operation_conflict`。

调用顺序按模式封闭：legacy单route保持`hard eligibility → policy/audit → reservation → Bulkhead permit → client lease → durable side_effect_started mark → send`；显式route chain唯一为`hard eligibility → candidate policy/audit → candidate reservation → durable attempt_lifecycle started identity → Bulkhead permit → candidate-isolated client lease → send/iterate`。因此chain started identity必须早于permit/client/prepare；这些步骤在send前确定失败时，以同一UoW写`client_not_started` proof并关闭为`not_started_proven`。Started提交后关闭前的任何崩溃或commit-ack未知都保守needs-review，不能回到permit/client或send。

每次同候选首次或后续 retry 在取得 client、进入 send 或其他 provider side-effect boundary 前，必须先在 owner shared-budget UoW/CAS 中追加该 global attempt 的 `lifecycle_state=started` 记录；初始观察位全 false、`side_effect_state=not_started`、`http_status/completion_observed/not_started_proof_digest=null`。`started` 只表示 Harness 已耐久占用该 attempt identity，并不声称远端已执行；随后 request/response/结果观察事实只能单调提升。该记录只允许原子收敛为：与同 attempt proof 同一 UoW 提交的 `not_started_proven`、结果无法确定且保留 reservation 的 `unknown`，或与可信 actual/final settlement 同一 UoW 提交的 `settled`。三个终态均不可回退或改写；`not_started_proven` 必须与 candidate `not_started_proofs[]` 一一对应且 digest逐值相同，`unknown|settled` 的 proof digest必须为空。成功completed与可信actual cancelled都使用`lifecycle_state=settled/side_effect_state=result_committed`；后者只允许provider-neutral stream关闭结果证明`stopped`、usage `finality=complete`且所有启用维度完整、无durable delta intent或发布确认不明，保存实际观察位并强制`completion_observed=false`。任何 `started` 悬空记录，包括 started mark 已提交但尚未发送、已发送但 proof/settlement 未提交，以及其 commit-ack 不明，都按最坏情况保留 reservation并进入 needs-review，不得自动再发同一 attempt、创建下一 attempt或推进 provider。

静态资格失败唯一编码为 candidate `state=static_ineligible`、`reason=static_ineligible`；预算不可用唯一编码为 `state=budget_ineligible`、`reason=soft_budget|balance`。两类状态都要求 `side_effect_state=not_started`、六个观察位全 false、`http_status/completion_observed` 为 null、proof list为空，且该 ordinal不得成为 active/waiting/selected/current-reservation candidate；都没有实际 provider attempt或 reservation。Static与普通 allow/soft-fallback预算跳过的 approval bindings均为空，且中间 skip不单独追加 transition；获批候选在 activation时因 balance不足而 budget-ineligible时，request/grant binding digest必须两者都非空并逐值保持原 waiting/approved identity，禁止只出现一个。该特殊形状保留此前进入 waiting的 coordination transition，但不得出现为该候选建立 reservation的 `approved|activated` transition；若继续到后继，只允许一条 `from_ordinal=<该 budget-ineligible ordinal>`、`state=transferred/reason=balance`、released bounds为零的 coordination→reservation transition。`soft_budget` 必须逐值绑定 frozen candidate 的 soft-threshold fallback decision且不允许 approval bindings；`balance` 必须由 owner lock/CAS 中当前 ledger version 对该候选冻结 bound 的 reservation 判定产生。首次 durable decision 提交后，同一 stable key/chain exact replay必须先返回已保存状态，不得因后来余额或 policy变化重新评估或把候选变回 pending。

若前导若干 `static_ineligible|budget_ineligible` 后存在首个可激活候选，唯一初始 transition仍是 `from_ordinal=null`、`to_ordinal=<该候选>`、`state=activated`、`reason=initial`；被跳过 ordinal只由 candidate state与 exhausted causes证明。初始扫描从首次 usage identity绑定的零 impact claim/allocation carrier开始，确保首个 balance决定在 provider前耐久；若没有可激活候选，transitions为空并在同一 carrier提交 exhausted result。若已有 current reservation且当前实际 attempts 已全部可信 not-started，扫描中间不可用候选后存在后继，则只追加一条从 current 直达首个后继的 `transferred` transition；不存在后继则只追加一条 `from_ordinal=<current>`、`to_ordinal=null`、`state=terminated/reason=route_exhausted` transition并释放 current reservation。终态的 canonical 空 reservation 为 `candidate_ordinal=null/token_bound=0/cost_bound=null`。`static_ineligible|budget_ineligible` 不得编码为 `pending`、`denied` 或 `not_started`；`not_started`只属于至少一次实际 attempt且 proof list完整覆盖的运行时终态。全链在资格/预算阶段耗尽时，selected/active/waiting均为空，`evidence_route_ordinal`等于最后一个 exhausted cause ordinal。

每个实际以可信 not-started 结束的 attempt MUST 在下一次同 route retry、transfer 或 terminal 前，把一条不可覆盖的 `not_started_proofs[]` record、对应 `attempt_lifecycle` 从 `started`→`not_started_proven` 的关闭，以及 provider attempt/settlement state 在同一 UoW 原子持久化。record 按全局 attempt 严格递增并逐值保存 proof 的全部 canonical input；`proof_digest` 是 `model-route-not-started-proof-v1` exact object经同一`model-route-canonical-json-v1`所得 bytes 的 SHA-256，输入固定为 schema version、chain id、candidate ordinal、全局 attempt、reason、attempt side-effect state、上述 request/response/status/四个观察位、completion state、endpoint policy digest及 nullable classifier ref/version，不保存 raw header/body。`client_not_started`与`trusted_business_not_started`必须分别匹配核心delta的`9acc29f4…`与`fe2a4837…`逐字节golden vector。前者必须为`side_effect_state=not_started/request_sent=false/http_response_observed=false/http_status=null/completion_observed=null`且classifier为null；后者必须为`side_effect_state=started/request_sent=true/http_response_observed=true/http_status=<白名单值>/completion_observed=false`，classifier逐值匹配当前endpoint policy；两类均要求response-identity/usage/text/delta四个观察位为false。proof与lifecycle的attempt、candidate、观察位、side-effect、status和digest必须逐值一致；只写其中一侧、跨attempt绑定或lifecycle已终态均关闭失败。

candidate summary 的 `side_effect_state` 取全部 lifecycle records 的单调最大值，六个 observed bool 取逻辑 OR；`http_status` 取全局 attempt 最大且观察到 response 的 record status，没有 response 时为 null；`completion_observed` 在任一 response明确未开始时为 false、所有 attempts均未越过 send时为 null；`reason` 取最后一条 proof reason。只要任一 proof 为 trusted-business，candidate 就保持 started/request/response 高水位；因此末次 attempt 即使是 `client_not_started`，也不得抹除更早 attempt 的 started/response/status。candidate 只有在该 ordinal 的 lifecycle records 与实际 attempts 从首次到末次一一对应、全部处于 `not_started_proven`、每项都有逐值匹配 proof、没有 `started|unknown|settled` 悬空或非零实际终态，且末条 proof reason 等于 terminal reason时才可进入 `state=not_started`；同候选 retry可以产生任意混合顺序的两类 proof。

`evidence_route_ordinal` 始终非空并唯一决定既有 `decision.route`：active/waiting/completed 分别等于 active/waiting/selected ordinal；cancelled等于唯一 cancelled candidate；denied 等于唯一 denied candidate；exhausted 等于最后一个 cause 的 ordinal；unknown 等于最后 active/unknown candidate；尚未激活且仍在规划/首候选拒绝时等于当前被评估 ordinal。它只表示证据锚点，不把 cancelled/denied/exhausted candidate 伪造成 selected。该 JSON 只保存去敏 identity/上界、观察事实与 proof/binding digest，不保存 secret、URL、header、prompt、response、approval id、lease id 或 raw error。使用独立列而不是塞进 `result_json`，因为现有 crash-window validator 规定未完成 claim 的 `result_json` 必须为空；改变该语义会破坏已归档单 route replay。

### 4. reservation transfer 是 claim/ledger/chain state 的单事务替换

shared-budget repository 增加 direct/allocation 对称的 `transfer_model_route_reservation`。调用方传入 current ordinal、chain_id、有序后继 candidates及其已绑定 soft-threshold decision、末次 `client_not_started|trusted_business_not_started` reason 与当前候选完整有序 proof-list identity；repository 按既有 owner lock/CAS 顺序验证：

1. stable key、identity 与 chain state 完整且 current ordinal 一致；
2. claim 为 `reserved`；当前 candidate 从首次到末次的每个实际 attempt 都有全局连续、不可覆盖的 lifecycle record，全部处于 `not_started_proven`并有逐值匹配的 `client_not_started|trusted_business_not_started` proof，没有 `started|unknown|settled` 悬空或冲突记录，末条 proof reason 与 transfer reason 相同；每条 trusted-business proof 都匹配当前 endpoint policy/classifier 与冻结 `cross_provider_failover_http_statuses`，所有 proof 都没有 response identity/usage/text/delta，且 `delta_fenced=false`。candidate 与 claim 的聚合高水位由完整 lifecycle/proof list 复算，不以末次 `client_not_started` 抹除更早 trusted-business response；
3. 所有待评估后继按冻结 ordinal 为 pending，所有更早前驱已 `static_ineligible|budget_ineligible|not_started`；
4. ledger 仍 active；candidate controller 逐 ordinal提供已绑定 route/identity的 durable policy结果，repository按 `static skip → policy deny/require-approval/soft fallback/allow → current-balance reservation` 的固定顺序消费：deny终止，require-approval进入零 impact waiting，soft fallback标记 `budget_ineligible/soft_budget`，allow才以“移除旧 impact并加入该候选冻结 bound”的同一 projected owner impact执行 reservation；余额失败标记 `budget_ineligible/balance`，直到找到首个 eligible候选或安全耗尽。Grant只授权自己的 ordinal，获批目标若 balance失败也只能标记 budget-ineligible并继续对更后 candidate重新执行独立 policy，不能把 grant复制过去。

同一 UPDATE/UoW 把 current candidate 置 `not_started`，把扫描中间项置 `static_ineligible|budget_ineligible`，并在找到首个 eligible 后把它置 `active`、修改 active ordinal、claim `reserved_*`/impact 与 owner ledger impact/version；只产生一条从 current 直达该 ordinal 的 transfer，不为中间 skip制造 reservation transition。遇到 require-approval时同一 UoW把 current actual-zero结算、释放 impact并直达该 ordinal的 waiting state；遇到 deny时同一 UoW释放 current并以 `model.policy_denied`终止，二者都不继续扫描。Waiting候选获批后 balance失败时保留零 impact carrier与两份 binding digest；继续到后继使用 `from=<该 ordinal>/state=transferred/reason=balance/released=0` coordination transition，无后继则以该 ordinal→null的 `terminated/route_exhausted`零释放 transition收口。若普通 active current没有 eligible后继，同一 UoW以 actual zero结算 current、释放其 impact、保存全部已评估 skip state、清空 current reservation并提交唯一 `route_exhausted` terminal transition与 exhausted failure；不存在退款后再次选择的窗口。初始尚无 current reservation时使用同一 ordered scan：前导 skip零 impact，首个 eligible以一次 `initial` transition建立 reservation；全不可用则以空 transitions/空 reservation收口。相同参数与完整 proof-list/候选 decision identity重放返回同一状态；ordinal、digest、bound、reason、proof数量/顺序/字段或 identity不一致返回 `budget.operation_conflict`。事务/CAS冲突才整体回滚并重试同一 frozen decision；已经提交的 `budget_ineligible`不得因余额变化重新评估。

候选每次真正进入 provider side-effect boundary 前仍调用扩展后的 `mark_*_attempt_started(global_attempt, candidate_ordinal, attempt_identity_digest)`；它必须在同一 UoW先追加不可覆盖的 `attempt_lifecycle=started`，再把当前 candidate/claim聚合高水位按已知事实单调推进。若 claim已因前序候选保持 started，也必须创建本次独立 attempt identity，不能把调用级或 candidate聚合高水位当作该 retry已开始。只有 started mark提交成功后才允许取得 client或 send；提交确认丢失时先按 stable key读取同一 record，匹配则视为该 attempt已开始并禁止自动调用，冲突则关闭失败。claim高水位、逐候选 side-effect、逐 attempt `request_sent/http_response_observed`各自只允许单调推进；仅取得prepared对象不提升任何request/response/result观察事实。端点绑定 classifier、显式状态白名单和零 response identity/usage/text/delta共同证明 `trusted_business_not_started`时，claim保持 started但当前 attempt可在同一UoW写proof并关闭为`not_started_proven`；其余 started、HTTP response、write/read timeout或结果观察只能把同一 lifecycle关闭为可信`settled`，或在无法结算时关闭为`unknown/needs_review`并保留当前reservation。可信actual取消的settled UoW按完整close usage原子替换reservation为actual、把claim/candidate/lifecycle置result_committed、candidate置`cancelled/invocation_cancelled`并清空active/waiting/selected/current reservation；它不新建transition、不发布completed、不fallback。此前两类可信 not-started attempts的 charge为0。

恢复必须联合验证调用级高水位、逐候选历史与 `attempt_lifecycle[]`：claim为not_started时所有candidate都不得为started/unknown；claim为started时MAY存在尚未创建下一attempt record、`side_effect_state=not_started`的active candidate，但其之前每个started candidate的lifecycle必须全部由完整proof actual-zero关闭并已有transition。结构和交叉引用校验之后，还必须使用冻结route chain逐项重建`model-route-attempt-identity-v1`与两类proof exact object，再用统一canonical serializer重算digest；即使篡改后的attempt/proof引用彼此同步、数量和字段形状合法，也不能越过该重算门禁。恢复只有在当前ordinal既无`started|unknown|settled`未决record、下一global attempt identity完全不存在、全部前序attempt均为`not_started_proven`，且冻结retry policy仍允许时，才可先耐久创建新的started record后继续；已经存在的started record无论`request_sent=false|true`都视为已开始，提交确认未知也只能needs-review，不得推断为“尚未发送”并重发。对已提交完整transfer只重放同一后继；当前active candidate存在started/unknown/response-observed lifecycle、attempt identity缺口/重复/冲突或`delta_fenced=true`时只能补投、结算或报needs-review，不选择后继。这样跨候选或同候选内部的`trusted_business_not_started ↔ client_not_started`混合顺序都不需要把claim/candidate高水位从started降回not_started。恢复错误的安全摘要按耐久事实分层：`attempt_count`统计全局attempt identity，`provider_called`仅由request、HTTP response、result、usage、text或delta观察事实推导；send前悬空started identity因此可以是正count但`provider_called=false`。

### 5. Policy/HITL 对每个候选独立执行，审批暂停不持有预算

候选按 ordinal 串行进入既有 `PolicyEngine`，每个 candidate 都使用自己的完整 route identity、价格与上界，前一候选的 allow/approval 不能授权后继。首候选在有预算影响的 reservation 前检查 policy；后继只在当前候选已安全收敛后检查 policy。`deny` 使用 `model.policy_denied` 终止整条链且绝不进入 exhausted causes；`require_approval` 使用既有 `model.approval_required` 返回 waiting，不调用该候选或更后候选。

进入 waiting 的同一 shared-budget UoW 会释放前一候选 reservation、把 current impact 置零并保存 `waiting_approval_ordinal`，但不建立目标候选 reservation、不获取 permit/client。若首候选直接 require approval，同一 UoW 使用首次入口已冻结的 `usage_call_id` 建立 `state=reserved/side_effect_state=not_started`、token/cost impact 均为 0 的 coordination claim/allocation，并写完整 v1 chain state；它是耐久协调记录而不是 model reservation，不占 owner 余额。此时 approval record 和 resolution lease尚未产生，所以只计算 `model-route-chain-approval-request-v1`：exact keys为`schema_version/chain_id/candidate_ordinal/route_digest/usage_call_id/operation_identity_digest/tenant_id/run_id/agent_id/request_id/trace_id/action/resource/arguments_ref/arguments_hash`，其中request/trace为nullable但必须出现，action逐字为`model.invoke`。该object只用`model-route-canonical-json-v1`并同时匹配核心delta的ASCII `802a004b…`与含`参数`原文UTF-8的`20dfca2b…`判别向量；`ensure_ascii=true`所得`bc72de9d…`必须拒绝。公共chain state保存合法bytes的SHA-256为`approval_request_binding_digest`，`approval_grant_binding_digest`必须为空。原始operation key与请求留在既有checkpoint/私有arguments artifact/ref，不进入chain state。

Orchestrator 随后按既有顺序持久化 `policy_approval` checkpoint，并由 `ApprovalService` 在自己的 UoW 创建携带同一 request binding digest、usage call id与 operation identity digest的 `ApprovalRecord`；resolution lease只在 approve/worker claim时由 approval repository独占提交。`ApprovalGrant` 产生后，`complete_approved()`/`stream_approved()` 从受信 checkpoint读取原始 operation key和上下文，重算初始 ID，先在只读 UoW逐值验证 waiting state、active lease、approval metadata和 request binding，再计算 `model-route-chain-approval-grant-v1`；exact keys为`schema_version/request_binding_digest/usage_call_id/operation_identity_digest/approval_id/lease_id/tenant_id/identity_id/agent_id/run_id/action/resource/arguments_hash`。该object只用同一canonical serializer，`request_binding_digest`必须等于已验证request bytes的SHA-256，并匹配核心delta的`d743ba66…`golden vector。随后 shared-budget UoW以 waiting ordinal + request digest + usage call id做 CAS，把 coordination/waiting row原子替换为目标 reservation、保存 grant binding digest，并只追加一条 canonical `from=waiting ordinal/to=同ordinal/state=approved/reason=approval_granted/released=zero_bound/reserved=target_bound` transition；candidate在同一提交中直接从 waiting变为 active，不存在可恢复的 approved candidate state，也不得再追加 activated transition。该 UoW不声称消费或完成 approval lease；lease的独占 claim、fencing、恢复与最终 resolution仍由既有 ApprovalService/repository workflow拥有。

这两个 UoW 通过 durable handoff 而不是虚构跨 repository 原子事务实现 exactly-once：lease已提交但 activation未开始时仍为零 reservation/零 provider，approval recovery只能重放同一 grant；activation提交确认丢失时，相同首次 usage call id、operation identity与 request/grant digest返回同一 claim、active ordinal/reservation。Route-chain approved continuation复用初始 settlement/outbox/stream group，禁止改用 legacy `operation_key="approved:<approval_id>"` 生成新 ID，禁止 rekey、identity mapping或第二 claim。不同 usage identity、approval/lease/grant fail closed；activation一旦保存 grant digest，lease takeover或另一个 continuation不得激活或调用 provider。provider只在 durable activation/settlement之后开始。恢复 waiting只重发/重领符合既有 approval fencing的同一 request binding；恢复 active approved candidate不重新预约，也不把新 lease替换进已激活 grant binding。后继 deny时，同一 shared-budget UoW将当前已以 `client_not_started|trusted_business_not_started` 收敛的 candidate按完整 proof以 actual zero结算、释放 reservation、写 rejected failure evidence并把 claim置为 `result_committed`；首候选 deny则保持零 coordination row、零 reservation。该方案覆盖 waiting→approval record、lease claim→activation、activation commit-ack loss、伪造 usage identity、provider started与 resolution publication的独立 crash windows。

### 6. provider/client/Bulkhead 按 deployment 隔离，生命周期仍由 composition root 拥有

Router provider registry 继续按 provider kind 选择 adapter protocol，但 adapter 获取资源必须使用完整 candidate plan。`ControlledOpenAIClientFactory` 的 cache key 扩展为 `deployment_id + endpoint_policy_digest + credential_ref + model_catalog_digest`；每个 deployment 拥有独立 client lease 与 `process_deployment` semaphore。不同 deployment 即使 provider kind 和 model 相同，也不能共享 credential-bearing client 或 permit。fake provider 只在显式 fake ref 中可选；真实 chain 耗尽不查询 fake registry 项。

`ModelRouter.aclose()` 与 runtime composition 按对象 identity 幂等关闭所有 provider/factory；关闭期间的 active prepare/stream 仍使用 Phase 18.1 的取消、等待与 shared failure 语义。reload 仅替换新 root 可见配置，durable chain 的 client 获取要逐值匹配原 snapshot；credential ref 若已不可用则 fail closed 为 not-started，不能换用新 ref。

### 7. completion 与 streaming 共享一个候选推进控制器

新增 provider-neutral chain invocation seam，负责 `candidate → policy/HITL → budget claim/transfer → durable attempt started identity → Bulkhead permit → candidate-isolated client/prepare → send/iterate → classify → settle`。该顺序只属于显式chain；legacy单route继续保留`reservation → permit → client → durable side_effect_started → send`。非流式和流式保留各自结果消费/事件发布，但只接受静态零副作用、`client_not_started` 或 `trusted_business_not_started` 三类推进事实。classifier 由 endpoint policy/version 绑定，跨 provider 还要求 HTTP status 命中 deployment 冻结的 `cross_provider_failover_http_statuses`；状态码、body 或 exception string 本身不能授权。若同一状态还在 `retryable_http_statuses` 且当前 candidate 尚有 attempt，先执行同 route retry，attempt 耗尽后才 transfer。write/read timeout、取消、无受信证明的 response、unknown、response identity、usage 或 text/delta 都停止 chain；取消只在stream关闭给出完整可信stopped actual且无durable delta不确定性时结算为cancelled，否则unknown/needs-review；两者都不fallback。Harness policy deny 不进入 classifier。

streaming 在取得第一个 provider delta 时立即在 invocation 内存围栏；在持久化首 delta intent 的同一 UoW 把 durable `delta_fenced=true`。这两个事实任一为真都禁止 transfer。若 delta 已观察但持久化失败，调用按 unknown 保留 reservation/stream slots；不得因为 durable flag 未提交而切换。reader 断线与恢复只处理 committed CanonicalEvent，不接触 chain controller。

### 8. evidence 字段版本化且只描述已证明事实

`ModelUsageEvidence.decision` 保留既有 exact `route`、`attempts[]` 与 `budget_charge`，并仅对 chain mode 增加一个 exact `route_chain` 字段：`{"schema_version":"model-route-chain-evidence-v1","identity":<完整 model-route-chain-v1>,"state":<完整 model-route-chain-state-v1>}`。started 与任何 final/failure evidence 都必须携带该容器：identity 逐值相同；started state 反映首次证据锚点/active ordinal，final state 反映 completed/cancelled/denied/exhausted，unknown 只进入私有 review、不得发布 final usage。既有 `decision.route` 在每份 evidence 中都逐值命中该 state 的 `evidence_route_ordinal`；completed 时它因此命中 selected candidate，cancelled/denied/exhausted 时命中取消/拒绝/最后耗尽候选但 selected 仍为 null。chain mode 不删除或改名既有字段，也不新增顶层 `ModelUsageEvidence.usage_call_id`；完整 state 中的 `decision.route_chain.state.usage_call_id` 是唯一新增的嵌套公开位置，并与 CanonicalEvent/telemetry metadata 的同名 correlation逐值相同。

Chain-mode `attempts[]` 完整继承 5.29 exact fields，并对每个实际 attempt 强制增加 `candidate_ordinal/deployment_id/provider/model`、`request_sent/http_response_observed/http_status`、`response_identity_observed/usage_observed/text_observed/delta_observed`、`completion_observed`、nullable `not_started_reason/not_started_proof_digest`、`endpoint_policy_digest` 与 nullable `classifier_ref/classifier_version`；validator 仅在 `route_chain.schema_version` 存在时接受这些字段。`attempt` 在所有候选和同候选 retry 之间从 1 全局连续，不按候选重置；route 字段必须命中对应 candidate。每个零 charge attempt 的新增字段必须逐值等于对应 candidate `not_started_proofs[]` record并可重算 digest；非 not-started attempt 的 reason/digest 必须为 null但仍保存实际观察事实。`budget_charge` 按全链全局 attempt 聚合，只有逐 attempt 完整证明的两类可信 not-started charge 为 0，unresolved ordinal 仍引用全局 attempt。所有容器拒绝 unknown fields、字段缺失、proof list/attempts 不一致和旧/新形状混搭。

可信actual取消的final attempt唯一使用`outcome=cancelled`、attempt `side_effect_state=started`、`completion_observed=false`、`not_started_reason/not_started_proof_digest=null`，并逐值保存close result产生的usage观察事实；对应lifecycle/candidate/claim已在同一UoW进入`result_committed`。`budget_charge`按完整close usage记actual，稳定错误为`model.invocation_cancelled`，不得出现response、selected ordinal、completed candidate或`model.output.completed`。若没有明确request/response/result/usage/text/delta观察，`provider_called=false`且attempt identity仍计入attempt_count；本地cleanup失败不得覆盖已经形成的稳定取消或unknown错误。

流式 public delta/completed 的 `attempt` 与 5.29 attempt 使用同一全局 provider-attempt ordinal：legacy 单 route 固定为 1；发生安全 failover 后，全部 delta/completed 使用实际产出文本候选的同一个大于等于 2 的 ordinal。该值不进入 event id，`usage_call_id` 与 stream group identity 保持不变；consumer 通过同一 usage evidence 的 attempt/candidate 映射解释它，不新增 candidate 字段或第二事件版本。

final top-level provider/model 必须逐值命中 final state 的 `evidence_route_ordinal`；completed 时该 ordinal 同时等于 `selected_ordinal`，denied/exhausted 时 selected 保持 null且不能伪造完成。公开 `model.request.started` 在 provider 副作用前发布，是调用生命周期 evidence而不是远端开始证明；started/final route 差异只能由同一 chain 中全部前驱逐项证明：静态/预算候选必须是零 provider attempt的 `state=static_ineligible|budget_ineligible`并满足各自reason/binding/transition约束，运行时候选的全部实际 attempts必须以 `client_not_started|trusted_business_not_started`完整有序 proof records与连续 transition安全收敛。provider claim/candidate聚合和逐 attempt request/response历史保持单调，除逐 attempt完整 `trusted_business_not_started`外的任何 started/response事实都禁止后继。exhausted error exact detail 为 `schema_version="model-route-chain-exhausted-v1"`、`chain_id` 与按 ordinal 连续的 `causes[]`，每项只有 `ordinal` 和 `cause: capability|catalog|input_bound|hard_budget|soft_budget|balance|not_started_failure`；全局 policy deny 单独返回 `model.policy_denied`，不得进入 causes。拒绝 unknown fields、重复 ordinal 与缺口。

全链安全耗尽使用 `model.route_chain_exhausted`。除完整 `trusted_business_not_started` 外，started/unknown、response/usage/text/delta 后沿用 `model.provider_side_effect_unknown` 或当前稳定 provider error并停止。证据不得包含完整 endpoint、credential ref 的 secret value、header、response id、raw body/exception 或 prompt/output 文本。

### 9. live smoke 独立双凭据前置，默认永远零调用

新增 `model-failover-live-smoke/v1` artifact 和 `make smoke-live-model-failover` / `make ci-smoke-live-model-failover`。artifact 是拒绝 unknown fields 的四分支判别联合，顶层 exact fields 为 `schema_version`、`status: passed|hosted-unverified|external-blocked|failed`、`provider_called: bool`、`attempt_count: int`（非 bool、非负）、`chain_id: str|null`、`selected_ordinal: int|null`、`candidates`、`usage` 与 `reason_code: str|null`。非空 `chain_id` 必须是 64 位小写十六进制；非空 `selected_ordinal` 必须是非 bool 正整数。`candidates` 的元素 exact fields 为 `ordinal/deployment_id/provider/model/outcome/attempt_count/not_started_proof_count/request_sent/response_observed/not_started_reason/http_status`：ordinal 必须从 1 连续、唯一且按升序排列，三个 identity 都是非空字符串；两个 count 都是非 bool 非负整数，顶层 attempt count 必须等于逐候选 attempt count 之和；outcome 封闭为 `not_started|completed|unknown|not_called`。`not_called` 强制两个 count 为 0、两个观察位为 false且 reason/status为 null；`not_started` 强制 attempt count大于 0且 proof count与其相等；其他 outcome的 proof count为0且 reason为null。观察字段为 bool，reason 只允许 null或两类 not-started，HTTP status为 nullable非 bool 100～599；`client_not_started` 强制 false/false/null，`trusted_business_not_started` 强制 true/true/显式白名单状态。`usage` 要么为 null，要么 exact 为 `{input_tokens,output_tokens,cost_usd,cost_status}`；两个 token 是非 bool非负整数，`cost_status=reported|estimated|unavailable`，cost是非 bool有限非负 number或null，且 null当且仅当 unavailable，组合语义逐值复用 API 5.29。

live smoke冻结恰好两个不同 `deployment_id`、两个不同 `credential_ref` 与两个不同受信 endpoint，并要求两条 smoke route的 `max_attempts=1`；两个 deployment 的 `provider_kind`可以相同，这只约束验收探针，不改变产品 route chain最多8候选或普通运行时 retry。前置必须同时具备本会话授权、failover opt-in、上述隔离对、首选可受控产生 `client_not_started`或`trusted_business_not_started`的 fixture与次选能力；公共 validator 必须包含同`provider_kind`但不同deployment/credential/endpoint的可达PASS正合同，以及任一隔离维度复用时的拒绝负合同。缺任一项的 `hosted-unverified` 分支 exact shape 为 `provider_called=false/attempt_count=0/chain_id=null/selected_ordinal=null/candidates=[]/usage=null`，`reason_code`按 `authorization_missing -> failover_opt_in_missing -> credential_pair_missing -> deployment_pair_invalid -> not_started_fixture_missing` 只取最高优先级，进程0/CI skipped；不探测 credential内容、不发网络。

`passed` 分支唯一成功形状为：`provider_called=true`、`attempt_count=2`、`chain_id=<非空64位摘要>`、`selected_ordinal=2`、恰好两个 ordinal 为 `[1,2]` 的 candidate；第1项 `outcome=not_started/attempt_count=1/not_started_proof_count=1` 并命中上述两类可信组合之一，第2项 `outcome=completed/attempt_count=1/not_started_proof_count=0/request_sent=true/response_observed=true/not_started_reason=null/http_status∈{200..299}`，只有第2项与`selected_ordinal`相等且全表唯一 completed；`usage`非空合法且 `reason_code=null`。producer/validator还必须从同一 durable route-chain evidence逐值核对`chain_id`、两个 candidate identity/outcome、全局 attempts长度2、首项 proof record、`selected_ordinal`与`usage`，而非只相信 artifact自报字段。

四分支联合按`status`与identity形状判别：`hosted-unverified`使用上述前置缺失空形状；`failed`另允许且只允许一个chain冻结前子变体，其 exact shape 为`provider_called=false/attempt_count=0/chain_id=null/selected_ordinal=null/candidates=[]/usage=null/reason_code=contract_failure`，进程1/CI fail。除此子变体外，`passed|external-blocked|failed`都表示chain已冻结，强制`chain_id`非空、`candidates`恰好为上述两个冻结identity/ordinal；`provider_called`逐值复制durable invocation安全事实，任一request/response观察都强制其为true，false则所有request/response观察均为false，`response_observed=true`另蕴含request sent。前置完整后的外部网络/配额/provider故障才为`external-blocked`、进程2/CI fail：`selected_ordinal=null`、不得出现completed，`reason_code`只允许`network_unavailable|provider_rejected|quota_blocked|provider_timeout|provider_result_unknown`。chain冻结后的本地预算/恢复/证据/terminal失败为`status=failed/reason_code=contract_failure`、进程1，必须保留两个candidate与已观察facts，`selected_ordinal`若非空必须命中唯一completed。后两类失败的非空`usage`仍须满足公共组合并与durable evidence逐值一致。`failed`的空/非空identity不得混搭；任何status/field、artifact/durable evidence或count绑定冲突都关闭失败，required acceptance不得记PASS。

## Affected Surfaces

- 配置与 registry：`config/schemas.py`、`config/model_endpoints.py`、`registry/descriptor.py`、`registry/_loader.py`、profile/Agent YAML 示例。
- 路由与 provider：`models/providers.py`、`models/_router_contracts.py`、`models/_router_current.py`、`models/_router_current_chain.py`、`models/_router_snapshot.py`、`models/_router_snapshot_chain.py`、`models/router.py`、`models/__init__.py`。
- invocation/结算/恢复：`models/invocation.py`、`models/_invocation_execution.py`、`models/_invocation_planning.py`、`models/_invocation_streaming.py`、`models/_invocation_chain.py`、`models/_invocation_chain_base.py`、`models/_invocation_chain_routing.py`、`models/_invocation_chain_approval.py`、`models/_invocation_chain_completion.py`、`models/_invocation_chain_stream.py`、`models/_invocation_chain_stream_support.py`、`models/_invocation_chain_stream_terminal.py`、`models/_invocation_chain_evidence.py`、`models/_invocation_chain_settlement.py`、`models/_route_chain_state.py`、`models/_route_chain_state_initial.py`、`models/_route_chain_state_approval.py`、`models/_route_chain_state_attempts.py`、`models/_route_chain_state_completion.py`、`models/_invocation_settlement.py`、`models/_invocation_evidence.py`、`models/_settlement_contracts.py`、`models/_settlement_validation.py`、`models/_settlement_evidence_validation.py`、`models/_settlement_evidence_models.py`、`models/_settlement_chain_evidence_validation.py`、`models/_streaming_consumption.py`、`models/_streaming_events.py`、`models/_streaming_settlement.py`、`models/_settlement_publication.py`。职责拆分只移动 chain 内部协调、规划、证据 DTO/交叉校验、stream terminal 收敛与状态变换，公共 bound façade、DTO、错误码和顺序语义不变。
- adapter/composition：`adapters/models/_pydantic_ai_client.py`、`adapters/models/pydantic_ai.py`、`adapters/models/_pydantic_ai_streaming.py`、`adapters/models/fake.py`、`runtime/services.py`、`runtime/shared_budget.py`、`runtime/_shared_budget_identity.py`、`runtime/_shared_budget_snapshot.py`、`runtime/_shared_budget_recovery.py`。
- approval/continuation：`runtime/continuation.py`、`runtime/_run_continuation.py`、`approvals/service.py`、`approvals/_continuation.py`、`storage/access_repositories.py`、`storage/approval_records.py`、`storage/approval_recovery_repositories.py`、`storage/service_approval_repositories.py`；保持现有 approval resolution UoW owner，不把 lease 私有字段公开或塞进 shared-budget 事务。
- storage/migration：`storage/shared_budget.py`、`storage/shared_budget_models.py`、`storage/model_route_chain_state.py`、`storage/_model_route_candidate_validation.py`、`storage/_model_route_chain_recovery.py`、`storage/_shared_budget_route_chain_repository.py`、`storage/_shared_budget_route_chain_validation.py`、`storage/_shared_budget_repository_records.py`、`storage/_shared_budget_direct_repository.py`、`storage/_shared_budget_allocation_repository.py`、`storage/_shared_budget_replay_repository.py`、`storage/_shared_budget_lifecycle_repository.py`、`storage/shared_budget_repositories.py`、`storage/usage_evidence_repositories.py`、`storage/stream_evidence_repositories.py`、`storage/usage_attempt_review_repository.py`、`storage/migrations/versions/0017_model_route_chain_state.py` 及 migration catalog/runner；stream durable validator 与 unknown/needs-review repository 都必须接受并逐值保存全链 provider attempt ordinal，legacy 单 route 才固定为 1。
- 验证/交付：新增 Phase 18.2 route/config/invocation/composition/recovery/streaming/SQLite/PostgreSQL/live smoke contracts；其中 completion 策略/预算、proof-transfer与后继恢复、stream取消结算、allocation仓储、allocation cleanup、仓储主合同、仓储 guardrail、初始/审批transition交叉不变量、候选聚合/history交叉不变量及live后置编排失败恢复分别由 `test_controlled_multi_provider_failover_policy_budget_contracts.py`、`test_controlled_multi_provider_failover_recovery_contracts.py`、`test_controlled_multi_provider_failover_stream_cancellation_contracts.py`、`test_shared_parent_budget_route_chain_allocation_contracts.py`、`test_shared_parent_budget_route_chain_allocation_cleanup_contracts.py`、`test_shared_parent_budget_route_chain_repository_contracts.py`、`test_shared_parent_budget_route_chain_repository_guardrail_contracts.py`、`test_shared_parent_budget_route_chain_transition_contracts.py`、`test_shared_parent_budget_route_chain_candidate_state_contracts.py`、`test_controlled_multi_provider_failover_postgresql_candidate_state_contracts.py`与`test_controlled_multi_provider_failover_live_smoke_orchestration_contracts.py`承载；typed配置、provider/bound装配及共享live executor相邻支撑分别由`controlled_multi_provider_failover_settings_test_support.py`、`controlled_multi_provider_failover_test_support.py`和`test_controlled_real_model_offline_contracts.py`承载，职责拆分后的验收节点名称与断言保持不变；durable chain/usage读取与artifact投影由`scripts/live_model_failover_evidence.py`承载，`scripts/smoke_live_model_failover.py`保留正式composition、`run_authorized()`公共seam与CLI入口；更新共享失败域producer `scripts/smoke_live_model.py`、`scripts/live_model_failover_contract.py`、`scripts/ci_evidence.py`、Makefile、GitHub/GitLab CI、`compliance/ci-jobs.toml`、`docs/acceptance-matrix.md`、Product/API/DEV/living plan/change matrix、README 与中英文维护文档。
- UI：不适用。依赖：不新增第三方依赖。发布：只验证 build/license，不发布。

## Testing Seams

- `ModelRouter.plan_chain()` / `plan_chain_from_snapshot()`：两个不同 deployment/provider doubles、顺序冻结、request 子序列缩权、reload 后旧 snapshot 不漂移、非法 endpoint/credential/model 在 client/network 前失败。
- `BoundModelInvocationService.complete()` / `complete_approved()`：client-before-send 与受信业务未开始两类切换、逐候选 allow/deny/require-approval、同 route retry 先于跨 provider、403 显式选择、approval request/grant 两阶段 binding 与 replay、含糊 timeout/response/unknown/usage/text 停止、全链耗尽、真实链不隐式 fake。
- `BoundModelInvocationService.stream()` / `stream_approved()`：首 delta 前两类可信 not-started 可切换；含糊 timeout/response、unknown、观察或提交 delta 后取消/deadline/断线/recovery 均不切换并保留 prefix/预算。
- composition：不同 deployment 的 factory lease、credential origin、endpoint、catalog 与 Bulkhead semaphore 不共享；关闭/reload 不串扰。
- SQLite/PostgreSQL repository/UoW：两类 not-started proof 正反例、started claim 的可信业务未开始特例、其他 started/response 后 transfer 拒绝、余额竞争、相同参数 replay、冲突、审批零 reservation/恢复，以及 transfer 前、提交确认丢失、mark started 后和 result committed 后 crash recovery。
- usage/evidence publication：started/final 可跨候选但必须命中 durable chain；unknown 不退款、不发布 final、不重放；completed/usage/terminal 顺序保持。
- 默认离线与 live：网络哨兵证明 fake/local/test/eval/smoke-local 零网络；live 前置不足零调用 hosted-unverified，完整前置后的外部故障与本地合同故障分层。

## Risks / Trade-offs

- [迁移只新增 JSON 列，数据库无法逐字段检查全部嵌套状态] → DTO validator、repository lock/CAS、SQLite/PostgreSQL contract 和 import/quality gate 共同验证 exact shape；列默认 null，旧 row 不回填。
- [同一 usage started 与 final 的 provider/model 可能不同] → 只对带合法 durable chain identity 的 v1 evidence 放宽，并要求 selected ordinal、candidate route 与 final route 全量匹配；旧单 route 继续 exact equality。
- [候选较多会增加 planning 与快照体积] → Phase 18.2 先冻结最多 8 个 route refs，配置解析前拒绝更长列表；不引入动态发现或权重。
- [transfer 可能因下一候选更贵而被余额拒绝] → 在同一 owner ledger lock/CAS 下比较替换后的总 impact；余额不足时同一 UoW 将该候选耐久标记为 `budget_ineligible/balance`，继续扫描并把当前 reservation 原子直达首个 eligible 后继，或在没有后继时 actual-zero 结算当前候选、释放并提交 exhausted terminal，绝不暴露先退款再选择的窗口。
- [delta 观察与 durable fence 之间存在进程窗口] → 内存观察立即禁止切换；durable persist 失败按 unknown，而不是退回 not-started。
- [共享 provider object 可能被误解为共享 client] → adapter contract 直接断言不同 deployment 的 lease、transport credential、semaphore 与 close 生命周期独立；provider object 只是无 secret 的 protocol façade。

## Migration Plan

1. `0017` 只为两类 shared-budget operation row 增加 nullable `route_chain_state_json`，不回填历史 row；upgrade 在 SQLite/PostgreSQL 保持既有 0016 数据逐值不变。只降到 0016 时，downgrade 先检查两表：只要任一 non-null state（无论 completed、cancelled、active 或 needs-review）就以 `storage.route_chain_state_present` 关闭失败，DDL、数据与 ledger impact 全部不变；只有全部为 null 才允许删除列。若 Alembic 最终 target 继续低于 0016，`0017` 在删除本 revision schema 前先执行 0016 所需的 opt-in/evidence gate，使任何后续拒绝发生在 0017 DDL 前；这不阻断保留 v1 shared-budget evidence 的合法 `0017 -> 0016` 路径。
2. Agent 显式声明 `fallback_routes` 后，`plan_chain()` 产生的所有 chain-mode 调用都写 v1 state，包括 request 缩权后的单候选 chain；其 chain/evidence/replay 语义不降级。只有缺少 `fallback_routes` 且未提供 `route_refs` 的旧 descriptor/request 才继续走 legacy 单 route 路径和 null state。
3. 先部署能读取 null/v1 state 的代码，再通过 typed config 显式启用 multi-provider route refs；默认 profile/Agent 不改变。
4. 运行时回滚先停用新的 multi-provider policy；一旦产生过 non-null chain row，数据库迁移成为证据保留意义上的 forward-only，不能为了运行旧 binary 删除 completed/cancelled/active/needs-review evidence。仅当两个表从未写入 chain state 时才可降级到 0016；继续降到更早 revision 还必须先满足 0016 的显式 opt-in 与 evidence gate，否则整个跨 revision downgrade 在 0017 schema 仍完整时关闭失败。不得清空列、释放 unknown reservation或把真实失败改写为 fake success。

## Open Questions

- 无阻断问题。双真实 deployment 的 credential、endpoint 与授权属于 live smoke 外部前置；缺失时按合同保持零调用 `hosted-unverified`，不阻断未来离线实现完成后的 `ready-to-archive`。当前契约批次仍只到 `ready-for-implementation`，不把 0/39 tasks 或契约审查写成实现完成。
