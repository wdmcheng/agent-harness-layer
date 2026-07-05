---
name: bug-fixer
description: 当用户说'这个功能坏了'、'报错了'、'不正常'，或报告 bug、编译错误、运行时异常时使用。通过系统性调试定位根因并修复。
---

[任务]
    系统性定位 bug 根因并修复。一次只改一个，每次改前评估影响，修后回归验证。

[调用上下文]
    用户直接报 bug → 修完建议 /code-review 验证。
    code-review Stage 2 报出缺陷或安全问题 → 主 Agent 传入失败项，修完重派 code-review 从 Stage 1 起。

[依赖检测]
    必需：项目代码、bug 描述。
    可选增强：Product-Spec.md、DEV-PLAN.md、openspec/changes/<change>/、CONTEXT.md、CONTEXT-MAP.md、docs/adr/、设计工具 MCP、Playwright、git。

[文件结构]
    bug-fixer/
    └── SKILL.md  # 本文件，无 references / templates

[第一性原则]
    不猜不试：没证据不下结论。先收集、先分析、先假设、再验证。看到报错别急着改。
    红色回路优先：先构造一个能抓住用户原始症状的 red-capable 命令，再进入假设。没有这个命令，调试只是在赌。
    一次一个：一次只改一处，改完验证有效再继续。同时改多处无法判断哪个是真修复。
    修改纪律：修 bug 也是改代码，改前评估影响，改后回归。修 A 不能坏 B。
    联网优先：不熟的报错先联网搜索，第三方库 bug 先搜 known issues。
    反复失败就停：同一个 bug 反复修不好就停下重审，可能是架构、环境或理解问题，不是代码层面。

[输出风格]
    像医生诊断：先问症状，再查体征，再下诊断，最后开药。每步有依据，不说"可能是"，说"根据 XX 证据判断是 XX"。每次修复附证据：编译输出、运行结果、前后对比。

[调试标准]
    证据：完整报错和 stack trace、复现步骤、环境信息、最近的代码变更、相关日志。
    上下文：有 CONTEXT-MAP.md 就按映射读取相关 CONTEXT.md；否则有根目录 CONTEXT.md 就读。读取相关 docs/adr/，以及与 bug 对应的 OpenSpec change artifacts。缺失时静默降级。CONTEXT/ADR 只帮助理解术语和既有决策，不替代复现证据。
    反馈回路：先建立一个命令，已经运行过一次，能驱动真实 bug 路径并断言用户的精确症状。优先级：失败测试、curl 或 HTTP 脚本、CLI fixture、Playwright、捕获 trace 回放、一次性 harness、property 或 fuzz loop、git bisect run、差分 loop。人的点击只能作为最后手段，并要记录步骤和输出。
    Red-capable 完成标准：命令能在修复前变红、修复后变绿；确定性或对 flake 有足够高复现率；运行时间按秒算；Agent 可无人值守执行。只检查"不崩溃"不算 red-capable。
    最小化：回路变红后删输入、配置、步骤和依赖，一次删一个并重跑，留下每个元素都对失败必要的最小复现。
    假设：有 red-capable 回路后再列假设。一次最多 3 个，按可能性排序，每个有可证伪预测和验证方法，先验最可能的，否定了记原因不重复验。
    修复：先把最小复现固化成回归测试；没有合适公开 seam 时说明架构风险，用更高层验证锁住行为。一次一个逻辑点 → 编译验证 tsc --noEmit 零错误 → 功能验证 bug 不再复现 → 回归验证相关功能正常。修复失败就回退重新假设，连续 3 次失败停下重审。
    进程：bug 涉及服务状态时先 kill 占用端口的进程再调试，多实例是很多灵异 bug 的根因。
    清理：删除临时 harness、trace、日志和 debug 输出；临时日志必须带唯一前缀，收尾 grep 前缀确认清干净。

[工作流程]
    收集 bug 信息，不足追问用户 → 加载相关上下文 → 建立 red-capable 反馈回路 → 复现并最小化 → 列假设和验证方法 → 一次一个假设地插桩或实验 → 实施修复和回归测试 → 清理临时调试物 → 汇报根因、改动、编译和回归证据 → 问要不要 commit，message 用 fix: 前缀。

[初始化]
    收集 bug 信息后进入调试。
