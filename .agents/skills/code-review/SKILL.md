---
name: code-review
description: 当用户说要审查代码、检查质量、验证功能是否完整，或需要对照 Spec 和设计稿验证代码实现时使用。输出结构化审查报告，每项结论附证据。
---

[任务]
    对照 Product-Spec.md 和设计稿，审查代码实现的完整度和质量，输出结构化报告。修复由主 Agent 拿报告后用 dev-builder 或 bug-fixer 执行。

[依赖检测]
    必需：Product-Spec.md、项目代码。
    可选增强：DEV-PLAN.md、Design-Brief.md、openspec/changes/<change>/、CONTEXT.md、CONTEXT-MAP.md、docs/adr/、设计工具 MCP、Playwright、git。

[文件结构]
    code-review/
    └── SKILL.md  # 本文件，无 references / templates

[第一性原则]
    不信任声明：不接受"已实现""大致匹配"。每个功能要么有代码并附文件行号，要么没有。
    证据为王：说"通过"必须附编译输出、API 响应或数值对比。没证据的"通过"等于没审查。
    不放过：Spec 每条功能都要被检查到，不允许"其余看起来正常"。
    联网优先：可疑代码模式或安全隐患先联网搜索确认是否已知问题再下结论。

[输出风格]
    像严格的 QA：逐项打勾，不讲情面。每个结论附证据，每个 ✅ 附代码位置和验证方式，每个 ❌ 附 Spec 原文和实际差异。安全问题单独高亮。

[审查维度清单]
    分两阶段。Stage 1 通过才进 Stage 2。Stage 1 有 HIGH 问题就停在 Stage 1，报告标注"Stage 2 未执行"。
    同时分两轴汇报。Spec 轴回答做对了没有，Standards 轴回答做好了没有。两个轴的结论分开列，不合并排序，不用一个轴的通过掩盖另一个轴的问题。

    Stage 1，Spec 轴，做对了没有：
    - 功能完整性：Spec 每条功能逐项对照代码，输出完整实现、部分实现、未实现
    - 变更契约：有 OpenSpec change 时，对照 proposal.md、specs/**/*.md、design.md、tasks.md 检查本次行为是否完整、是否有 scope creep；审查 OpenSpec artifact 时把 `openspec validate` 只当格式和契约可解析证据，不当作 review 结论
    - 计划一致性：有 DEV-PLAN.md 时，对照当前 Phase 交付清单检查是否漏项或越界
    - 引导真实性：UI 占位符 / 提示 / 引导文案必须对应已实现的行为，揪出指向不存在功能的死引导（如 placeholder 写"输入 X 调用"但 X 无任何处理），算未实现
    - UI 一致性：有设计稿则提取设计数值与代码逐项比对，对照 Design-Brief 的色彩、密度、风格

    Stage 2，Standards 轴，做好了没有：
    - 代码质量：命名规范、无 any、单一职责、无重复、错误处理；按有效代码行审查文件规模，JS/TS/Python/Ruby/Go/Rust 超 300 行或 Java/C#/Kotlin/Swift 超 500 行要说明是否应拆分，前者超 500 行或后者超 1000 行默认判拆分问题；注释、空行、docstring/块注释说明行不计硬门槛，必要注释不能被行数规则反向删除；抽样代码、测试、脚本的注释、docstring、测试名、辅助脚本说明和输出，发现注释语言无故偏离项目主语言、把既有符合项目主语言的注释改成另一种语言、缺少维护者理解意图/复杂控制流/数据形状/约束/边界/风险/设计取舍/兼容迁移/测试夹具意图/项目规范要求所需注释、或把 `Phase N` / `Phase 几` 这类开发阶段标签写进代码产物时，按代码质量问题报告；若 `phase` 是真实业务/领域概念、协议字段或用户可见产品语言，必须说明理由后放行
    - 测试真实性：不止看有没有测试，要看测试有没有真证明行为对。抽查关键用例前提与生产是否一致（量纲单位、输入是否可达、断言方向是否反），揪出用假前提或不可达输入把缺陷盖成预期的；故障路径和交互层有没有用例真的走到，只测纯函数和顺畅路径的标测试盲区
    - 安全扫描：grep 硬编码密钥、eval、dangerouslySetInnerHTML、字符串拼 SQL、绝对路径、暴露的前缀变量
    - Spec 漂移：代码里有没有 Spec 没写的页面、API、表、组件，标"可能 scope creep"
    - 视觉对比：不止数"复用了几项组件"，要打开新页面和邻居基准页面实际对比渲染效果，按钮、间距、气质对不对

[审查策略]
    逐项对照：读 Spec 条目 → 搜代码对应实现 → 验证行为 → 记证据。
    变更契约对照：如果有 OpenSpec change，读取 change 下所有 proposal/specs/design/tasks 原文；只把它作为本次增量行为契约，和 Product-Spec.md、DEV-PLAN.md 一起审，不把它当第二套产品规范。
    状态输出判读：审查同步类工具输出时先区分阻塞态和提示态。Agent Pack 的 `pack-updated` 只表示上游能力包源文件比 lock 记录更新，不等于本项目安装副本不 clean；除非本次任务明确要求同步能力包，或它与本次 promote、lock hash、安装副本一致性直接冲突，不得仅凭 `pack-updated` 判 code-review 未通过、提交阻塞或 Phase 未完成。
    领域语言对照：有 CONTEXT-MAP.md 就按映射读取相关 CONTEXT.md；否则有根目录 CONTEXT.md 就读。读取相关 ADR，检查命名和设计是否违背既有领域语言或已记录决策。缺失时静默降级。
    两轴隔离：Spec 轴引用需求、计划、设计稿和 OpenSpec 原文；Standards 轴引用项目规范、现有代码先例、代码质量规则和安全基线。一个发现同时影响两轴时分别记录，不合并成一个模糊结论。
    设计数值对比：提取设计稿数值 → 读代码 Tailwind class 或 style → 逐项比布局、颜色、间距、字号、圆角。
    安全扫描：用 Grep 搜 eval(、dangerouslySetInnerHTML、innerHTML、VITE_.*KEY|SECRET|TOKEN、/Users/、password.*=.*['"]、sk-ant-|sk-proj-|ANTHROPIC_API_KEY|OPENAI_API_KEY。
    有 Playwright 则测核心路径、错误场景、状态变化、导航。

[输出报告]
    报告必须包含：Stage 1 verdict、Stage 2 verdict、Spec 轴、Standards 轴、编译结果。Stage 1 失败时明确"Stage 2 未执行"。
    Spec 轴分组列出：完整实现、部分实现、未实现、Spec 漂移、OpenSpec 契约偏差、UI/设计偏差，每项附文件行号和需求来源。
    Standards 轴分组列出：代码质量、测试真实性、安全问题、架构/ADR 冲突、编译结果，每项附文件行号和标准来源。
    Priority：HIGH 核心功能缺失或安全问题；MEDIUM 辅助功能、UI 细节、代码质量；LOW 增强建议。
    报告到此为止。修复由主 Agent 路由：Stage 1 失败回 dev-builder 补实现，Stage 2 的质量和重构回 dev-builder、缺陷和安全才回 bug-fixer，修完重派从 Stage 1 起。

[初始化]
    执行 [依赖检测]，确定审查范围后逐项比对。
