## Source Links

- Product-Spec.md: `REQ-018 README 与文档体系`、`REQ-021 开源合规与许可证`、`REQ-022 部署边界与未来微服务拆分基础`、`AC-049` 与 P0 完成定义。
- DEV-PLAN.md: `Phase 14: 深度文档、ADR 与维护者指南`；Phase 15 的 CI/CD 与发布自动化仅作为禁止越界的后续边界。
- Design-Brief.md or design artifact: 不适用；本变更不涉及 UI 或交互设计。
- CONTEXT.md / ADR: `docs/adr/0001-p0-service-boundaries.md` 提供当前部署边界和未来拆分顺序；仓库无 CONTEXT.md。

## Why

当前根 README 仍把多份深度文档标记为预留项，维护者无法从一个可追踪入口找到扩展契约、安全与信任边界、故障排查、发布边界和架构决策。Phase 14 需要把已经实现的运行时能力整理成可执行、可核验且不冒充 Phase 15 自动化的维护文档，收口 `AC-049`。

## What Changes

- 完成根 README、架构索引、扩展指南、adapter contract、context/trust boundary、安全策略、eval-observability loop 和 release process 的互链文档体系。
- 同步模板 README 与模板维护入口，移除“深度文档仍待后续交付”的过期声明，并链接回仓库级合同。
- 新增 vendor adapter isolation 与 Redis runtime license policy 两份 ADR，并与既有 P0 service boundary ADR 建立决策关系。
- 每个能力块记录当前公开 seam、可执行命令、证据位置、service profile 前置条件和常见故障排查。
- 以 `pyproject.toml`、`uv.lock`、生产代码、测试和脚本为当前事实来源，明确区分已实现能力、未来扩展点与 Phase 15 尚未实现的 CI/release automation。
- 建立文档命令、内部链接、外部引用和版本一致性的核验记录；区分仓库锁定依赖、Compose image tag、未锁定外部 CLI 和实测 runtime version。验证证据通过后先同步 Product-Spec 与 DEV-PLAN 状态，再冻结最终 diff 交 fresh reviewer。

## Non-Goals

- 不新增或修改运行时行为、HTTP/API schema、数据库 schema、事件契约或依赖版本。
- 不实施 Phase 15：不新增 GitHub Actions、GitLab CI、release dry-run、CHANGELOG 自动化、版本/tag 计算、registry publish 或部署。
- 不修改 `openspec/specs/`，不 sync 或 archive 本 change，不 commit、push、发布或部署。
- 不把未来 model/tool gateway、event pipeline 或 storage service 描述成当前已部署服务。

## Capabilities

### New Capabilities

- `maintainer-documentation`: 定义 app developer 与 scaffold maintainer 可从 README 找到深度文档，并通过真实命令、证据链接、故障排查和 ADR 理解及维护当前边界的行为契约。

### Modified Capabilities

- 无。现有产品需求与主规格行为不变；本 change 只交付 `REQ-018/AC-049` 已定义但尚未完成的文档能力。

## Impact

- 主要影响 `README.md`、`docs/architecture/README.md`、`docs/eval-observability-loop.md`、新增的五份深度文档与两份 ADR；为消除过期引导还影响 `templates/service-app/README.md`、`templates/service-app/docs/README.md`。验证证据通过后会纠正 `DEV-PLAN.md` 的版本真相说明，并同步 `Product-Spec.md`、`DEV-PLAN.md` 状态字段。
- 不影响代码、API、数据、UI、运行时依赖或发布系统。
- 文档验证会执行现有本地与 service profile 命令；这些命令只生成既有测试/构建临时产物，不改变产品状态。
