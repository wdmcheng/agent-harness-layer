# Service App 维护入口

本目录承载复制模板后与具体应用相关的架构决策、运行手册和 adapter 说明。仓库级公共合同已经交付；复制模板不会自动复制整套仓库文档，因此本目录只记录应用特有决策，不复制公共合同。

- app-specific 设计与运维说明放在本目录。
- 核心 `agent_harness` 公共 seam 见源仓库 [架构边界](../../../docs/architecture/README.md)、[扩展指南](../../../docs/extension-guide.md)、[adapter 合同](../../../docs/adapter-contracts.md)、[context 与信任边界](../../../docs/context-and-trust-boundary.md)、[安全策略](../../../docs/security-policy.md)、[eval/observability 闭环](../../../docs/eval-observability-loop.md)、[release 边界](../../../docs/release-process.md) 与 [ADR](../../../docs/adr/0001-p0-service-boundaries.md)。
- 复制到独立项目后，把失效的源仓库相对链接替换成固定版本的内部文档或上游仓库 URL，并记录所依赖的 `agent-harness` 版本。
- local CLI、`make dev` 与 run 命令的 fingerprint key、SQLite migration 和隔离状态前置见模板 [`Quick Start`](../README.md#quick-start)；应用运行手册不得省略这些 fail-closed 条件。
- 修改 API 前先更新 `API-Contract.md`，再用 OpenAPI drift tests 验证。
- 四个可运行示例、approved eval 与安全降级说明见 [`examples.md`](examples.md)。
