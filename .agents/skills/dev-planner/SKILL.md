---
name: dev-planner
description: 当 Product-Spec.md 已完成、需要规划怎么分阶段开发时使用。也在 Spec 变更后用于更新已有开发计划。输出 DEV-PLAN.md。
---

[任务]
    生成模式：读 Product-Spec.md 和 Design-Brief.md，分析功能依赖，联网搜索验证技术选型；有前端 UI + 后端 API 交互时，先输出字段级 API-Contract.md，再输出分阶段开发计划 DEV-PLAN.md。
    迭代模式：Spec 变更后分析影响范围；变更影响接口、页面数据、异步任务、流式事件或错误状态时，先更新 API-Contract.md，再更新 DEV-PLAN.md 的 Phase 划分和文件清单，已完成的 Phase 不动。

[依赖检测]
    必需：Product-Spec.md。
    可选，缺了标降级：Design-Brief.md、设计工具 MCP、已有项目代码。

[文件结构]
    dev-planner/
    ├── SKILL.md                  # 本文件
    └── templates/
        ├── api-contract-template.md  # API-Contract.md 输出模板
        └── dev-plan-template.md      # DEV-PLAN.md 输出模板

[第一性原则]
    契约先行：有前端 UI 和后端 API 的项目，开发计划前必须先定 API-Contract.md。不能把接口文档放到开发后期从 OpenAPI 摘要补；那时前后端已经开始互相猜字段。
    契约随验：API 契约验证不能全放到收尾 Phase。每个新增或修改 endpoint 的功能点 / Phase 验收都必须做局部 OpenAPI 或等价运行时接口文档漂移检查；最后只做全量复扫和证据汇总。
    页面反推接口：API-Contract.md 必须从 Spec 的流程、Design-Brief 的页面状态、设计稿的首屏数据和交互动作反推，覆盖首屏接口、操作接口、异步任务、错误状态和流式事件。
    字段级契约：接口契约不能只列 endpoint。每个 endpoint 必须写请求方法、路径、认证、请求头、Path 参数、URL 参数、请求体、响应头、响应体、响应码、错误码、幂等性、副作用、前端状态和安全规则。
    可验证：每个 Phase 完成后必须能编译、能运行、能看到效果，不允许"写一堆跑不起来"的 Phase。
    依赖正序：地基先打。基础设施排在业务功能前，被依赖方先做。
    粒度适中：一个 Phase 对应一个可独立验收的功能单元，通常 1-3 个核心交付物。
    文件路径明确：每个 Phase 列出要创建或修改的具体文件路径，不写"实现聊天功能"这种。
    无占位符：不允许 TBD、待补充、"类似 Task N"。每个 Task 描述完整到没有项目上下文的人也能照着开工。
    联网优先：技术选型、关键依赖先联网搜索或查官方文档确认版本、兼容性、breaking changes。

[分析维度清单]
    必须分析：
    - 技术栈：框架加版本、UI 方案、数据库、包管理器、部署目标，联网搜索验证。有多个合理选项给用户 2-3 个方案选
    - API 契约：有前端 UI + 后端 API 时，先生成 API-Contract.md；覆盖页面到接口映射、通用请求响应、错误结构、分页过滤、文件上传、异步任务、流式事件、鉴权、CSRF/幂等/安全规则，并把每个相关 Phase 的局部契约验证写入验收标准
    - Phase 拆分：按依赖关系和复杂度分解为有序 Phase，每个是可独立验收的功能单元
    - 每个 Phase 的交付清单：动词开头，描述用户可感知的功能
    - 每个 Phase 的关键文件：具体路径
    - 功能依赖图：确保 Phase 排序不违反依赖
    尽量分析：数据库表及所属 Phase、每个 Phase 的验收标准、已知风险与限制
    不需要分析，交给 dev-builder：函数签名、CSS 方案、测试用例、分支策略

[分析策略]
    依赖图：列功能点 → 问每个"依赖什么" → 构建 DAG → 拓扑排序得 Phase 顺序，基础设施是根节点。
    优先级：核心功能先，重要功能中间，辅助和收尾最后。
    粒度校准：交付清单超 5 项或涉及 3 个不相关功能就太大，只有 1 项简单交付就太小。
    风险前置：没用过的框架、关键第三方 API、性能敏感点尽量排早。

[命名纪律]
    Phase 编号面向用户时只指 DEV-PLAN 的技术开发阶段。对用户描述产品交付顺序不用 Phase N 泛指业务阶段，改用"用户端阶段、后台阶段"或功能名，免得和 DEV-PLAN 的 Phase 撞出歧义。

[信息充足度判断]
    必须满足才生成：技术栈确定并验证；需要 API 的项目已生成 API-Contract.md 且接口覆盖所有 P0 页面/流程；涉及 endpoint 的 Phase 写明局部契约漂移检查；Phase 拆分完成且每个有交付清单、依赖顺序合理、每个 Phase 有关键文件、Spec 所有核心功能都被覆盖。
    没达成继续分析，不生成半成品。

[工作流程]
    生成模式：加载 Spec、Design-Brief、设计稿 → 联网搜索验证技术栈 → 若产品有前端 UI + 后端 API，按 templates/api-contract-template.md 生成 API-Contract.md → 构建依赖图拆 Phase → 充足度达标 → 按 templates/dev-plan-template.md 生成 DEV-PLAN.md → 自检无占位符、API 契约覆盖页面和流式/异步交互、相关 Phase 含局部契约验证、Spec 功能全覆盖、依赖不冲突。设计稿存在时 API 契约和 Phase 拆分都以设计稿实际页面结构为准。
    迭代模式：读现有 DEV-PLAN、API-Contract、更新后的 Spec、CHANGELOG 定位变更 → 识别接口和 Phase 影响范围 → 向用户说明 → 先更新 API-Contract.md，再在现有 DEV-PLAN 上改，已完成 Phase 不动；涉及 endpoint 的变更必须补局部契约验证标准 → 重新校验依赖 → 变更动到已写代码的 Phase 时，提醒回 dev-builder 同步实现，只提醒不自动改。
    确认策略：技术栈多选、Phase 粒度偏好、功能优先级有歧义时才问用户，其余 Spec 写清了就不追问。

[初始化]
    执行生成或迭代模式的加载阶段。
