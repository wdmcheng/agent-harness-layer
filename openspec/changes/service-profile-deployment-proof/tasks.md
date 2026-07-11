## 1. 可复制四服务镜像与 Compose

- [x] 1.1 先以 Compose contract锁定 PostgreSQL、Redis、migration、API、worker服务、health/dependency、共享 DB/queue/DBOS/PostgreSQL-event env与角色 command，再实现单一 Dockerfile、`.dockerignore`和容器内 override。
- [x] 1.2 先以模板复制合同锁定 wheel-only构建、不依赖根源码/PYTHONPATH、唯一 compose/queue namespace、默认 container/network/volume/credential全清与 `SERVICE_APP_KEEP_DATA=1`精确保留，再实现仓库内/外一致的 bootstrap。

## 2. 真实 HTTP-to-Worker Smoke

- [x] 2.1 重构 `smoke_service.py`：先启动 PostgreSQL/Redis、迁移，bootstrap隔离 API-key hash；以缺失/无效 credential验证401零副作用，再以有效 token经 RUN-001提交 `created` run并逐值读取 Redis entry四字段。
- [x] 2.2 通过 smoke-only failpoint让 stable-executor worker A在 workflow/application owner/ref与 PENDING/durable状态建立后 hard-exit；确认 A容器停止，再让 B复用同 executor id、真实 `XAUTOCLAIM`并由 DBOS恢复同 workflow/step。逐值对比 executor/workflow/owner/status，另测并行同-id实例 fail closed。
- [x] 2.3 以 `examples.dev_assistant`真实动作确定性产生 waiting application checkpoint/approval：从 HTTP/PostgreSQL/events读取同 run evidence；验证 approve enqueue失败补投、worker continuation完成，以及独立 deny零 lease/queue/DBOS/handler和 approve/deny仲裁。
- [x] 2.4 输出 migration/auth/API、Redis receipt/reclaim四字段、DBOS owner/ref、execution identity、application checkpoint、approval resolution、PostgreSQL envelope与终态脱敏证据；任一失败进入精确 finally cleanup。

## 3. 部署边界文档与架构产物

- [x] 3.1 同步根/模板 README、API-Contract 和 `docs/adr/0001-p0-service-boundaries.md`，准确区分当前四服务与未来 worker -> tool/model -> observability/event -> storage 拆分路径。
- [x] 3.2 同步部署架构图 `.drawio`、`.excalidraw` 与 PNG 预览并做视觉复核，移除 Phase 13 待实现标记，保留 DTO/CanonicalEvent/source/trust/context/guardrail/audit 关联边界。
- [x] 3.3 回写 DEV-PLAN 的 Phase 13 进度、验证、风险与 ready-to-archive 状态，不提前完成 Phase 14/15 或自动 archive。

## 4. 组合与全量证据

- [x] 4.1 在仓库内和 workspace外运行四服务 smoke、认证零副作用、真实 worker crash/reclaim、Redis四字段、PostgreSQL event sink、Compose/static contracts、真实 PostgreSQL/Redis/DBOS条件测试和局部 OpenAPI drift，证明不是 local-only证据。
- [x] 4.2 运行全量质量、pytest、local/service smoke、eval、build、license、pre-commit、OpenSpec strict 和 diff check，并记录合理 skip、资源清理与最终可审计证据。
