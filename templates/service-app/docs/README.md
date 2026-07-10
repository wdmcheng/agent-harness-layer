# Service App 维护入口

本目录承载复制模板后与具体应用相关的架构决策、运行手册和 adapter 说明。当前 change 只固定维护入口与边界，不在这里提前伪造尚未完成的深度文档。

- app-specific 设计与运维说明放在本目录。
- 核心 `agent_harness` 公共 seam 的契约仍以仓库 API、Product Spec 与 OpenSpec 主规格为准。
- adapter、security、release process 与 ADR 的完整 `REQ-018` 文档体系由后续文档交付收口。
- 修改 API 前先更新 `API-Contract.md`，再用 OpenAPI drift tests 验证。
- 四个可运行示例、approved eval 与安全降级说明见 [`examples.md`](examples.md)。
