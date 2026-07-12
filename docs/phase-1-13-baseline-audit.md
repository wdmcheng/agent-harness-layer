# Phase 1-13 全量基线审查报告

## 1. 审查目标与边界

- 目标：审查并修复 Phase 1-13 的产品、架构、契约、实现与验证基线，使其可作为 Phase 14、15 的可信输入。
- 明确排除：不实施或完成 Phase 14、15；不发布、不 push、不创建 tag、不自动归档 OpenSpec change。
- 起始 commit：`f27753ae9e8685750ddbe67fd000d87a26c72f7e`（`develop`）。
- 起始工作区：`git status --porcelain=v2 --branch` 仅输出 branch 元数据，无 tracked 或 untracked 文件，工作区干净。
- 审查开始日期：2026-07-12。
- 报告状态：进行中，尚未冻结；最终联合审查前的更新不构成基准 PASS。

## 2. 文档权威关系

1. `Product-Spec.md` 与 `docs/architecture/` 四组架构图共同构成第一层产品和架构基准。两者冲突时不自行覆盖；涉及范围、产品取舍或难逆转架构决策时暂停请求用户决定。
2. `DEV-PLAN.md` 将第一层基准映射到 Phase 和验收顺序；`API-Contract.md` 将其映射到 endpoint、schema、error、security 和跨入口契约。二者不得扩张产品范围，也不得把未来能力写成当前能力。
3. `openspec/specs/` 是当前长期行为契约；`openspec/changes/archive/` 只作为 delta、设计、任务与来源追溯证据。归档记录不能反向覆盖当前产品范围。
4. 生产代码、运行时 OpenAPI 和测试必须实现上述已确认契约；实现存在不等于需求成立，测试通过也不能替代语义对账。
5. Phase 14、15 在 `DEV-PLAN.md` 中保持待实现；本轮只能识别其真实缺口，不能把审查或文档修复冒充为 Phase 完成。

## 3. 完整审查范围

### 3.1 架构与产品

- `docs/architecture/pydantic-ai-agent-architecture.{drawio,excalidraw,png}`：产品全景。
- `docs/architecture/agent-harness-technical-architecture.{drawio,excalidraw,png}`：技术架构。
- `docs/architecture/agent-harness-runtime-trust-boundaries.{drawio,excalidraw,png}`：运行与信任边界。
- `docs/architecture/agent-harness-deployment-boundaries.{drawio,excalidraw,png}`：部署边界。
- `docs/architecture/README.md`、`Product-Spec.md`。

### 3.2 计划、API 与 OpenSpec

- `DEV-PLAN.md`、`API-Contract.md`、运行时 OpenAPI。
- `openspec/specs/` 下 23 个主规格。
- `openspec/changes/archive/` 下 20 个归档 change 及其 proposal、delta specs、design、tasks 和附属矩阵。

### 3.3 实现与验证

- `packages/agent-harness/`、`templates/service-app/`、`examples/`、`tests/`、`evals/`、`scripts/`、构建与质量配置。
- unit、contract、integration、eval、smoke-local、真实 PostgreSQL/Redis service smoke、build、license、pre-commit 和 OpenSpec strict validation。
- capability 批次和最终跨 capability 联合 Stage 1/2 审查。

## 4. 审查与裁决规则

- 每一轮显式创建 3 个 fresh `code-reviewer`；三者读取相同完整范围和原始证据，不分工、不共享报告、不读取主 Agent 摘要。
- 平台并发允许时 3 个 reviewer 同时执行；否则按 2+1 执行。每个结论都保留独立记录。
- 主 Agent 逐条用原文、文件行号、运行输出或官方资料复核。采纳或驳回不按票数决定。
- 修复后的相关范围必须重新三审；审查后出现任何相关 tracked diff，原 PASS 失效。
- `openspec validate` 只证明结构可解析，不充当语义审查结论。
- 所有 HIGH、MEDIUM 和涉及正确性或维护性的 LOW 必须闭环，才允许声明 Phase 1-13 基线通过。

## 5. 起始文件 SHA-256

| 文件 | SHA-256 |
|---|---|
| `Product-Spec.md` | `e1b70204e1e2559571229bdd315bdf91b4e1d01ac41657df15269aa21a0ed368` |
| `DEV-PLAN.md` | `1a2402e50bb2f55d8141ad80f0d74d1c6bb3be7b52ff23259bd9b33850f65b01` |
| `API-Contract.md` | `2d4a3e6cd48c71d43cc9d768329c0fa53ee08233a4748268e0947b7582f6c16f` |
| `docs/architecture/README.md` | `f8e1527c26d1f4ed31a597a980aee796ffa248d2524c3faa786486b418a1f3c7` |

四组架构源文件与预览的逐文件起始 hash 保留在本轮命令证据中；修复完成后在本报告记录最终 hash。

## 6. 阶段记录

| 阶段 | 三份独立结论 | 主 Agent 复核 | 修复与复审 | 状态 |
|---|---|---|---|---|
| 架构四组图 | 多轮失败与复审后，最终 A/B/C 均为 Stage 1/2 PASS，且无 HIGH/MEDIUM/LOW | 主 Agent 逐项复核并闭环所有有证据发现；未按票数裁决，见 6.1-6.10 | 四格式、当前/未来边界、部署启动链和可读性已冻结为候选基准 | 候选基准已冻结 |
| Product Spec 与架构双向对账 | 首轮三份均 Stage 1 FAIL；产品决策后复审三份仍 Stage 1 FAIL，缺陷集中在下游计划/OpenSpec 未同步 | 主 Agent逐条采纳有原始证据的产品缺口；用户已裁决 delegation/SSE 保留 P0、SecretProvider 留 P1 | Product/架构语义已对齐；待 DEV/API 和 OpenSpec 完成后以同一最终 hash 重审冻结 | 进行中 |
| DEV-PLAN / API Contract | 首轮三份均 Stage 1 FAIL；Stage 2 为两份 PASS、一份 FAIL | 采纳 RUN-002 状态、OpenAPI 精确性、tenant/idempotency/deny、文件所有权与 Phase 15 验收缺口 | 已修复并在组合 hash `18170c8b...9de` 上重新三审 | 复审中 |
| OpenSpec 主规格与归档来源 | 待执行 | 待执行 | 待执行 | 未开始 |
| capability 批次 | 待执行 | 待执行 | 待执行 | 未开始 |
| 跨 capability 联合 Stage 1/2 | 待执行 | 待执行 | 待执行 | 未开始 |

### 6.1 架构首轮三审与主 Agent 复核

三位 reviewer 均直接读取四组图源、PNG、产品/计划/API/ADR、实现和测试，未读取本报告或其他 reviewer 结论。

| 发现 | Reviewer 结论 | 主 Agent 复核与裁决 | 证据 |
|---|---|---|---|
| 技术图和运行图仍把 Phase 9-13 画成未来态 | R1 HIGH、R2 HIGH、R3 MEDIUM | 采纳，严重度按 HIGH 闭环。图的状态声明与 `DEV-PLAN.md`、已归档 OpenSpec 和实现直接冲突。 | 旧图 `legend-current` 为 Phase 1-8，Retrieval、Observability、Eval、示例 agent 仍标待实现；CodeGraph 定位当前实现，reviewer 定向合同测试分别得到 50/62/124 通过。 |
| 运行图缺失 Phase 13 service queue/worker 链路 | R1 合并进 HIGH、R2 未单列、R3 MEDIUM | 采纳。service API 已改为持久化/enqueue，worker pickup/reclaim 后执行；旧图仍画 API/CLI 直达 orchestrator。 | `API-Contract.md` RUN-001 与入口映射、`docs/adr/0001-p0-service-boundaries.md`、`RedisRunQueue`、service runtime composition。 |
| HITL 把 approve 与 deny 都画成 resume | R1 HIGH、R2/R3 未单列 | 采纳，按 HIGH 闭环。deny 不得创建 continuation；approve 必须经过私有 lease/grant、唯一 claim 和 fenced enqueue。 | `Product-Spec.md` FLOW-003、`API-Contract.md` RUN-005/APR-002、ADR 0001、`ApprovalGrant` 和 split runtime recovery tests。 |
| 产品全景图未区分当前、目标扩展位与未来能力 | R1 MEDIUM、R2 MEDIUM、R3 MEDIUM | 采纳。SSE/WS、Graph/REPL 扩展位、不可变日志/KMS、未来搜索 adapter、Phase 15 Release Gate 需要显式状态；BM25 必备与 PGroonga/pgvector 可选必须遵守 Product Spec。 | `API-Contract.md:1417-1428`、`Product-Spec.md` SCOPE-017 与 OUT-006/010/011、`DEV-PLAN.md` Phase 15。 |
| deployment Excalidraw 缺少三条队列边标签 | R1 未单列、R2 MEDIUM、R3 LOW | 采纳。`Queue DTO`、`XADD/XAUTOCLAIM/fenced ACK`、`pickup/reclaim` 是 Phase 13 关键协议语义。 | drawio 原有三个 edge label；Excalidraw 原有对应箭头但无文本元素。 |
| 四份 Excalidraw 的连线无端点绑定 | R1/R2 未单列、R3 LOW | 采纳为维护性 LOW。`docs/architecture/README.md` 将 Excalidraw 定义为协作编辑源，移动节点时连线失联会破坏可维护性。 | 首轮统计 77/77 条箭头的 `startBinding/endBinding` 均为空。 |

### 6.2 架构修复与自检

- 技术图：将当前状态更新为 Phase 1-13，Phase 14-15 保持待实现；Retrieval、Observability、Eval Gate/experiment、四个 P0 示例 agent、Eval API 和 worker queue/recovery 改为当前能力。
- 运行/信任边界图：新增 local inline 与 service Redis queue -> worker 的执行分派；事件持久化区分 local JSONL 与 service PostgreSQL；HITL 明确 approve 的私有 lease/grant continuation 和 deny 零 continuation；retrieval 改为已实现。
- 全景图：明确未来 SSE/WS、Graph/REPL 目标扩展位、未来不可变审计与 KMS adapter、Phase 15 Release Gate；修正 P0 Retrieval 为 FTS/BM25 必备、PGroonga/pgvector 可选、搜索集群 adapter 未来；service API/worker 标为已物理分进程。
- deployment Excalidraw：补齐 `Queue DTO`、`XADD / XAUTOCLAIM / fenced ACK`、`pickup / reclaim` 三个标签。
- 四份 Excalidraw：所有 78 条箭头均补 `startBinding/endBinding` 和节点反向 `boundElements`；运行图新增 1 条 queue -> orchestrator 箭头。
- drawio 结构校验：四份文件均 `0 error(s), 0 warning(s)`；XML 和 Excalidraw JSON 均可解析。
- PNG：从修复后的 drawio 重导出全景、技术、运行图并逐张以原分辨率视觉核验；deployment PNG 未变。
- Excalidraw MCP：四份源可导入并由 `describe_scene` 解析连接关系；当前环境没有前端 canvas client，无法通过 MCP 截图，因此 PNG 视觉核验仍以 drawio 导出为准，该限制保留到最终未验证项。

### 6.3 外部声明刷新

- 2026-07-12 通过官方 PyPI JSON 刷新：`pydantic-ai` current `2.9.0`，仓库锁定 `2.5.0`；这证明锁定版本不是 current，但不要求本轮升级依赖。
- `pydantic-ai-harness` current `0.6.0`；Pydantic 官方仍将其定义为独立 capability library，支持 Product Spec 的“可选、不作为 P0 必选依赖”判断。
- DBOS 官方文档仍说明 workflow 中断后从最后完成 step 恢复，workflow ID 可作 idempotency key；仓库锁定与 PyPI current 均为 `2.26.0`。
- Redis 官方文档仍说明 `XAUTOCLAIM` 转移超时 pending entry 的 consumer ownership；仓库 `redis-py` 与镜像策略使用的 `8.0.1` 与 PyPI current 一致。
- Pydantic Evals 官方仍以 Dataset、Case、Experiment、Task、Evaluator 为核心模型，与当前 eval experiment 表达一致。

官方资料：`https://pydantic.dev/docs/ai/harness/overview`、`https://pydantic.dev/docs/ai/evals/evals/`、`https://docs.dbos.dev/python/tutorials/workflow-tutorial`、`https://redis.io/docs/latest/commands/xautoclaim/`、各包 `https://pypi.org/pypi/<package>/json`。

### 6.4 架构第二轮三审与修复

第二轮 reviewer D 判定 Stage 1 FAIL（2 MEDIUM）并报告 1 个维护性 LOW；reviewer E 判定 Stage 1/2 PASS；reviewer F 判定 Stage 1 PASS、Stage 2 PASS WITH NOTES（1 个维护性 LOW）。主 Agent 逐条复核后，不采用多数票，采纳以下全部有原始证据的发现：

- MEDIUM：技术图 `sse` 节点仍使用当前态蓝色实线，但生产 route 只有 JSON events，`API-Contract.md` 和未完成的 AC-038 均把 SSE/WS 定为未来 adapter。
- MEDIUM：全景图 `infra-db` 无条件声明 PostgreSQL RLS；迁移、生产代码和测试没有 `CREATE POLICY` / `ENABLE ROW LEVEL SECURITY`，当前证据只是 repository tenant filtering。
- LOW（维护性）：`templates/service-app/configs/profiles/service.yaml` 仍称 Redis worker queue 为“未来 seam”，与 Phase 13 当前 durable queue 冲突。
- LOW（维护性）：全景 drawio 的 `e-hitl-access` 只有绝对坐标、没有 source/target 绑定；移动节点时回边不会跟随。

修复：

- 技术图将 SSE formatter 改为黄色虚线未来态，明确当前仅 JSON events seam；同步 Excalidraw 和 PNG。
- 全景图改为“当前 repository tenant filtering；未来 RLS”；同步 Excalidraw 和 PNG。
- service profile 注释改为当前 Redis Streams durable queue，并说明 pickup/reclaim、持久化后 fenced ACK 与 reachability smoke。
- drawio HITL 回边绑定 `runtime-hitl -> layer-access`，重路由到 Access/Runtime 间空白带；`validate.py` 仍为 `0 error(s), 0 warning(s)`，PNG 无节点/标签遮挡。

这些修改只纠正既有实现和文档表达，不改变行为契约，因此未创建 OpenSpec change。任何第二轮 PASS 均因上述 tracked diff 失效。

### 6.5 架构第三轮三审与修复

第三轮 reviewer G、H 判定 Stage 1/2 PASS；reviewer I 判定 Stage 1 FAIL（1 MEDIUM、1 LOW），Stage 2 PASS。主 Agent 直接读取 adapter、worker、ADR 和图源后采纳两项，不以 2:1 票数驳回：

- MEDIUM：部署图旧链路为 `API -> Redis Queue Adapter -> Redis -> Worker`，把 `pickup/reclaim` 放在 Redis 直连 worker 的边上；实际 worker 与 API 都调用同一 `RedisRunQueue` library seam，Redis 命令封装在 adapter 内。
- LOW：全景图右翼 `Runtime Spans / GraphState / 分支 / HITL` 将当前 HITL span 与未来 GraphState/分支混写成同一当前态节点。

修复：

- service profile 区域改为左右对称的稳定边界：API 与 worker 分别调用共享 `Redis Queue Adapter`；adapter 再访问 Redis Streams。边语义分别为 `enqueue Queue DTO`、`pickup / reclaim / fenced ACK`、`XADD / XREADGROUP / XAUTOCLAIM / XACK`，不再存在 Redis 直连 worker。
- 同步移动 deployment Excalidraw 节点、箭头、端点 binding 和三条标签；重导 PNG 后以原分辨率视觉核验。第一次导出发现 API 标签压线，随后将边改走列间空白带并把标签移至 API 下方；最终 `validate.py` 为 `0 error(s), 0 warning(s)`。
- 全景观测节点改为“当前：run/checkpoint/HITL；未来：GraphState/分支”，同步 Excalidraw 与 PNG。

这些仍是架构表达修复，不改变生产行为或 OpenSpec 契约。第三轮 PASS 因 tracked diff 全部失效。

### 6.6 架构第四轮三审与修复

第四轮 reviewer J 判定 Stage 1 PASS、Stage 2 FAIL（1 MEDIUM）；K 判定 Stage 1 FAIL、Stage 2 PASS WITH NOTES（1 MEDIUM、1 LOW）；L 判定 PASS WITH LOW（1 LOW）。主 Agent 采纳全部有原始证据的发现：

- MEDIUM：部署图 API -> RuntimeComponents 边穿过 API 正文；运行图 HTTP -> RunCreateRequest 边沿 orchestrator 左边框上行，PNG 容易误读为 HTTP 直达 orchestrator。
- MEDIUM：运行图、技术图和 service profile 虽未真的让 worker import Redis，但措辞仍写成“Redis queue -> worker / Redis queue 消费”，未统一表达 API 和 worker 都只依赖 `RunQueue` protocol、composition root 注入 `RedisRunQueue`。
- LOW：全景 Excalidraw 的 feedback 文本以换行替代 drawio 中 Phoenix 后的分号，未达到逐字一致。

修复：

- deployment `e-svc-api-runtime` 显式从 API 底部进入 RuntimeComponents 顶部；runtime `e-api-req` 走 Access/Runtime 两列之间的空白带再进入请求 DTO，重新导出 PNG 后无正文/边框视觉歧义。
- runtime 节点改为 `RunQueue seam -> worker / RedisRunQueue adapter 隐藏 Redis`；technical worker 改为 `RunQueue protocol pickup/reclaim / Redis adapter 隔离`；service profile 注释明确 API/worker 只依赖 protocol、composition root 注入 adapter。
- Excalidraw 同步文本、箭头几何与 binding；全景 feedback 补回分号。
- 四份 drawio 再次全部 `0 error(s), 0 warning(s)`，`git diff --check` 通过，三张受影响 PNG 已逐张视觉核验。

第四轮 PASS 因 tracked diff 全部失效。

### 6.7 架构后续复审：交叉、migration 与能力状态

后续 reviewer 始终由 3 个 fresh 实例读取相同完整范围，禁止读取本报告、其他 reviewer 结论或主 Agent 摘要。

| 轮次 | 三份独立结论 | 主 Agent 复核与处置 |
|---|---|---|
| 运行图线交叉复审 | A 报 1 个可读性 LOW；B/C PASS | 采纳 A。先重路由，确认图拓扑在不引入新歧义的前提下无法完全平面化后，在 Draw.io 使用 arc line-jump，在 Excalidraw 使用白色 bridge 元素；两者均有图例“线桥=交叉不连接”、无交点 binding。 |
| migration/能力状态复审 | R1：1 MEDIUM + 1 LOW；R2：PASS；R3：2 LOW | 全部按原始证据采纳。deployment 新增 one-shot migration 和 `PostgreSQL healthy -> migration -> API/worker`；全景图将 Ragas/DeepEval、session cache 改为未来可选，并把当前 Redis 职责限定为 Streams durable RunQueue。 |
| 当前/未来口径复审 | R1：2 MEDIUM；R2：1 LOW；R3：1 MEDIUM + 1 LOW | 采纳 Pydantic Evals 未接入、Docker secret/SecretProvider 未实现、Graph 主链路目标态和字号元数据发现。全景图改为当前 internal EvalRunner/pytest，Pydantic Evals/Ragas/DeepEval 可选未接入；secret 分为当前 env、待实现 SecretProvider/Docker secret、未来 KMS；Graph 标签补“目标”。 |
| 字体元数据复审 | 三名 reviewer 均 Stage 1 PASS、Stage 2 PASS WITH NOTES，均报告 migration 文本缺少 `fontString` 的 LOW | 采纳 deployment 单文件局部漂移，补 `fontString="10px Helvetica"` 与 baseline。驳回“给全景图 66 个文本补字段”的扩大修复：全景图 66/66 一致不使用该派生字段，不存在文件内部漂移。 |

任何一轮后的架构 tracked diff 都使该轮 PASS 失效；只有 6.9 的最终三审结论用于冻结。

### 6.8 架构最终结构与视觉证据

- Draw.io validator：deployment `0 error / 1 warning`；runtime/trust `0 error / 1 warning`；technical、overview 均 `0/0`。
- 两个 warning 是受控非连接交叉：deployment 的 `e-migration-worker` 与 runtime 的 `e-hitl-checkpoint` 均使用 `jumpStyle=arc`；图例明确“线桥=交叉不连接”。
- Excalidraw 在同一交点使用白色 bridge 元素，bridge 与跨线箭头同组、先绘 bridge 后绘箭头，且交点无 binding。四图全部箭头都有 start/end binding 与 reciprocal backref。
- Draw.io / Excalidraw 箭头数逐图一致：deployment `22/22`、runtime `20/20`、technical `18/18`、overview `16/16`；规范化可见文本集合逐图一致。
- 四张 PNG 均从最终 Draw.io 重新导出为 2000px 宽 RGB 图，原尺寸视觉检查无空白、裁切、文字遮挡或语义颜色漂移。
- 指定六个架构合同测试最终复跑：`40 passed`；`jq empty docs/architecture/*.excalidraw` 与 `git diff --check` 通过。

### 6.9 架构最终三审与冻结结论

最终 reviewer A、B、C 均在组合 hash `cb53603743edbced8b8f78f8181a30c7b9f0d4416320926908121cb2a33f444a` 上独立给出：

- Stage 1 / Spec 轴：PASS。
- Stage 2 / Standards 轴：PASS。
- HIGH / MEDIUM / LOW：无。
- 实际验证：四个 validator、四个 Excalidraw JSON、`git diff --check`、指定六文件 pytest `40 passed`；均未用 local smoke 代替真实 service smoke。

主 Agent 复核三份结论后确认：架构图只作为候选架构基准冻结；尚未与 Product Spec 共同形成第一层基准。Phase 14、15 仍为待实现。

### 6.10 架构最终 SHA-256

| 文件 | SHA-256 |
|---|---|
| `docs/architecture/README.md` | `f152992232815e00479436ff5dc0173e9c4c557b64424968069d75fb278e3b61` |
| `docs/architecture/agent-harness-deployment-boundaries.drawio` | `b2ef274d9e13b9706c5f31a41364847f1c8b2071dca49cc1e0cc4a8744266dae` |
| `docs/architecture/agent-harness-deployment-boundaries.excalidraw` | `82d2e7f177f4fe8366be82ca91120bd5ef23c2e65291e06984892610d30d932b` |
| `docs/architecture/agent-harness-deployment-boundaries.png` | `e1ebe500631ff5919c80a288d79618efa9c4375865356355fb54ec98ee92d26b` |
| `docs/architecture/agent-harness-runtime-trust-boundaries.drawio` | `20e5dfabfacc8f996ae21998d455da06d3ecdc0e7fe916df15c742eb60876408` |
| `docs/architecture/agent-harness-runtime-trust-boundaries.excalidraw` | `9ab7f95a65ed66505c565491b6f3054ad1cf3e5f4cb4db351608ec64ae639c22` |
| `docs/architecture/agent-harness-runtime-trust-boundaries.png` | `81bf5fb87687e457ba4346a13795661a4d032b8173b72fda11e228ecf80154e7` |
| `docs/architecture/agent-harness-technical-architecture.drawio` | `aebcd5fa3a88f94cde7d82ac5613610874b1f6515622eb2d74fb654f3b64a591` |
| `docs/architecture/agent-harness-technical-architecture.excalidraw` | `fcab2497fc9704d7ff26b668991c29e8c4e90d867c212ebbdef7552f8fc1300d` |
| `docs/architecture/agent-harness-technical-architecture.png` | `2eb7ec47a5804559d1a2348f729e38fceb54c134e806919ed9fa02f2a361a5ce` |
| `docs/architecture/pydantic-ai-agent-architecture.drawio` | `6f498bdf9fc26447ed1042dedd6004c9a5be5e8f42aec756474192c9fdbd0ba0` |
| `docs/architecture/pydantic-ai-agent-architecture.excalidraw` | `b502c951c592d66b765eb2f875488b052531d78c98d68412f35cc547ba4f0f9b` |
| `docs/architecture/pydantic-ai-agent-architecture.png` | `d7ab93e17c9d854c1ebfa1a2bd8d4f69fe2e3aca236a27d53224721fbb176383` |

### 6.11 Product Spec 首轮三审

本轮有效 reviewer A、C、D 均为 fresh 实例，读取相同完整范围和原始文件，禁止读取本报告、其他 reviewer 结论或主 Agent 摘要。原 reviewer B 与第一次 replacement 在工具层未返回最终报告，已中断且不计入三份结论；D 为重新创建的 fresh replacement。三份有效结论如下：

| Reviewer | Stage 1 / Spec 轴 | Stage 2 / Standards 轴 | 主要结论 |
|---|---|---|---|
| A | FAIL | 因 HIGH 未执行 | delegation 执行闭环、SSE transport、Phase 15 CI/release 缺失；model cost/latency trace、NFR 证据和文档/合规仍有缺口。 |
| C | FAIL | 因 HIGH 未执行 | delegation 只有手工 summary seam；DEV-PLAN 的“无阻塞”与 P0 真实缺口矛盾；SSE、SecretProvider 需要产品裁决。 |
| D | FAIL | 因 HIGH 未执行 | delegation、SSE、Phase 14/15、SecretProvider 为高严重度缺口；数据模型、核心字段表、NFR 映射和待确认问题已漂移。 |

主 Agent 不按三份报告的一致票数裁决，而是直接复核原始证据：

- **采纳 HIGH：真实 delegation 未实现。** `Product-Spec.md:534-544` 要求受控 agent A -> B 调用和 parent run 的 usage/budget/trace 聚合；`registry.py:192-238` 只有 edge check 和由调用方传入结果字段的 summary DTO；唯一合同测试 `test_agent_registry_model_context_contracts.py:345` 手工填入 child run、usage 和 trace。归档 design 明确 Phase 6 只做摘要、不做跨 agent 调度，说明 Phase 6 的实际交付范围与产品 P0 行为不等价。
- **采纳 HIGH：SSE transport 未实现。** `Product-Spec.md:811-887` 要求 SSE 和 `Last-Event-ID -> seq`；`runs.py:275` 只有 JSON events route，`sse.py` 只有 frame formatter；`API-Contract.md:835-848` 和架构图均明确当前只有 JSON seam。
- **采纳 MEDIUM：model token/cost/latency trace 不完整。** `Product-Spec.md:760` 为 MUST；`ModelResponse` 只有 token 和 latency，没有 cost；`ModelRouter` 不发布统一 model request/usage trace。该项需要聚焦 OpenSpec behavior change，不可用文档勾选掩盖。
- **采纳 HIGH：SecretProvider/Docker secret 是孤儿 P0 声明。** `Product-Spec.md:132` 明确写 P0，但 22 个 REQ、64 个 AC、DEV-PLAN 和 OpenSpec 都没有可验收交付；架构正确标为待实现。
- **采纳 MEDIUM：Product Spec 状态和数据模型漂移。** 至少 18 个未勾选 AC 有实现/测试证据；AC-050 的 red-green 历史无法仅由当前树证明。Phase 12.5 的 dataset split、experiment、acceptance record 未进入产品数据模型，核心字段表和“所有核心实体带 tenant_id”的规则也不一致。
- **采纳 MEDIUM：NFR 缺少验收映射。** fake run 时延与 SSE 首事件时延没有稳定 AC/OpenSpec/test evidence；后者还依赖未实现 transport。
- **采纳 LOW：运行/信任图把 InputGuardrail 画宽。** 图源写 `external/tool checks`，而 `InputGuardrail` 只处理 API/CLI 输入；tool/MCP/retrieval 分别由 registry/output guard/context 边界处理。该维护性缺陷将在产品决策后同步修图并触发架构重审。
- **暂不采纳“超过 500 行必须立即拆分”为本阶段缺陷。** 这是 code-review Skill 的维护性启发式，不是 Product Spec/架构双向对账结论；留到 capability Stage 2 逐文件判断，避免在产品阶段进行无证据的大范围重构。
- **驳回“Phase 14/15 缺失本身使 Phase 1-13 基线失败”。** Phase 14/15 明确不在本轮实施范围；真正缺陷是 DEV-PLAN 未如实列出 Phase 1-13 后仍存在的 P0 行为缺口，不能把待实现 Phase 本身当作本轮必须实现的缺陷。

首轮定向合同测试覆盖 workspace/package、typed config、identity、auth、tool 边界、registry/model/context、retrieval 和 observability，共 `52 passed`。该结果只用于 AC 证据裁决，不替代最终全量验证。

### 6.12 Product Spec 待决策项

以下项目会改变 P0 范围或新增行为 Phase，按仓库规则暂停并请求用户决定：

1. **Delegation：**保留 P0 真实执行与 parent 聚合，并在 Phase 14/15 前新增聚焦实现阶段；或把 P0 收窄为 registry/edge/summary seam。
2. **SSE：**保留 P0 transport、`Last-Event-ID` 续传和首事件时延验收，并在 Phase 14/15 前新增聚焦实现阶段；或正式降为 P1。
3. **SecretProvider：**保留 P0 SecretProvider + env/Docker secret，并补 REQ/AC/Phase；或将 P0 收窄为 env 注入、redaction 与部署 secret 配置约定，把 provider adapter 放到 P1。

推荐保留 delegation 与 SSE 的既有 P0 承诺，并新增 Phase 13.6/13.7 聚焦 change；SecretProvider 建议 P0 只保留 env/Docker secret 的配置消费与 redaction，不引入抽象 provider，避免为了尚无第二实现的接口制造空抽象。Graph 节点和 Redis session cache 已有更高优先级原文支持其属于目标/P1，可直接修正文案，无需用户决策。

### 6.13 产品决策、修复与第一层复审

用户选择推荐方案并形成明确产品边界：

1. 真实 delegation 与 parent usage/budget/trace 聚合保留 P0，在 Phase 14/15 前新增聚焦补缺阶段。
2. SSE transport、`Last-Event-ID` 续读与首 frame 性能保留 P0，在 Phase 14/15 前新增聚焦补缺阶段。
3. P0 secret 范围只含 env/Docker secret file consumption、redaction 与部署装配；`SecretProvider`/KMS adapter 为 P1。

据此修复：

- Product Spec 增加 RUN-006 和 AC-063 至 AC-066；AC-017 改为未完成；AC-050/051 改为可验证的覆盖矩阵与 CI 分离门禁；Phase 12.5 实体及持久化 `tenant_id` 规则补齐。
- 架构四组图把 P0 待实现和 P1/未来拆分开；InputGuardrail 限定为 API/CLI input；Redis 当前仅承担 Streams durable queue；secret 当前/P0/P1 状态分开。
- Product changelog v1.6 记录上述范围决策。技术图黄色图例最终改为中性的“待实现”，节点正文负责说明 P0 补缺或 Phase 14/15，避免一个颜色承载两种状态。

第一层复审的三个 fresh reviewer 均判 Stage 1 FAIL。主 Agent没有按一致票数直接裁决，而是逐条对账：

- 采纳下游 `DEV-PLAN.md`、`API-Contract.md` 与 OpenSpec 尚未承接新增 P0 行为的发现；这使“共同冻结”为时过早，但不推翻 Product/架构自身的产品决策。
- 采纳技术图黄色图例混合 `P0 待实现 / Phase 14-15` 的维护性发现，改为“待实现”并重导 PNG。
- 驳回“Phase 14/15 未完成本身使 Phase 1-13 产品基准失败”；本轮明确禁止实施这两阶段，真实缺陷是其前置 P0 gap 没有计划所有权。

任何架构旧 PASS 均因 Product 对账后的 tracked diff 失效；最终第一层冻结必须与 DEV/API/OpenSpec 的最终 hash 一起重新三审。

### 6.14 DEV-PLAN / API Contract 首轮三审与裁决

三个 fresh reviewer 在组合 hash `7a997c2f966000de443799398ef86f7c407cf9c6ca3e54c47d28ae6fc43fd2f4` 上读取相同完整范围。结论：

| Reviewer | Stage 1 | Stage 2 | 主要发现 |
|---|---|---|---|
| A | FAIL | FAIL | RUN-002 当前/目标 schema 混写、AC-050 无明确 owner、13.7/13.9 共享 smoke 文件却声称可并行、逐 change 三审规则不够精确、技术图图例混合状态。 |
| B | FAIL | PASS | AC-017 未纳入 RUN-006、RUN-002 当前/目标混写、delegation 同 key 异 body 未定义、Phase 15 遗漏 Makefile/integration/template 版本范围，另有 AC 编号追踪 LOW。 |
| C | FAIL | PASS | ModelUsageEvidence 缺 `tenant_id`、delegation deny evidence 自相矛盾、RUN-002/OpenAPI 额外 status 与 202 状态漂移，另有直接 AC 索引和实施顺序 LOW。 |

主 Agent逐条复核并采纳所有有原始证据的 HIGH/MEDIUM 与维护性 LOW：

- `API-Contract.md` 将 RUN-002 分成当前 `RunCreateResponse` 和 Phase 13.8 目标 `RunDetailResponse`；记录 run router 共享 responses 造成的额外 status，并要求 drift test 同时拒绝缺失和多余状态。
- 新增 Phase 13.5 `run-openapi-contract-accuracy`，只修 RUN-001 至 RUN-005 当前 OpenAPI 精确性，不提前实现 RUN-006 或 delegation。
- `ModelUsageEvidence` 强制 `tenant_id`；delegation deny 只允许一条脱敏 policy/audit evidence，禁止 child/queue/provider/业务事件副作用。
- delegation 幂等以规范化 request hash 为基准：同 key 同 hash 重放，同 key 异 hash 返回 `delegation.idempotency_conflict` 且零业务副作用。
- Phase 13.9 明确依赖 13.7 并顺序接管 `scripts/smoke_local.py`；13.5-13.9 每个 change 均需 3 个 fresh reviewer 两阶段 PASS，再由 3 个新的 fresh reviewer 做多 change 联合审查。
- Phase 14 直接承接 AC-049；Phase 15 直接承接 AC-050/051/053-056/058，并把根 `Makefile`、`make integration`、CI 中 `make quality`/`make test` 分离结果、模板兼容版本范围和 acceptance matrix 写入交付与验收。
- Redis 风险说明从不存在的当前 cache adapter 改为 durable queue adapter；技术架构图图例改为“待实现”。

修复后的审查组合包括 Product、Product changelog、DEV、API、四组架构三格式、架构 README 与 service profile，SHA-256 聚合值为 `18170c8bf059679dc4786f2641770299e7acef073e45433798722245ac77f9de`。该组合正在进行新的三审，首轮 PASS 已全部失效。

### 6.15 DEV-PLAN / API Contract 第二轮三审与修复

平台并发按 2+1 执行。前两名 fresh reviewer 在旧组合上均判 Stage 1/2 FAIL；两份报告的发现分别复核如下：

| 发现 | 主 Agent 裁决 | 原始证据与修复 |
|---|---|---|
| 13.5-13.9 共享 `runs.py`、schema、`app/runtime.py`、`app/main.py` 与 smoke 文件，但 DAG 没有唯一合并顺序 | 采纳 MEDIUM | `DEV-PLAN.md` 各阶段关键文件确有重叠；现改为 `13.5 -> 13.6 -> 13.7 -> 13.8 -> 13.9 -> 14 -> 15` 严格串行，并新增逐文件接力所有权和累计三审规则。 |
| 原 DAG 允许 13.7 后直接进入 Phase 14 | 采纳 MEDIUM | 依赖图缩进与顶部“13.5-13.9 全闭环后才能开始 14”冲突；已按上述严格链修复。 |
| API Contract 状态枚举只允许 `已实现/规划中/保留路径`，RUN-006 与 DLG/MOD/CFG 却写 `P0 待实现` | 采纳维护性 LOW | 四处状态统一为 `规划中（P0）`；长期契约中的阶段号替换为稳定 change ID。 |
| 生产 docstring、migration 说明和合同测试仍以 `Phase N` 描述长期行为 | 采纳维护性 LOW | 扫描 `packages/templates/examples/scripts/tests` 后清除全部阶段标签，保留稳定能力名；migration downgrade 错误改为 `eval experiment evidence exists`，不改变拒绝条件。 |
| 指定组合 hash 无法复现 | 采纳证据流程缺陷 | 上一轮只给了结果、未给组合算法，且文字误称包含 changelog。新快照统一以 `LC_ALL=C git diff --binary -- . ':(exclude)docs/phase-1-13-baseline-audit.md' \| shasum -a 256` 计算，结果 `34a2ce4e34f4a7edd59ac0fc478da223de8f999eaeadc371a2e9a1a5f0e8acb5`。 |

修复后定向工具/观测/存储/eval 合同测试通过，`make quality` 与 `git diff --check` 通过。由于 tracked diff 已产生，第二轮的任何局部 PASS 全部失效；新三审只接受可复现的 `34a2ce4e...acb5` 快照。

### 6.16 DEV-PLAN / API Contract 最终快照三审

最终轮固定审查快照为：

```text
LC_ALL=C git diff --binary -- . ':(exclude)docs/phase-1-13-baseline-audit.md' | shasum -a 256
34a2ce4e34f4a7edd59ac0fc478da223de8f999eaeadc371a2e9a1a5f0e8acb5  -
```

本轮按平台并发限制采用 2+1。三份有效独立判断及主 Agent 裁决如下；外部 reviewer 中另有一次实例把 68 个 AC 误写为 66，另一次反复重跑同一离线测试且未形成最终报告，两者均不计入有效结论。

| Reviewer | Stage 1 | Stage 2 | 结论与主 Agent 复核 |
|---|---|---|---|
| A | PASS | PASS | 直接核对 22 REQ、68 AC、51 已完成/17 未完成、严格 DAG、API 当前/目标边界、四组图与测试证据；快照 hash 一致，0 个有效发现。 |
| B | PASS | FAIL | 唯一 Stage 2 发现按物理行数把 5 个文件判为超过 500 行。主 Agent 依据 code-review skill 的有效行口径，用 AST/tokenize 排除空行、注释与 docstring 后得到 496、477、466、465、483；均低于 500，且逐文件职责内聚，故有证据驳回。 |
| C（fresh replacement） | PASS | PASS | 独立复核相同完整范围，确认 22/68/17 计数、迁移只有维护文本变化、有效行计数与职责边界；快照 hash 一致，0 个有效发现。 |

本轮不是按 2:1 票数判 PASS。Reviewer B 的 Stage 2 结论因适用了错误计数口径而被驳回；原始文件的有效行计数和职责核验才是裁决依据。审查后没有相关 tracked diff，因此该快照的 Stage 1/2 PASS 有效。

第一层基准与计划/契约最终文件 hash：

| 文件 | SHA-256 |
|---|---|
| `Product-Spec.md` | `3de8bf99dcb93d94c77ef2386f4e72b596acb20e3548c61eff5c622a9d4eeecd` |
| `Product-Spec-CHANGELOG.md` | `b8cf76e22bba8184b11626c905abd2a78cc648868e6d1c27fddf09f5d4469ab5` |
| `DEV-PLAN.md` | `87dd84a4ccb0477f7de86ee23e6068cc0909c6b975508be647fbbea8aee7415e` |
| `API-Contract.md` | `381f6310760af3c949018a4e6fc4bf3d3f233657899d623845e52a08554ba7f1` |
| `docs/architecture/README.md` | `f152992232815e00479436ff5dc0173e9c4c557b64424968069d75fb278e3b61` |
| `agent-harness-deployment-boundaries.drawio` | `a01370c9a06aadfa74d0445e3c1ed1c08e47ab36247519d39891919278163d08` |
| `agent-harness-deployment-boundaries.excalidraw` | `164c83e1bfa3667e551207e5fb7618e701ac49181515620e564379018aadecb6` |
| `agent-harness-deployment-boundaries.png` | `43209ab30ceadac0385d94bb9fe3614f13f6d4599c816134822c930bb01b463b` |
| `agent-harness-runtime-trust-boundaries.drawio` | `9dbc0ec779e1b0a058f7c0cc3f4f313fe3890bae8295959c364fa38af86e62ac` |
| `agent-harness-runtime-trust-boundaries.excalidraw` | `8db3eaeb1aa5b2d833b47bf7a6c152856fc51862cc0b05db1f2a12db7d23bb75` |
| `agent-harness-runtime-trust-boundaries.png` | `f9e23c337ddb26f7e41a8ebbfccb97ea2a9cf6521b738a6d66dcb12d2ee5c6b4` |
| `agent-harness-technical-architecture.drawio` | `eefff7ee27bab52d3d273bf5d950a846aead9dfa6193dfdc082927fe39f7ca32` |
| `agent-harness-technical-architecture.excalidraw` | `4534d84212bce28b39f1b066f9e8e01c00a4ed571aa5394ca0214218204a8297` |
| `agent-harness-technical-architecture.png` | `400837fe5725d9ca09bae3b7cfe7cba1402170e368a5875f7ba63e3fa254baca` |
| `pydantic-ai-agent-architecture.drawio` | `d80b7e086d3b6a08f94aea3b7147efce0d1d354f0f686369560dddccdf1a9482` |
| `pydantic-ai-agent-architecture.excalidraw` | `6128a9d4ba7355a2b02568d535cad788ea2f182e39c1d6199ac7db52a1f75d0f` |
| `pydantic-ai-agent-architecture.png` | `1d53b9534cee91c12eca36fd215ad3b99e56044d4fc120e2855153e7b86abaf4` |
| `templates/service-app/configs/profiles/service.yaml` | `55ef761edab078f49b70bdb1507ac6586389a9fefd73416fafef34f5db4ed5f0` |

### 6.17 OpenSpec 主规格与归档来源初审

三个 fresh reviewer 读取相同完整范围；均禁止读取本报告和其他 reviewer 结论。结论不是按票数裁决：

| Reviewer | Stage 1 | Stage 2 | 主要发现 |
|---|---|---|---|
| A | FAIL | HIGH 后未执行 | delegation 主规格把摘要接缝写成真实执行闭环；一个归档 change 缺 metadata；三项去阶段化措辞没有精确 delta；长期规格残留阶段标签。 |
| B | FAIL | HIGH 后未执行 | delegation 同一 HIGH；当前 OpenAPI 精确长期契约被 route 共享 response 状态违反且测试是假阴性；两个 Purpose 为 `TBD`；长期阶段标签。 |
| C | FAIL | PASS WITH NOTES | 两个 Purpose 为 `TBD`；DEV-PLAN 的“本轮基线报告”没有稳定路径。未识别 delegation 缺口，但该遗漏不能推翻生产代码和上游原文证据。 |

主 Agent 逐项复核并裁决：

- **采纳 HIGH：delegation 主规格状态虚高。** 归档 design 明确只做摘要、不做完整 workflow；生产 `registry.py:192-238` 只校验 edge 并包装调用方 refs；Product AC-015/016、DEV Phase 13.8 和 API DLG-001 均明确真实执行未完成。
- **采纳 MEDIUM：OpenAPI 长期契约当前被违反。** `service-app-shell` 主规格要求不得缺失或额外；`runs.py` router 共享 errors 导致 operation 暴露额外状态，现有测试只做子集判断。由 `run-openapi-contract-accuracy` 修复。
- **采纳维护性缺陷：** `canonical-events-artifacts`、`storage-migration-uow` 的 Purpose 为 archive `TBD`；quality/eval 主规格残留 `Phase 1/10`；observability 归档缺 `.openspec.yaml`；DEV-PLAN 报告引用缺稳定路径。
- **记录但不把稳定措辞改名当行为丢失。** Tool/Auth 三个标题已去除阶段号且 requirement/scenario 行为完整；归档是来源证据，不回写伪造历史 delta，在本报告记录精确追溯变化。

23 个主规格全部具有至少一个归档 delta 来源，20 个归档 change 的 tasks 均无未勾选项。多来源关系如下：

| 主规格 | 归档来源 |
|---|---|
| `agent-registry-model-context` | `2026-07-08-agent-registry-model-context`、`2026-07-10-p0-example-agent-flows` |
| `auth-policy-hitl-approvals` | `2026-07-08-auth-policy-hitl-approvals`、`2026-07-10-p0-example-agent-flows`、`2026-07-12-split-api-worker-runtime` |
| `runtime-checkpoint-runs` | `2026-07-06-runtime-checkpoint-runs`、`2026-07-10-p0-example-agent-flows`、`2026-07-12-split-api-worker-runtime` |
| `service-app-shell` | bootstrap、service-app-template-surface、service-profile-deployment-proof、split-api-worker-runtime 四个归档 delta |
| `tool-execution-boundaries` | `2026-07-08-tool-execution-boundaries`、`2026-07-10-p0-example-agent-flows` |
| 其余 18 个主规格 | 各至少一个同名或上游聚合归档 delta；git 首次引入 SHA 与完整路径清单保存在本阶段命令证据中。 |

已完成修复：主 delegation 规格恢复为当前 edge-check/调用方摘要接缝；真实执行进入 active delta；两个 Purpose 补齐；阶段标签改为稳定能力名；缺失 metadata 补齐；DEV-PLAN 指向 `docs/phase-1-13-baseline-audit.md`。五个行为 change 均已创建并保持未实施：`run-openapi-contract-accuracy`、`config-secret-file-loading`、`model-usage-evidence`、`agent-delegation-execution`、`sse-event-streaming`。当前 `openspec validate --all --strict` 为 `28 passed, 0 failed`，该结果仅证明可解析。

### 6.18 DEV-PLAN / API Contract 补充三审与 OpenSpec 状态修复

6.16 的 `API-Contract.md` hash 仍为 `381f6310760af3c949018a4e6fc4bf3d3f233657899d623845e52a08554ba7f1`，但 6.17 为稳定报告路径修改了 `DEV-PLAN.md`，其 hash 从 `87dd84a4...` 变为 `de50fcc3...`，因此 6.16 的 PASS 按 tracked diff 规则失效。补充轮固定快照为：

```text
LC_ALL=C git diff --binary -- . ':(exclude)docs/phase-1-13-baseline-audit.md' | shasum -a 256
ffdbc8688439b1b2ee8a21cf1db75a040765c3f3b27093263aedbdb345e4366b  -
```

三个 fresh reviewer 读取相同完整范围，均未读取本报告或其他 reviewer 结论：

| Reviewer | Stage 1 | Stage 2 | 主要结论 |
|---|---|---|---|
| A | FAIL | PASS | 发现 DEV-PLAN 仍声称无 active change、下一步仍是创建 change；22 REQ、68 AC、51 完成/17 未完成、DAG、API 当前/目标边界与定向测试其余通过。 |
| B | FAIL | PASS WITH NOTES | 同一状态漂移；发现 `ModelUsageEvidence.trace_id` 在 API Contract 为必填、active delta 却写为可选；发现 `AC-053/054` 缩写不利于字面追踪。 |
| C | FAIL | FAIL | 同一状态漂移；另称未 tracked 的本报告路径不能作为 AC-050 当前证据，并称 API Contract 的 P0/P1 叙事违反长期契约语言纪律。 |

主 Agent 逐项裁决：

- **采纳 MEDIUM：DEV-PLAN active change 状态过期。** `uv run openspec list --json` 显示五个 change 均为 `in-progress`、0 个任务完成，而 `DEV-PLAN.md:14,23,25,28` 仍写无 active change 和下一步创建 change。
- **采纳 MEDIUM：`trace_id` 必填性冲突。** API Contract 5.29 将 `trace_id` 标为必填，Product AC-064 要求同一 run/trace 关联；`model-usage-evidence` 的 delta、design 和 tasks 却写成可选，实施时会造成 DTO、事件与 parent aggregation 漂移。
- **采纳维护性 LOW：AC 标识缩写。** `AC-050/051/053/054` 与 `AC-051/053-058` 对人可读，但不能稳定支持字面追踪，改为逐项完整标识。
- **驳回“报告未 tracked 即不可作为当前审查证据”。** 用户明确要求本目标把持久化结论写到该路径，文件真实存在且目标尚未收口；Product AC-050 仍保持未完成，DEV-PLAN 也明确 Phase 15 将把当前基线矩阵复用到 `docs/p0-acceptance-matrix.md`，没有把临时工作区状态冒充 CI/发布证据。
- **驳回 API Contract 的 P0/P1 语言问题。** Product Spec 将 P0/P1 定义为稳定产品范围，API Contract 用它区分当前、P0 待补和 P1 可选协议能力；code-review skill 对真实稳定标识和产品语言明确允许在说明理由后放行。

已修复 DEV-PLAN 的 active change 状态与下一步，五个 change 仍为 0 个任务完成，Phase 13.5-13.9、14、15 均未标记完成；已展开 AC 稳定标识；已把 `model-usage-evidence` 的 delta、design、tasks 统一为可选 `request_id`、必填 `trace_id`。验证结果：

- `uv run openspec validate model-usage-evidence --type change --strict`：PASS。
- `uv run openspec validate --all --strict`：`28 passed, 0 failed`，仅作为可解析证据。
- `git diff --check`：PASS。
- 修复后 `DEV-PLAN.md` SHA-256：`a4eafa9bfafcee55809db677d9d89048d430371e0e8a59e973ffccaa77b23644`。
- 修复后排除本报告的工作区快照：`0018a83a056e3b170ea67176c8f9c09549a6dddb0442b21d1f9409544f7ecfbb`。

由于以上 tracked diff，补充轮三份 FAIL/PASS 局部结论均不再作为最终门禁；下一轮必须由三个新的 fresh reviewer 对修复后完整范围重新审查。

### 6.19 DEV-PLAN / API Contract 修复后最终三审

修复后固定快照：

```text
LC_ALL=C git diff --binary -- . ':(exclude)docs/phase-1-13-baseline-audit.md' | shasum -a 256
0018a83a056e3b170ea67176c8f9c09549a6dddb0442b21d1f9409544f7ecfbb  -
```

三个新的 fresh reviewer 均读取第一层基准、DEV-PLAN、API Contract、service profile、运行时 OpenAPI 和 `model-usage-evidence` 全部 change 产物；均未读取本报告或其他 reviewer 结论：

| Reviewer | Stage 1 | Stage 2 | 结论与证据 |
|---|---|---|---|
| A | PASS | PASS | 22 REQ、68 AC、51 完成/17 未完成，五个 active change 均 0 个任务完成；39 个聚焦合同测试通过；单 change 与全量 OpenSpec 严格校验通过；开始/结束 hash 一致。 |
| B | PASS | PASS | 同一计数与 DAG；运行时 OpenAPI 证明 RUN-002～005 额外状态和 RUN-006 缺失仍被如实标为待补；90 个相关合同测试通过；开始/结束 hash 一致。 |
| C | PASS | PASS | 同一计数、active change 和 Phase 14/15 边界；35 个 model/embedding/observability/OpenAPI 合同测试通过；开始/结束 hash 一致。 |

主 Agent 没有按三票裁决，而是复核原始文件与运行输出：

- Product 计数脚本仍为 22 REQ、68 唯一 AC、51 完成、17 未完成；17 项与 Phase 13.5-13.9、14、15 的所有权一致。
- `uv run openspec list --json` 显示五个 active change 均为 `in-progress` 且 0 个任务完成；DEV-PLAN 当前状态与此一致。
- API Contract 5.29、`model-usage-evidence` proposal/spec/design/tasks 对 `request_id` 可选、`trace_id` 必填的定义一致；delegation、SSE、Phase 14/15 均保持范围外。
- 运行时 OpenAPI 仍真实暴露已记录的额外 run status，RUN-006、ModelUsageEvidence、真实 delegation 与 secret file 尚未实现；文档没有把 future seam 写成当前能力。
- 三名 reviewer 的结束 hash 均为 `0018a83a056e3b170ea67176c8f9c09549a6dddb0442b21d1f9409544f7ecfbb`，审查后没有相关 tracked diff。

因此第 4 点在该快照上达到 Stage 1/2 PASS。最终关键 hash：

| 文件 | SHA-256 |
|---|---|
| `DEV-PLAN.md` | `a4eafa9bfafcee55809db677d9d89048d430371e0e8a59e973ffccaa77b23644` |
| `API-Contract.md` | `381f6310760af3c949018a4e6fc4bf3d3f233657899d623845e52a08554ba7f1` |
| `model-usage-evidence/proposal.md` | `1b93302ea9b2f3e2b0da61026894a8810d0c1807a9622f47aaf9b4b4f52e3771` |
| `model-usage-evidence/design.md` | `56dca338da3ea2036c09745b37ab68d778931cf8ac83fe974489a774e63a0443` |
| `model-usage-evidence/tasks.md` | `ebfd1a13249b17bad5a3f35e0bbd2732e52bd8982c1c1a568dcc8f0cb829e86a` |
| `model-usage-evidence/specs/model-usage-evidence/spec.md` | `283bc29c4960a5104e720690639051cfb9253cf4f5d2e34f533938a0fc00e5d5` |

### 6.20 OpenSpec 全量三审、联合契约修复与 trace change

OpenSpec 首轮全量三审固定在 `0018a83a056e3b170ea67176c8f9c09549a6dddb0442b21d1f9409544f7ecfbb`。三个 fresh reviewer 均完整读取 23 个主规格、20 个归档 change、五个 active change 和上游真相源；均未读取本报告或其他 reviewer 结论：

| Reviewer | 主规格/归档 | Active 单项与联合 | 主要结论 |
|---|---|---|---|
| A | 23/23 有来源；20/20 metadata；262/262 tasks 完成 | 联合 Stage 1/2 FAIL | 发现 usage event 缺稳定调用关联、nullable token 聚合不可表达、approval trace 与 Product 冲突、SSE cursor 未验证属于当前 run。 |
| B | 23/23 有来源；20/20 metadata/tasks 完整 | 联合 Stage 1 FAIL、Stage 2 PASS | 发现 `model-usage-evidence` 把后续相关 change 错写成“独立”。 |
| C | 23/23 有来源；20/20 metadata/tasks 完整 | 单项与联合 Stage 1/2 PASS | 未识别上述五项；该遗漏不能推翻 API/Product/active delta 原文冲突。 |

主 Agent 逐条裁决：

- **采纳 MEDIUM：usage 调用关联未定义。** started/terminal 是两个全局唯一 event_id，不能共享所谓 evidence id；public ModelUsageEvidence 又禁止额外字段。现改为 event/telemetry metadata 专用 `usage_call_id`，在 provider 副作用前生成，started/terminal 共享但各自 event_id 保持唯一。
- **采纳 MEDIUM：nullable token 聚合不闭合。** ModelUsageEvidence token 允许 null，而 DelegationSummary 原写“所有 child token 合计”的必填整数。现定义为已知值之和；任一未知值必须 `budget_status=incomplete`，预算门禁不得把未知当 0。
- **采纳 MEDIUM：approval trace 与 Product 冲突。** Product FLOW-003 要求 approval 关联非空 trace，当前 API/DTO/存储却允许 null。该修复跨 runtime、worker、migration、approval 与 event，不塞入 usage change；通过 `openspec-propose` 新建 `run-trace-correlation`，在 Phase 13.6 与 13.7 之间增加 13.6A，定义副作用前生成、跨进程传播和历史确定性 backfill。当前能力仍明确为 optional，change 未实施。
- **采纳 MEDIUM：SSE cursor 归属遗漏。** RUN-006 delta/tasks 现覆盖超过 max seq、run 内空洞和只属于其他 run 的 seq，统一在握手前返回不泄漏其他 run 状态的 422。
- **采纳 MEDIUM：错误独立性措辞。** `model-usage-evidence` 已改为“后续相关 change”，并显式受 `13.5 -> 13.6 -> 13.6A -> 13.7 -> 13.8 -> 13.9` DAG 与联合审查约束。

23 个主规格的完整来源证明：

| 主规格 | 归档 delta 来源 |
|---|---|
| `agent-registry-model-context` | `2026-07-08-agent-registry-model-context`；`2026-07-10-p0-example-agent-flows` |
| `agent-scaffold-cli` | `2026-07-10-agent-scaffold-cli` |
| `auth-policy-hitl-approvals` | `2026-07-08-auth-policy-hitl-approvals`；`2026-07-10-p0-example-agent-flows`；`2026-07-12-split-api-worker-runtime` |
| `canonical-events-artifacts` | `2026-07-06-canonical-events-artifacts` |
| `core-contracts`、`identity-context`、`typed-config`、`vendor-boundary-doctor` | `2026-07-06-core-config-identity-contracts` |
| `durable-run-queue` | `2026-07-12-durable-run-queue` |
| `eval-dataset-splits` | `2026-07-11-eval-dataset-split-foundation` |
| `eval-experiment-api-acceptance` | `2026-07-11-eval-experiment-api-acceptance` |
| `eval-gate-trace-loop` | `2026-07-10-eval-gate-trace-loop` |
| `eval-harness-experiments` | `2026-07-11-eval-harness-experiment-comparison` |
| `observability-provider-adapters` | `2026-07-10-observability-provider-adapters` |
| `p0-example-agents` | `2026-07-10-p0-example-agent-flows` |
| `quality-compliance-entrypoints`、`workspace-packaging` | `2026-07-06-bootstrap-workspace-packaging` |
| `retrieval-rag` | `2026-07-10-retrieval-rag-foundation` |
| `runtime-checkpoint-runs` | `2026-07-06-runtime-checkpoint-runs`；`2026-07-10-p0-example-agent-flows`；`2026-07-12-split-api-worker-runtime` |
| `service-app-shell` | `2026-07-06-bootstrap-workspace-packaging`；`2026-07-10-service-app-template-surface`；`2026-07-12-service-profile-deployment-proof`；`2026-07-12-split-api-worker-runtime` |
| `service-deployment-boundaries` | `2026-07-12-service-profile-deployment-proof` |
| `storage-migration-uow` | `2026-07-06-storage-migration-uow` |
| `tool-execution-boundaries` | `2026-07-08-tool-execution-boundaries`；`2026-07-10-p0-example-agent-flows` |

归档证明：20 个 change 均有 `schema: agent-pack-product-change` 与 `created` metadata，合计 262/262 tasks 已勾选；`phase-12-5-change-matrix.md` 和 `phase-13-change-matrix.md` 是矩阵文档，不计入 change 数。未发现归档 TBD、伪造来源、无主规格落点的 delta 或废弃行为重新进入长期规格。

修复后状态与验证：

- 六个 active change 均为 `in-progress`、0 个任务完成：10 + 12 + 15 + 14 + 11 + 11，共 73 个未实施 task；Phase 14/15 未开始。
- `run-trace-correlation` proposal/specs/design/tasks 4/4 artifact 完成；只达到 apply-ready，未实施、未归档。
- `uv run openspec validate run-trace-correlation --type change --strict`、`model-usage-evidence`、`agent-delegation-execution`、`sse-event-streaming` 单项严格校验均 PASS。
- `uv run openspec validate --all --strict`：`29 passed, 0 failed`，仅证明可解析。
- `git diff --check`：PASS。
- 修复后 `DEV-PLAN.md` SHA-256：`450395bbd99f5724ce13ce351b3217f5239c4a827be6517acd763cf5370798ef`。
- 修复后 `API-Contract.md` SHA-256：`fa7040277422e44a7c0338821dbeb6d4049713757ff953fd0d47086a3d7c3231`。
- 修复后排除本报告的工作区快照：`6b06f2bde7016722db1077cc2660fade8c0a921f06becfc310a3e195fe6a86cc`。

上述 API/DEV/OpenSpec tracked diff 使 6.19 的第 4 点 PASS 和本轮 OpenSpec 局部 PASS 全部失效；必须由三个新的 fresh reviewer 对修复后的文档与六 change 完整范围重新审查。

### 6.21 OpenSpec 修复后复审、迁移边界与质量门禁修复

复审快照为 `6b06f2bde7016722db1077cc2660fade8c0a921f06becfc310a3e195fe6a86cc`：

| Reviewer | 单项/联合 Stage 1 | Stage 2 | 主要结论 |
|---|---|---|---|
| A | 六项与联合 PASS | PASS WITH NOTE | 契约无发现；报告当前 `make quality` Pyright 失败，但把它归为 Phase 15/AC-051 当前态。 |
| B | 六项与联合 PASS | PASS | 52 个定向合同测试与 strict validation 通过，0 个发现。 |
| C | trace/usage 与联合 FAIL | FAIL | 历史同 run 冲突非空 trace 无迁移语义；estimated price source 无合法落点；`make quality` Pyright 46 errors。 |

主 Agent 复核并裁决：

- **采纳 MEDIUM：冲突非空 trace。** “已有非空不覆盖”与“同 run 全部一致”在历史存在两个非空值时无解。run-trace change 现要求 SQLite/PostgreSQL migration 在单事务内检测冲突，输出脱敏 run/record 标识并整批 fail closed，不选择、不覆盖、不部分提交。
- **采纳 MEDIUM：estimated price provenance。** API 5.29 的既有 `decision` object 现明确承载 `price_source_ref` 与 `price_source_version`；只在 `cost_status=estimated` 时要求，不新增顶层 DTO 字段、不内联完整价目。
- **采纳 MEDIUM：Pyright 搜索路径。** 主 Agent 复现 `smoke_service.py` 同目录 `service_smoke_support` 无法解析并产生 46 个派生 unknown-type error。即使 AC-051/Phase 15 未完成，Git 提交门禁仍要求当前质量通过；根 `pyproject.toml` 已把 `templates/service-app/scripts` 加入 pyright `extraPaths`。

修复后当场验证：

- `make quality`：Ruff format `273 files already formatted`；Ruff PASS；Pyright `0 errors, 0 warnings, 0 informations`；import-boundary `ok`。
- `uv run openspec validate run-trace-correlation --type change --strict`：PASS。
- `uv run openspec validate model-usage-evidence --type change --strict`：PASS。
- `uv run openspec validate --all --strict`：`29 passed, 0 failed`，仅作可解析证据。
- `git diff --check`：PASS。
- 修复后排除本报告的工作区快照：`ff5b26f6d4b4847c0305093b12638abbdbb7627039dda09b265613bc2bed7066`。

以上 tracked diff 再次使前一轮 PASS 失效；提交开发前基线前必须再由三个新的 fresh reviewer 对相同完整范围审查。

### 6.22 提交前门禁的接力所有权与技术图修复

提交前门禁快照为 `ff5b26f6d4b4847c0305093b12638abbdbb7627039dda09b265613bc2bed7066`。Reviewer A/C 对六 change 单项与联合均判 Stage 1/2 PASS；Reviewer B 判六项单项 PASS、联合 Stage 1 FAIL、Stage 2 PASS，并给出两项 MEDIUM。主 Agent 按原始证据裁决：

- **采纳 MEDIUM：`app/main.py` 接力漏记 13.5。** `run-openapi-contract-accuracy/design.md` 明确 Phase 13.5 需要唯一 OpenAPI factory，而 DEV-PLAN 原从 13.6 开始列 owner。现改为 `13.5 -> 13.6 -> 13.6A -> 13.9`，后序累计保留前序合同。
- **采纳 MEDIUM：技术架构图遗漏两个 P0 gap。** DEV-PLAN 已记录 Run OpenAPI accuracy 与 canonical run trace，技术图状态框却只列 secret/usage/delegation/SSE。已同步 `.drawio`、`.excalidraw` 与 PNG，加入两项并保持 Phase 14/15 未完成。

drawio skill 自检与验证：

- `validate.py agent-harness-technical-architecture.drawio`：`0 error(s), 0 warning(s)`。
- Excalidraw JSON 解析：PASS。
- draw.io CLI 30.3.6 重新导出 2000px PNG；主 Agent 视觉核验状态框无裁切、遮挡或出界。
- `make quality`：Ruff/Pyright/import-boundary 全 PASS。
- `uv run openspec validate --all --strict`：`29 passed, 0 failed`，仅作解析证据。
- `git diff --check`：PASS。

修复后 hash：

| 文件 | SHA-256 |
|---|---|
| `DEV-PLAN.md` | `6910095585195a2dc358ecbea7df99fa2726a3644c82326fee73b0a94e71d5fb` |
| `agent-harness-technical-architecture.drawio` | `00d0658e0cd0ba014f41b11e3605444d25e151f0c534fb2e73db42e6b97eec27` |
| `agent-harness-technical-architecture.excalidraw` | `2d17041ad67767d1b6822604a15824d62585011bb3efa4c5957aea6d6787b380` |
| `agent-harness-technical-architecture.png` | `06725df91de4089c2a14f02bc776a7d8ec21133776df06a102636d980686f38a` |

修复后排除本报告的工作区快照为 `536034fc25b464dc8e7f35abc31f79f98438d588be4ace6071e43f9c0888661c`。受审图与 DEV tracked diff 已变化，前一轮 PASS 再次失效；需要新的三审确认后才能提交。

### 6.23 OpenSpec 提交前快照缺陷修复与最终复审启动

在 `536034fc25b464dc8e7f35abc31f79f98438d588be4ace6071e43f9c0888661c` 快照的 2+1 审查中，前两名 fresh reviewer 均直接读取原始文档、六个 active change、图稿和验证证据，未读取本报告或彼此结论。主 Agent 对发现逐条复核，不按票数裁决：

- **采纳 MEDIUM：trace idempotent replay 语义冲突。** `run-trace-correlation` 原 delta 把同 key 下“caller trace 缺失或不同”都写成复用首次 trace，与 API Contract 的目标 `409 trace.idempotency_conflict` 冲突。现由 `design.md:23`、delta `runtime-checkpoint-runs/spec.md:6-12` 与 `tasks.md:3,17` 统一为：缺失或相同 trace 安全重放首次 run/trace；不同 trace 返回 409，且不改写 context、不产生 run/event/queue/approval/provider 副作用。
- **采纳 MEDIUM：技术图缺少六项关键约束。** 技术架构三格式现同时记录 `usage_call_id` 仅进入 event/telemetry metadata、estimated price provenance 位于 `decision`、trace 冲突/孤立 migration 整批 fail closed、Approval trace 当前 nullable 到目标 required、nullable token 只累加已知值并标记 `budget_status=incomplete`、SSE cursor 必须属于当前已授权 run。
- **采纳维护性 LOW：部署图与运行/信任图存在交叉线。** `e-svc-api-queue` 与 `e-migration-worker`、`e-jsonl-events` 与 `e-hitl-checkpoint` 已重新布线；没有改变数据流方向或边界所有权。

drawio skill 结构和视觉核验：

- technical、deployment、runtime/trust 三张 `.drawio` 的 `validate.py` 均为 `0 error(s), 0 warning(s)`。
- 三张 `.excalidraw` 均通过 JSON 解析；对应约束文本与 `.drawio` 一致。
- draw.io CLI 30.3.6 以 `--width 2000` 重新导出 PNG，尺寸分别为 2000×1409、2000×1329、2000×1387；主 Agent 逐张视觉核验，无裁切、遮挡、节点穿线或语义反转。
- runtime/trust 图进一步明确：`Last-Event-ID` 是唯一 cursor；非零值若非法、属于其他 run、落在 seq 空洞或超过 max seq，均在握手前返回 422。

修复后提交前验证：

- 六个 active change 单项 `--type change --strict`：全部 PASS。
- `uv run openspec validate --all --strict`：`29 passed, 0 failed`，仅作为可解析证据。
- `make quality`：Ruff format/check PASS；Pyright `0 errors, 0 warnings, 0 informations`；import-boundary `ok`。
- `git diff --check`：PASS。

最终复审候选快照与关键文件 hash：

| 文件 | SHA-256 |
|---|---|
| 排除本报告的工作区快照 | `dcf5d10fc082449c6a0a7fb1e6a580b5d8e8c93ee3aac8a22ca80bad32565616` |
| `DEV-PLAN.md` | `6910095585195a2dc358ecbea7df99fa2726a3644c82326fee73b0a94e71d5fb` |
| `API-Contract.md` | `b1b4aeff80433476d64e38e38f428245d409859128fb475e7c390132863fcb8c` |
| technical `.drawio` / `.excalidraw` / PNG | `3bced5b4e42c4aa3e9b517e23ee93a9cf855db00b54aa5ad286cbc5f48c0d85b` / `031fa1fcc469c00a3c523071e970808d86318bc99f76f09462041147b4495ee2` / `6e3e84b59e5bb3b65c7e1e736da9fa379e8b2f01fc130686c9e3c4648673ac9e` |
| deployment `.drawio` / `.excalidraw` / PNG | `bfed2c5a61ca8dee9ac72673c3992081ef2d7f31eb5492df06e74970790d7f6d` / `c6920f3baff7b344af757808891494d824b156e3da54694de11f02a24cb97c58` / `cfeb586d8a747f279e9aeadfacf1c8c03d533f5772c58e607fbaab742ff6f4f0` |
| runtime/trust `.drawio` / `.excalidraw` / PNG | `d956e6a8ab2f766fd4fa76479017b5b18af9328fa9f3f9007f99b07141a96cda` / `8d83a0e5da19517430ba5fa9e6df8474c6235dcdb97f27bacb664e0658a8a61e` / `45ab516f8b7629db651acf23c148e53b12a02d271a94a6ef46328d5f120dd407` |

受审 tracked diff 再次变化，前一轮局部 PASS 已失效。现按平台并发限制启动 2+1 的三个全新 reviewer；只有三份报告均完成且主 Agent 证据复核闭环后，才允许提交开发前基线。

### 6.24 第一轮提交前复审发现与第二轮快照

第一轮固定快照为 `dcf5d10fc082449c6a0a7fb1e6a580b5d8e8c93ee3aac8a22ca80bad32565616`。两个 fresh reviewer 均在相同开始/结束 hash 下完成全部范围，未读取本报告或彼此结论：

| Reviewer | Stage 1 | Stage 2 | 主 Agent 证据裁决 |
|---|---|---|---|
| A | PASS | PASS WITH LOW | 采纳维护性 LOW：`Product-Spec.md:1459` 仍记录两张图各 1 个 warning，但四图 validator 已全部 0 warning。 |
| B | FAIL | FAIL | 采纳三项 MEDIUM：`usage_call_id` event/telemetry 路径不唯一；技术图 Excalidraw 状态框高度 90 小于文字高度 194；Product validator 证据漂移。 |

主 Agent 没有按一票 PASS、一票 FAIL 裁决。原始 schema、JSON 尺寸和 validator 输出直接证明三项发现成立：

- 当前 `CanonicalEvent` 只有自由形状 `payload`，没有 envelope metadata 字段；原 change 同时允许 envelope/payload metadata，确实会让实现可选择多个不兼容路径。
- Excalidraw `ex-status-note` 原高度为 90，`ex-status-note-label` 为 194，编辑源必然溢出；PNG 来自 drawio，可读不等于 Excalidraw 源可维护。
- 四张 `.drawio` 的 `validate.py` 当前均输出 `0 error(s), 0 warning(s)`，Product 旧数值无法复现。

已完成修复：

- `usage_call_id` 的唯一 CanonicalEvent 路径固定为 `payload.correlation.usage_call_id`，类型为非空 string；TelemetryFacade 必须保留为 `TelemetryRecord.payload.correlation.usage_call_id`；禁止新增 envelope 顶层字段、替代 payload 路径或写入 `ModelUsageEvidence`。API Contract 5.9、change design、delta spec 和 tasks 已同步，task 明确要求运行时 OpenAPI/序列化合同。
- technical Excalidraw 恢复 contract note 为 980×90，并把 status note 调整为 500×210、label 为 480×194；JSON 解析与尺寸脚本通过。drawio 状态框同步唯一字段路径，重新导出 2000px PNG 并经主 Agent 视觉核验无裁切。
- Product 12.3 改为四图全部 `0 error(s), 0 warning(s)`，并把 validator 数值与 PNG 人工视觉结论分开记录。

修复后验证：`make quality` 全 PASS；六 change 单项 strict 全 PASS；全量 strict 为 `29 passed, 0 failed`；四张 drawio 均 `0 error(s), 0 warning(s)`；四张 Excalidraw JSON 均可解析；`git diff --check` PASS。

第二轮 fresh 2+1 的固定快照为：

```text
3a0087781f420d0520d92518de0e589413764c9e722794e647668aeed1954888
```

第一轮全部 PASS/FAIL 结论因修复后的 tracked diff 失效；只有第二轮三个 fresh reviewer 完成相同范围并经主 Agent 逐条复核后，才允许提交。

### 6.25 第二轮提交前复审：A2/B2 与主 Agent 裁决

第二轮固定快照为 `3a0087781f420d0520d92518de0e589413764c9e722794e647668aeed1954888`。平台协作树因 19 条历史 completed reviewer 记录达到 thread 上限，无法再创建新的内置节点；已完成记录没有删除/归档接口且不再运行。为保证 fresh 隔离不降级，本轮采用 1 个内置 fresh reviewer 加 2 个 `codex exec --ephemeral` 独立临时进程，仍按 2+1 顺序执行。每个实例都读取同一原始范围，禁止读取本报告、其他 reviewer 报告或主 Agent 摘要。

已完成的两份结论：

| Reviewer | 单项 Stage 1/2 | 联合 Stage 1/2 | 结论与主 Agent 复核 |
|---|---|---|---|
| A2 | 六项全部 PASS | PASS / PASS | 0 个发现；开始/结束 hash 一致。验证 22 REQ、68 AC、51/17、73 个 active tasks、四图 0 warning、39 个定向合同测试和全部关键边界。 |
| B2 | 六项全部 PASS | FAIL / PASS | 报告两个 MEDIUM；主 Agent 依据原文统计与 CHANGELOG 语义逐项驳回，不按票数裁决。开始/结束 hash 一致。 |

B2 发现与裁决：

1. **驳回“只有 66 个 AC、49 完成”。** B2 使用只匹配 `AC-[0-9]{3}` 的统计，漏掉 Product Spec 中真实的 `AC-045A` 与 `AC-045B`。按 checklist 原文和允许字母后缀的稳定标识 `AC-[0-9]{3}[A-Z]?` 统计，结果为 68 个唯一 AC、51 完成、17 未完成；算术和 DEV-PLAN 未完成清单一致。该发现是统计正则缺陷，不是产品账本漂移。
2. **驳回“CHANGELOG 声称当前仍有两个 warning”。** `Product-Spec-CHANGELOG.md` 的条目位于 v1.6 历史变更列表，原文“修正架构 validator 的两个受控 crossing warning 记录”描述本版本做过的修订动作，没有说 warning 仍存在。当前状态由 `Product-Spec.md` 12.3 明确为四图 `0 error(s), 0 warning(s)`，并与本轮 validator 输出一致；历史动作与当前结果不冲突。为避免等价改写增加审阅成本，不修改 CHANGELOG 历史。

B2 额外运行证据有效：`make test` 为 `325 passed, 13 skipped`；`make build` 成功生成 wheel/sdist；quality、六 change strict、29 项全量 strict、图稿结构/尺寸、定向合同和安全扫描均通过。两个发现被驳回不影响这些原始验证证据。

第三名 C2 以新的 ephemeral 进程完成相同完整范围：六个 change 单项 Stage 1/2 PASS，唯一联合 Stage 1/2 PASS，Spec/Standards 双轴 PASS，0 个 HIGH/MEDIUM/LOW；开始/结束 hash 均为 `3a0087781f420d0520d92518de0e589413764c9e722794e647668aeed1954888`。C2 独立使用含字母后缀的 AC 统计得到 22 REQ、68 AC、51 完成、17 未完成，并确认 CHANGELOG 是历史动作记录、当前 Product 12.3 与四图 validator 均为 0 warning。

本轮最终不是按票数裁决：A2 与 C2 的 PASS 由原始文件、OpenAPI、测试和 validator 输出支持；B2 的两个发现因统计正则漏项和误读 CHANGELOG 文体被证据驳回。三个 fresh reviewer 都完成相同完整范围，审查期间受审快照未变化，因此第二轮门禁有效。

### 6.26 开发前可信基线最终 hash 与提交边界

开发前可信基线固定为排除本报告的工作区快照：

```text
3a0087781f420d0520d92518de0e589413764c9e722794e647668aeed1954888
```

上游文档与架构图最终 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `Product-Spec.md` | `e7b4b5f15982c6c484cfdab473aa7f63f2577ab543876386e2003a7038b9c38a` |
| `Product-Spec-CHANGELOG.md` | `b8cf76e22bba8184b11626c905abd2a78cc648868e6d1c27fddf09f5d4469ab5` |
| `DEV-PLAN.md` | `6910095585195a2dc358ecbea7df99fa2726a3644c82326fee73b0a94e71d5fb` |
| `API-Contract.md` | `0f04c999c8c34f778d6119690d9db60ac0433d45b52966593ef482c781a21b58` |
| `docs/architecture/README.md` | `f152992232815e00479436ff5dc0173e9c4c557b64424968069d75fb278e3b61` |
| deployment `.drawio` / `.excalidraw` / PNG | `bfed2c5a61ca8dee9ac72673c3992081ef2d7f31eb5492df06e74970790d7f6d` / `c6920f3baff7b344af757808891494d824b156e3da54694de11f02a24cb97c58` / `cfeb586d8a747f279e9aeadfacf1c8c03d533f5772c58e607fbaab742ff6f4f0` |
| runtime/trust `.drawio` / `.excalidraw` / PNG | `d956e6a8ab2f766fd4fa76479017b5b18af9328fa9f3f9007f99b07141a96cda` / `8d83a0e5da19517430ba5fa9e6df8474c6235dcdb97f27bacb664e0658a8a61e` / `45ab516f8b7629db651acf23c148e53b12a02d271a94a6ef46328d5f120dd407` |
| technical `.drawio` / `.excalidraw` / PNG | `48620d859a15efce7870ea7c53c1d80d09e514611c153b936c169bc77eaf881b` / `bc349274188de38e480d1c07c731e0b8483bea450af75bf7fe99faf97cb81471` / `3b6cbc7124ca3609af562b52596dc2afa38e23b76a5f3147dff78a6a66c3131e` |
| overview `.drawio` / `.excalidraw` / PNG | `d80b7e086d3b6a08f94aea3b7147efce0d1d354f0f686369560dddccdf1a9482` / `6128a9d4ba7355a2b02568d535cad788ea2f182e39c1d6199ac7db52a1f75d0f` / `1d53b9534cee91c12eca36fd215ad3b99e56044d4fc120e2855153e7b86abaf4` |

六个 apply-ready change 的可复现 tree hash（对目录内文件路径排序后逐文件 SHA-256，再对清单取 SHA-256）：

| Change | Tree SHA-256 |
|---|---|
| `run-openapi-contract-accuracy` | `137e1a2fe25fe389d78c07468991405d28ecd0dbd490ec226955c97fc54022e8` |
| `config-secret-file-loading` | `308cd9c09d9185658cbd5864d2525012d60f480dae401a365e15156d14e34d37` |
| `run-trace-correlation` | `501e84ca0701051b51753a9f6e87afa690276d8bc6fb5675b056cd50face8fd8` |
| `model-usage-evidence` | `fe25b28ce4ec3efe505ac49be2f65e3279791579ffc00854ac58dd1e5990d064` |
| `agent-delegation-execution` | `47ad3661290ccfad3e3d457c6d5e5926b3bc7c04dd4eddae7c94c96ee6e35eda` |
| `sse-event-streaming` | `d59e1f2953144167c7472d54042ee8d786b051908b6d46e379e68eec311caef8` |

提交边界：本提交只冻结已审查文档、图稿、长期规格来源修复、维护性代码/测试说明、Pyright 搜索路径与六个 0-task apply-ready change；不实施任何 change，不归档、不 push、不 tag，不把 Phase 14/15 标记完成。提交后开发必须从 `run-openapi-contract-accuracy` 开始，每个 change 独立走 TDD、自测、3 fresh review、修复、提交。

## 7. 覆盖矩阵

本节先固定文档层 `REQ/AC -> Phase -> endpoint/schema/error/security -> 当前验收证据/缺口`。第 6 点 capability 审查继续在同一矩阵上补充 OpenSpec、生产符号和 unit/contract/integration/eval/smoke 的逐项证据；当前不得把“已规划”当作“已实现”。

| REQ / AC 状态 | 覆盖 Phase | endpoint / schema | error / security | 当前证据与真实缺口 |
|---|---|---|---|---|
| REQ-001；AC-001、AC-002、AC-003 已完成 | Phase 1 | 无 HTTP；workspace、package metadata、wheel/sdist | 构建失败必须非零退出；模板 wheel 安装不得依赖源码路径 | workspace/packaging contracts、`uv sync`、`make build`；最终轮重跑 build。 |
| REQ-002；AC-004、AC-005 已完成 | Phase 2、6、10、12 | provider-neutral protocol/DTO，无业务 HTTP | import boundary 禁止业务 agent 直接依赖 vendor SDK | import boundary、fake adapter 与 provider contracts；第 6 点复核所有生产 import。 |
| REQ-003；AC-006、AC-007 已完成 | Phase 1、12 | service-app、RUN-001、HLT-001、CLI 等价入口 | API/worker 分进程；health/error 不回显 credential | `make smoke-local` 与真实 PostgreSQL/Redis `make smoke-service` 必须分别重跑。 |
| REQ-004；AC-009 已完成，AC-008、AC-063 未完成 | Phase 2、12、13.6 | typed settings、CFG-001、HLT-001 | schema/startup 错误结构化且脱敏；secret file 受信根、普通文件、大小与冲突门禁 | typed config contracts 只证明现有 loader；application startup failure 与 `_FILE` 由 `config-secret-file-loading` 待实现。 |
| REQ-005；AC-010、AC-011、AC-012 已完成 | Phase 3、12 | repository/UoW/migration contracts，无新增公开 endpoint | tenant 边界、事务原子性、migration downgrade 拒绝条件 | SQLite/PostgreSQL repository 与 migration contracts；真实 service persistence 由 service smoke 复核。 |
| REQ-006；AC-013、AC-014 已完成；canonical trace 关联缺口待补 | Phase 5、12、13、13.6A | RUN-001、RUN-002、RUN-004、RUN-005；RunCreateResponse/checkpoint/resume DTO | 409 invalid transition、503 queue unavailable；resume/approval 私有 lease 不进入公开 DTO；trace 缺失/冲突 fail closed | checkpoint/restart/idempotency、split API/worker contracts；`run-trace-correlation` 0/15，最终 smoke 分 local/service。 |
| REQ-007；AC-015、AC-016 未完成 | Phase 6 summary seam、Phase 13.8 真实执行 | DLG-001、DelegationSummary、目标 RunDetailResponse | edge+policy+cycle/depth/budget、tenant、idempotency conflict、零副作用 deny | 当前只有 registry edge check 和调用方 summary；`agent-delegation-execution` 0/11，未实施。 |
| REQ-008；AC-018 已完成，AC-017 未完成 | Phase 5、7、11、12、13.5、13.8、13.9 | AGT、RUN、APR、POL、EVL、HLT 全部 P0 endpoint；CLI doctor | 每个 operation 的精确 status、ApiErrorEnvelope、认证与可见性 | CLI doctor 与当前 route 存在；RUN-002～005 多余状态由 `run-openapi-contract-accuracy` 待修，RUN-006 由 13.9 待实现。 |
| REQ-009；AC-019、AC-020 已完成 | Phase 2、7 | 所有非 health P0 endpoint 的 identity/tenant context | 401 invalid/missing credential、403 policy、跨 tenant 404/403 不泄漏存在性 | auth/tenant/API contracts；第 6 点复核所有 route dependency 与 storage query。 |
| REQ-010；AC-021、AC-022、AC-023、AC-024 已完成；ApprovalRecord trace 缺口待补 | Phase 2、4、7、12、13.6A | APR-001/001A/002、POL-001、RUN-001 input guardrail、tool module seam | require_approval/deny/audit；approval continuation 单次执行；目标 approval trace 必填且不可被 caller 覆盖 | HITL/policy/guardrail contracts 与 queued approval integration；当前 trace 仍 optional，13.6A 收口。 |
| REQ-011；AC-025、AC-026、AC-027、AC-028 已完成 | Phase 8、12 | ToolInvocation/ArtifactRef/MCP DTO；P0 不暴露远程 tool HTTP route | workspace escape、allowlist、shell approval、输出截断、untrusted 标记 | tool registry/builtin/CLI/MCP/execution contracts；第 6 点复核真实故障路径。 |
| REQ-012；AC-029、AC-030、AC-031、AC-032 已完成，AC-064 未完成 | Phase 2、4、6、13.7 | MOD-001、ModelUsageEvidence；model/embedding adapter seam | budget/policy/fallback 可追踪，usage/error 脱敏，业务 agent 不拼 raw usage | fake model、router/context/embedding cache contracts；统一 durable usage evidence 由 `model-usage-evidence` 0/14 待实现。 |
| REQ-013；AC-033、AC-034、AC-035、AC-036 已完成 | Phase 6、9、12 | retrieval/RAG module DTO，无 P0 retrieval HTTP endpoint | citation 来源与 untrusted 标记；prompt injection 不覆盖系统/策略指令 | local/service retrieval、RRF、citation trust contracts。 |
| REQ-014；AC-037、AC-039、AC-040 已完成，AC-038 未完成 | Phase 4、5、13.6A、13.9 | RUN-003 JSON、RUN-006 SSE；CanonicalEvent | run-scoped canonical trace、terminal 唯一、visibility、seq/Last-Event-ID 当前 run 归属、握手前后错误与脱敏 | JSON after_seq 与 event contracts 已有；trace 传播由 13.6A、SSE transport/Last-Event-ID 由 `sse-event-streaming` 0/11 待实现。 |
| REQ-015；AC-041、AC-042 已完成；run trace 传播缺口待补 | Phase 4、10、13.6A | TelemetryFacade/trace refs；HLT-001 只暴露安全配置摘要 | local-first、provider failure degraded、redaction、无 SaaS 也可运行 | local JSONL 与 provider adapter contracts；13.6A 固定 run trace，13.7 再增加 usage_call_id 与 usage evidence。 |
| REQ-016；AC-043、AC-044、AC-045、AC-045A、AC-045B 已完成 | Phase 10、11、12.5 | EVL-001A/B、002A/B、003A/B/C、004A/B/C/D 与对应 DTO | draft/approve/accept 权限、tenant、409 状态冲突、人工 reason/audit | eval dataset/split/experiment/API contracts 与 `make eval`；最终轮重跑。 |
| REQ-017；AC-046、AC-047 已完成 | Phase 12 | AGT-001、RUN-001、CLI agents list/run/eval | registry 找不到返回稳定错误；示例不得绕过 vendor/import/policy 边界 | 四个示例 registry/scaffold/eval contracts；第 6 点逐 agent 复核。 |
| REQ-018；AC-048 已完成，AC-049 未完成 | Phase 1、12、14 | 文档与 ADR，不新增 endpoint | adapter/release/security 文档不得泄露 secret 或把未来能力写成当前 | README/architecture 基础可定位；深度 adapter/release/security/maintainer 文档归 Phase 14，未完成。 |
| REQ-019；AC-052 已完成，AC-050、AC-051、AC-065、AC-066 未完成 | Phase 1、all phases、13.7、13.9、15 | 跨 endpoint/OpenAPI；性能证据关联 run/trace | quality/test 分离、真实 red evidence、local 总时延 <5s、SSE 首 frame <1s | 本报告建立基线矩阵；`make eval` fake path 已有，CI 分离与两项性能门禁仍未完成。 |
| REQ-020；AC-053、AC-054、AC-055、AC-056 未完成 | Phase 15 | CI/release control plane，无产品 endpoint | publish credential、tag/release 零副作用 dry-run、artifact provenance | GitHub/GitLab 等价 pipeline、release preview 尚未实现；不得因本轮验证通过而标记完成。 |
| REQ-021；AC-057 已完成，AC-058 未完成 | Phase 1、14、15 | LICENSE/NOTICE/license check，无 HTTP | vendoring 来源、兼容 license、修改说明与发布阻断 | Apache-2.0 LICENSE 已存在；NOTICE/第三方追踪与发布门禁仍待 Phase 14/15，最终轮运行 license-check。 |
| REQ-022；AC-059、AC-060、AC-061、AC-062 已完成 | Phase 2、4、5、13、13.8、13.9、14 | API/worker/storage/model/tool/event 当前边界与 DTO/CanonicalEvent | credential/trust boundary、tenant/request/run/trace 关联、业务 agent 禁止 ORM/vendor 直连 | 四组架构图、split runtime contracts、import boundary 与真实 service smoke；13.8/13.9 只补 P0 缺口，不把 Phase 14 标记完成。 |

## 8. 验证账本

| 命令 | 结果 | 证据摘要 |
|---|---|---|
| `openspec validate --all --strict` | PASS | `29 passed, 0 failed`；六个 active change 单项 strict 亦全部 PASS，仅作为可解析证据。 |
| `make quality` | PASS | Ruff format/check PASS；Pyright `0 errors`；import-boundary `ok`。 |
| `make test` | PASS | `325 passed, 13 skipped`；由 B2 在固定快照运行。 |
| `make eval` | 待执行 | - |
| `make smoke-local` | 待执行 | - |
| `make smoke-service` | 待执行 | 真实 PostgreSQL/Redis service smoke，必须与离线测试分开报告。 |
| `make build` | PASS | wheel 与 sdist 成功生成。 |
| `make license-check` | 待执行 | - |
| `uv run pre-commit run --all-files` | 待执行 | - |
| `git diff --check` | PASS | 无输出，退出码 0。 |

## 9. 修复记录、遗留风险与 Phase 14/15 缺口

待审查推进后填写。Phase 14、15 始终保持未完成状态。
