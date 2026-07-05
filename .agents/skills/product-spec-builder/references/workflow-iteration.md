---
name: workflow-iteration
description: product-spec 迭代模式工作流。用户对已有 Product-Spec.md 提变更时，追问、检测冲突、更新并记 changelog。
---

[使用时机]
    项目里已有 Product-Spec.md，用户开发中提新功能、改需求、调 UI。

[顶层规则]
    无缝衔接，不打开场白，接住需求直接往下问。
    同样守 references/interview-principles.md：苏格拉底式、一次一问、逼出具体、反失败自检。别一听改需求就讨好照单全收，该顶回去的顶回去。

[变更轻重判断]
    重度：涉及新 AI 能力、核心路径、布局结构、新增主模块。问到能答"这变更怎么影响现有产品"。
    中度：现有功能逻辑调整、局部布局。问到能答"具体改成什么样"。
    轻度：只改文字、选项、样式。确认理解即可。

[流程]
    接住需求，按轻重定追问深度，追问时按需翻 references/question-bank.md 对应维度。
    用户报新竞品或参考 → 触发搜索增强，再搜那竞品做法追问差异。
    冲突检测：加载现有 Spec，检查新需求与现有内容冲突。有冲突直接指出冲突点，加解决方案，加让用户选。
    停止追问的标准：能直接动手改 Spec，改完用户不会说"不是这个意思"。

[更新]
    在现有 Spec 上直接改，保持文档结构，只改需要改的部分。
    涉及新 AI 功能 → 补全模板扩展 AI 能力表的概率性四问，质量条、触发方式、不确定行为、降级，加护栏。
    按 templates/changelog-template.md 追加变更到 Product-Spec-CHANGELOG.md。
    变更涉及已有 DEV-PLAN 的核心路径、功能或结构时，提醒用户 DEV-PLAN 可能受影响，要不要 /dev-planner 同步。只提醒不自动改。
