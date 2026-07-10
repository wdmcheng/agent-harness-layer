## 1. HTTP 契约先行

- [x] 1.1 先按 `API-Contract.md` 第 3 节补齐 `HLT-001`、单项 approval GET、`EVL-001` 至 `EVL-003` 每个 operation 的完整字段表，明确 health 公开只读语义和 Phase 12/12.5 边界。
- [x] 1.2 再新增运行时 OpenAPI drift contract tests，逐 operation 覆盖 Phase 12 path/method、schema、认证、响应和错误 models，并参数化证明所有适用 operation 的 422 `ApiErrorEnvelope`；此时测试应因 health 尚未实现而失败。

## 2. Template 目录与公开入口

- [x] 2.1 补齐 `eval-cases/drafts`、`eval-cases/approved`、`tests`、`docs` 和独立 `scripts` 的可提交模板内容，并用目录 contract test 证明完整布局及 local/service profile 文件。
- [x] 2.2 实现公开只读 `/api/v1/health` 专用 DTO/route，测试 profile 与 storage/queue/observability 摘要、`request_id` 和敏感配置不泄漏。
- [x] 2.3 在唯一 `create_app` factory 注册 health 与现有 P0 routers，使 Swagger、Redoc、OpenAPI/drift tests 通过，并保持未定义 `/api/v1/tools` route 关闭。
- [x] 2.4 实现模板 Typer CLI 的 app-specific `serve` 入口及 pyproject script，测试 help、参数传递和不复制核心 CLI 业务逻辑。

## 3. 开发与维护体验

- [x] 3.1 补齐不依赖 `cd ../..` 的模板 Makefile、可信 source bootstrap 和模板自身 service smoke；把模板复制到 workspace 外，只安装当前已可构建 wheel 并清除源码路径/PYTHONPATH，验证缺少 `.env` 时给出复制提示、local FastAPI health/OpenAPI/serve，以及 PostgreSQL/Redis 当前 latest migration、repository/UoW、worker和 queue reachability。basic/fake 的最终 AC-006 显式 executor terminal evidence由后续 P0 change完成。
- [x] 3.2 新增模板静态边界测试，证明 app/agents/eval runner 不直连 vendor SDK 或 ORM session，且核心包不依赖模板。
- [x] 3.3 更新模板 README 的 app developer/scaffold maintainer 入口和目录边界，明确完整 `REQ-018` 深度文档仍由 Phase 14 收口；补充模板 `docs` 维护入口。
- [x] 3.4 盘点 Product-Spec P0 CLI 列表，证明本 change 只增加 app-specific `serve`；记录既有核心命令覆盖和需要后续窄 change 补齐的真实缺口。
- [x] 3.5 运行 workspace 内与 copy-out wheel-only targeted tests、local/service smoke、全量测试、quality、build、license 和 pre-commit，并记录可复现结果。
