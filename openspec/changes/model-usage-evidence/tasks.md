## 1. Usage DTO 与可用性合同

- [ ] 1.1 先增加 `ModelUsageEvidence` red contract tests，逐字段固定 API Contract 5.29 的 `usage_kind`、tenant/provider/model、nullable token、`cost_usd`/`cost_status`、latency、decision、run/agent、optional request、required trace 形状及 extra-field 拒绝；estimated 必须在 `decision` 内含安全 price source ref/version。
- [ ] 1.2 实现 provider-neutral evidence DTO 和公共 import seam，明确 reported/estimated/unavailable `cost_status`、nullable token 与真实零值的区分，禁止自由 dict 或第二套同义 DTO 进入公共 evidence。
- [ ] 1.3 增加 fake model、Pydantic AI adapter 替身和 OpenAI-compatible embedding 替身映射测试与实现，验证三者输出同一 shape 且不暴露 provider SDK object/raw response。

## 2. 路由、预算与调用生命周期

- [ ] 2.1 让 ModelRouter/embedding composition 在 provider 副作用前生成稳定 `usage_call_id`，绑定 `run-trace-correlation` 提供的 canonical trace 并发布 started evidence；完成、受控拒绝和 provider 失败恰好产生一条 terminal usage。
- [ ] 2.2 把实际 provider/model、route/fallback、budget/policy decision 与已知 token/cost/latency 归一化进 evidence，覆盖 fallback、budget/policy required、timeout 和 provider exception。
- [ ] 2.3 增加 import/static boundary tests，证明业务 agent、template agent 和 API route 不解析 raw usage、不导入 provider client，也不创建或修改 `ModelUsageEvidence`。

## 3. Local-First Event 与 Telemetry

- [ ] 3.1 扩展 CanonicalEvent/EventBus/API contracts，逐值验证同一调用的 started/terminal tenant/run/request/agent/trace 与 `payload.correlation.usage_call_id`、单调 seq 和唯一 terminal usage；TelemetryFacade 必须把同一非空 string 保留在 `TelemetryRecord.payload.correlation.usage_call_id`，并用运行时 OpenAPI/序列化合同证明未新增 envelope 顶层字段或其他 payload 路径，payload 其余内容只保留有界摘要或安全 ref。
- [ ] 3.2 让 TelemetryFacade 先写 local durable usage 再 fan-out，增加未配置 provider、provider 成功/失败和 local sink 失败测试，确保外部失败不删除、隐藏、改写或重复结算 local evidence。
- [ ] 3.3 使用 prompt、embedding、vector、headers、secret、raw exception/response fixtures 扫描 DTO、event、trace、error、local/provider payload，补齐双出口 redaction 与封闭失败状态。

## 4. Runtime 组合与性能门禁

- [ ] 4.1 在 service-app runtime/model/embedding composition 注入已认证 tenant/run/request/agent 与 `run-trace-correlation` 提供的 canonical trace context，验证 local JSONL 与 PostgreSQL event sink 都能按关联字段读取 evidence。
- [ ] 4.2 扩展 `scripts/smoke_local.py`，用固定 fake provider 从公开 single-agent run 入口计时到唯一 terminal，断言总时延小于 5 秒并输出不含 secret 的阶段时延与关联标识。
- [ ] 4.3 增加超限负向测试，证明 smoke 非零失败且不会跳过、放宽阈值或用单元测试内部墙钟替代入口验收。

## 5. 验证与收口

- [ ] 5.1 运行 model/embedding/event/observability 定向 contract/integration/eval tests、`make eval`、`make smoke-local` 和真实 PostgreSQL/Redis `make smoke-service`，分别记录 local-first 与 service persistence 证据。
- [ ] 5.2 运行 import/secret scans、OpenAPI drift、`make quality`、`make test`、`make build`、`make license-check`、pre-commit、`git diff --check` 和 `openspec validate model-usage-evidence --type change --strict`，保持 delegation/SSE 与 Phase 14/15 在范围外。
