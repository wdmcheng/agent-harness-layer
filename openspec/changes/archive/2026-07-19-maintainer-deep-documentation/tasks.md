## 1. 双受众导航与架构入口

- [x] 1.1 收口根 `README.md` 的 app developer/scaffold maintainer 导航、目录禁止跨界规则、验证命令矩阵和当前/未来能力声明；证据为全部必需深度文档可从 README 到达。
- [x] 1.2 扩充 `docs/architecture/README.md`，说明图稿真相层级、当前部署边界、未来拆分顺序、相关 ADR、验证命令和故障排查；证据为图源/PNG/ADR/合同链接均存在。
- [x] 1.3 同步 `templates/service-app/README.md` 与 `templates/service-app/docs/README.md`，移除深度文档仍待交付的过期引导并链接仓库级维护合同，不把复制模板描述成生产部署完成。

## 2. 扩展与 adapter 合同

- [x] 2.1 新增 `docs/extension-guide.md`，覆盖 agent、tool、model、retrieval、observability、eval 的允许扩展 seam、禁止依赖、验证命令、证据位置和常见故障。
- [x] 2.2 新增 `docs/adapter-contracts.md`，覆盖 DTO/protocol/facade/repository/UoW/provider adapter 合同、错误与降级边界、测试 seam 和 import boundary 证据。

## 3. Context/trust 与安全边界

- [x] 3.1 新增 `docs/context-and-trust-boundary.md`，覆盖 Agent Loop、ContextAssembler、source/trust refs、guardrail/HITL 回边、SSE 当前回传、WebSocket 未来边界和 untrusted input 处理。
- [x] 3.2 新增 `docs/security-policy.md`，覆盖 auth、permission、policy、approval、workspace、secret loading/redaction、event visibility、审计证据和故障处置。

## 4. 证据闭环与发布边界

- [x] 4.1 收口 `docs/eval-observability-loop.md` 的导航、当前能力、公开 seam、可执行命令、证据位置和排障，并保持 provider-neutral 与人工批准边界。
- [x] 4.2 新增 `docs/release-process.md`，只描述当前可执行质量/构建/license 流程和 Phase 15 未来合同，明确不存在自动版本、tag、CHANGELOG、workflow 或 registry publish。

## 5. 架构决策记录

- [x] 5.1 新增 `docs/adr/0002-vendor-adapter-isolation.md`，记录 vendor SDK 隔离边界、替代方案、后果、证据和复审触发条件。
- [x] 5.2 新增 `docs/adr/0003-redis-runtime-license-policy.md`，记录 Redis 8.0.1 当前 pin/用途、license 与 NOTICE 复审、替代方案、证据和升级触发条件。

## 6. 文档与 Phase 验收

- [x] 6.1 建立并执行命令、内部路径/锚点、外部引用和版本一致性核验；按 dependency lock、Compose image reference、未锁定外部 CLI 与实测 runtime 分别裁决，纠正 DEV-PLAN 中 `uv`/PostgreSQL 的已知表述冲突，本地命令与真实 service profile 结果均作为冻结证据。
- [x] 6.2 执行 `make quality`、`make test`、`make eval`、`make smoke-local`、`make smoke-service`、`make build`、`make license-check` 和 change strict validation，记录退出状态与关键产物。
- [x] 6.3 全部交付、命令/链接/版本验证和 strict validation 通过后，更新 Product-Spec `AC-049`/P0 文档完成项与 DEV-PLAN Phase 14 状态，Phase 15 继续标记未开始；随后冻结包含这些状态更新的候选最终 diff 进入 fresh Stage 1/2 review。
