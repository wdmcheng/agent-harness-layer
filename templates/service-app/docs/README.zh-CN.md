# Service App 维护入口

[English](README.md) | [简体中文](README.zh-CN.md)

本目录承载复制模板后与具体应用相关的架构决策、运行手册和 adapter 说明。仓库级公共合同已经交付；复制模板不会自动复制整套仓库文档，因此本目录只记录应用特有决策，不复制公共合同。

- app-specific 设计与运维说明放在本目录。
- 需要把初始化或功能实现交给 AI / Agent 时，明确要求它先读[中文版 AI / Agent 项目操作指南](ai-agent-guide.zh-CN.md)；英文入口见 [`ai-agent-guide.md`](ai-agent-guide.md)。两者都是按需引用的普通文档，不会自动施加目录级指令。
- 核心 `agent_harness` 公共 seam 见源仓库 [架构边界](../../../docs/architecture/README.zh-CN.md)、[扩展指南](../../../docs/extension-guide.zh-CN.md)、[adapter 合同](../../../docs/adapter-contracts.zh-CN.md)、[context 与信任边界](../../../docs/context-and-trust-boundary.zh-CN.md)、[安全策略](../../../docs/security-policy.zh-CN.md)、[eval/observability 闭环](../../../docs/eval-observability-loop.zh-CN.md)、[release 边界](../../../docs/release-process.zh-CN.md) 与 [ADR](../../../docs/adr/0001-p0-service-boundaries.zh-CN.md)。
- 复制到独立项目后，把失效的源仓库相对链接替换成固定版本的内部文档或上游仓库 URL，并记录所依赖的 `agent-harness` 版本。
- local CLI、`make dev` 与 run 命令的 fingerprint key、SQLite migration 和隔离状态前置见模板[首次使用](../README.zh-CN.md#首次使用local-profile)；应用运行手册不得省略这些 fail-closed 条件。
- 修改 API 前先更新 `API-Contract.md`，再用 OpenAPI drift tests 验证。
- 四个可运行示例、approved eval 与安全降级说明见 [`examples.zh-CN.md`](examples.zh-CN.md)。
