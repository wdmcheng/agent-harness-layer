## Context

前两个 change 提供稳定的 split、experiment、comparison DTO/service 和 Phase 12.5 repository。现有 service-app 通过唯一 FastAPI factory、HTTPBearer identity dependency、`ApiErrorEnvelope` handler 和薄 route 层暴露 eval gate；现有 CLI 组合本地 identity/storage/policy。EVL-004 必须复用这些边界，不能在 route 或 CLI 内复制算法或直接操作 ORM。依赖和共享验收见 `../phase-12-5-change-matrix.md`。

## Goals / Non-Goals

**Goals:**
- 以薄 HTTP/CLI adapter 完整暴露 create/show/comparison/accept。
- 用持久化 request hash 和唯一约束保证 create/accept 在进程重启后仍幂等。
- 把人工 reviewer、policy decision、comparison evidence 和 audit 原子绑定到 acceptance。
- 让 OpenAPI、错误 envelope、tenant isolation 与既有 P0 API 保持一致。

**Non-Goals:**
- 不实现 UI、自动调参、自动改写配置或 release rollout。
- 不新增第二套 app factory/router，不让 route/CLI 绕过 ExperimentService/repository。

## Decisions

1. **Route/CLI 只做边界转换，业务规则留在 `ExperimentService`/`AcceptanceService`。** HTTP 从依赖获取 identity/request id，CLI 构造 local identity；两者调用相同 request DTO。备选在两个入口各实现 split/accept 会产生安全和幂等漂移。
2. **Create 幂等使用 `(tenant_id, idempotency_key)` 唯一约束 + canonical request hash + 私有 execution claim。** 同 hash 返回原记录，不同 hash 映射稳定 409；split、experiment 和首个 claim 在同一 transaction 内提交，避免并发双写或 orphan split。活跃 `running` 重放只读返回；heartbeat 续租失败/异常、过期、中断或 terminal 写入失败等无法证明 evaluator 副作用的状态转 `needs_review`，terminal repository 更新同时原子校验未过期租约，不得为了恢复 completed 而自动重跑。仅用进程内 cache 或只用可 takeover lease 都无法同时满足跨 worker/restart 与副作用幂等。
3. **Accept 使用 experiment 唯一且不可变的 review decision record 与 audit transaction。** 独立 `evals/acceptance.py` service 先验证 comparison 和 candidate version，再执行 `PolicyEngine.evaluate(action="eval.harness.accept")`；accepted 时在同一 UoW 写 decision、production binding 和 audit，rejected 时写 decision/audit 但不产生 binding。同 reviewer 相同 request hash 读取既有结果，其他 reviewer 或冲突 request 返回 409；不回改 `evals/experiments.py`。
4. **认证 identity 是 reviewer 真相源。** body 不接收 `tenant_id`/`reviewer_id`；跨 tenant get 返回 404，policy deny 返回 403，资源状态/幂等冲突返回 409，输入不合法返回 422。所有错误通过现有 error handler 包装 request id。
5. **CLI 增加 `show` 保持 read 等价。** 参数化 CLI 接受 JSON 文件或明确 flags 构建 HarnessDTO，输出 `model_dump_json` 兼容形态；不通过 shell 拼接或读取 provider SDK object。
6. **OpenAPI contract test 读取真实 app schema。** 对四个 operation 检查 security/error/response schema，并用真实 route 调用证明无 side effect；文档字符串不作为实现证据。

## Affected Surfaces

- `templates/service-app/app/api/routes/evals.py`：EVL-004 request/response DTO 与四个薄 endpoints。
- service-app app factory/dependencies：注入 ExperimentService、PolicyEngine、Audit/UnitOfWork，不新增 router 树。
- `packages/agent-harness/src/agent_harness/cli.py`：`eval experiment create/show/compare/accept`。
- `evals/acceptance.py`：独立人工 review、candidate version binding、policy 和原子 decision/audit seam；只消费 `evals/experiments.py` 公共 DTO/service，不回改 experiment 算法。
- policy 配置/默认 provider：声明 `eval.harness.accept` 的 allow/deny/require_approval 行为；accept 只接受 allow，require_approval 返回 409 `eval.experiment.approval_required`，允许保留 policy decision audit但不创建嵌套 approval 或 decision/binding。
- API/CLI/contract tests 与 `docs/eval-observability-loop.md` 操作指南。

## Testing Seams

- HTTP：TestClient 调用真实 app，覆盖 success、401/403/404/409/422、cross-tenant、idempotency、provider degraded 和 side-effect counts。
- OpenAPI：运行时 `/openapi.json` paths/security/error/schema drift。
- CLI：Typer runner 通过真实 service composition 覆盖 create/show/compare/accept 和非零错误。
- Persistence：并发/重启后的 create hash、原子 split+experiment+claim、active replay、过期 claim/中断/terminal 写失败转 `needs_review` 且 evaluator/provider 不重复、accept unique record/audit，SQLite 与 PostgreSQL contract。
- Policy/Audit：recording provider 断言 action/resource/context，deny/require_approval 不写 accepted record。

## Risks / Trade-offs

- [HTTP 与 CLI composition 不一致] → 公共 request/result DTO 和 service 是唯一业务 seam，入口只负责 identity/serialization。
- [并发 accept 重复 audit] → repository 唯一约束与同 transaction get-or-create；冲突映射 409。
- [recommendation 被误当自动批准] → service 仍强制人工 identity/reason/policy/gate，comparison 只读且无 side effect。
- [错误消息泄漏 provider/secret] → 复用 redaction 和统一 error handler，只返回 reason code、字段路径与 refs。
- [重启时无法证明 evaluator 是否完成] → 返回 persisted `needs_review` 供人工核对，不自动重跑；这是显式安全终态，不伪装成 provider degradation 或 completed。

## Migration Plan

本变更不自行拥有 revision，部署前必须依次应用上游 `0009` 三表 schema、`0010` execution claim 列和 `0011` legacy-created 数据升级，并装配 ExperimentService。新 routes/CLI 可与旧基础 eval endpoints 并存；代码回滚时保留 experiment/acceptance/audit 数据，新版本可再次读取。不得通过 downgrade 删除任一非空 Phase 12.5 evidence。

## Open Questions

无；EVL-004 路径、CLI 表面、policy action、幂等与人工 acceptance 判据已经由 API Contract 和本 delta spec 固定。
