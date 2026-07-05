---
name: skill-builder
description: 当用户说要创建新技能，或 EVOLUTION.md 提议自动生成新 Skill 时使用。按照框架模块化规范创建结构一致的新 Skill。
---

[任务]
    根据用户需求或 EVOLUTION 提议，创建符合框架规范的新 Skill，结构和现有 Skill 一致、即插即用。

[依赖检测]
    无必需依赖。可选：来自进化提议时读对应 proposal 了解背景。

[文件结构]
    skill-builder/
    ├── SKILL.md               # 本文件
    └── templates/
        └── skill-template.md  # 新 Skill 骨架模板

[第一性原则]
    模板优先：先读 templates/skill-template.md 骨架按结构填，不从零写。
    参照现有：创建前读 1-2 个交互模式最接近的已有 Skill 保持一致，不发明新格式。
    最小必要：只建需要的 Section，不为"看起来完整"加空内容。
    可验收：每个关键步骤都要有 completion criterion，让 Agent 能判断这步是否真的完成。
    渐进披露：所有分支都会用到的规则留在 SKILL.md；只有部分分支才用的长参考、样例和模板下沉到 references/ 或 templates/。
    单一真相：同一条行为只写一个地方，别在 description、原则、流程和模板里重复四遍。
    联网优先：涉及不熟的领域先联网搜索了解最佳实践再设计维度和策略。

[创建规范]
    三层模块化：
    - Section 是原子能力。维度清单定义查什么收什么，策略定义怎么做，工作流程定义什么顺序。改一个 Section 不影响其他
    - Skill 是多个 Section 的组合，解决一个领域问题
    - [共享规则文件] 编排 Skill 的顺序和触发，改工作流不改 Skill 内容
    Section 分类：
    - 必须有：[任务]、[依赖检测]、[文件结构]、[第一性原则]、[初始化]
    - 推荐有：[输出风格]、[XX维度清单]、[XX策略]
    - 按需有：[信息充足度判断]、[回退策略]、[Phase 完成度判断]、多模式工作流程
    交互模式定参照：对话采集型参照 product-spec-builder、design-brief-builder；自主分析型参照 dev-planner、code-review；执行操作型参照 dev-builder、release-builder；诊断修复型参照 bug-fixer。

[技能质量标准]
    Invocation 拆分：需要 Agent 自动触发或被其他 Skill 调用的，保留 model-invoked description，description 只写触发分支和产出；只靠用户手动调用的，避免写成常驻上下文负担，可用路由 Skill 告诉用户什么时候用。
    Description 去重：一个触发分支只写一次，同义词堆叠删掉；description 不复述正文已有流程。
    Completion criterion：流程型 Skill 的每个关键步骤都要写清完成标准，标准必须能检查。比如"读完相关 ADR 并列出本次受影响决策"比"充分理解架构"可验收。
    Progressive disclosure：正文只放执行必读内容。长问题库、格式样例、术语表、外部 API 细节放 references/，正文用一句话指针说明什么时候读。
    分支隔离：一个 Skill 里出现两条互不共享上下文的用法时，优先按 invocation 或执行顺序拆 Skill，避免 Agent 带着无关后续步骤提前收工。
    No-op 清理：逐句检查是否改变 Agent 行为；不改变行为的礼貌话、重复话、空泛形容词直接删。

[写作规范]
    遵循：
    - 格式：[标题] 段、四空格缩进、中文、嵌套 [name] 块，frontmatter 只有 name 和 description
    - 不用括号写补充逻辑，直接写成正文或短句
    - 不点具体模型或产品名，直接讲本质
    - 人称用"你"，写直接的指令，不写外部观察
    - 讲规则、需求、标准，不写分步执行剧本，剩下交给你自己
    - 言简意赅，删含糊、绕、废话

[工作流程]
    了解新 Skill 解决什么、何时触发、输入输出 → 判断 invocation 是自动触发、手动触发还是路由提示 → 按交互模式找参照 Skill → 读模板定 Section → 逐个填并给关键步骤补 completion criterion → 将长参考下沉到 references/ 或 templates/ → 去重和删 no-op → 在 [技能源目录]/[skill-name]/ 创建 SKILL.md，有模板建 templates/ → 执行 ln -s ../../.agents/skills/[skill-name] [Claude技能目录]/[skill-name] 创建 Claude 暴露 symlink → 自检格式、写作规范和技能质量标准 → 在 [共享规则文件] 补 [Skill 调用规则] 和工作流程。
    如果是把已验证的项目 Skill 升格到能力包，先由主 Agent 判断跨项目通用性并征得用户明确同意，再执行 agent-pack promote --patch；新 Skill 整体升格且用户明确批准时才用 agent-pack promote --replace。

[初始化]
    收集新 Skill 需求。
