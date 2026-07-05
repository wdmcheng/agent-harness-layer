---
name: evolution-engine
description: 自进化的消化引擎。由 evolution-runner 调用，drain 信号队列、扫 git 挖掘重复模式、生成改动建议供主 Agent 当场问用户。也可由用户手动 /evolution-engine 触发。
---

[任务]
    消化待处理的纠正信号，产出改动建议写进 proposals.md，供主 Agent 在 session 启动时当场问用户。不替用户决定，不自己落地。

[输入源]
    被动：[进化目录]/signals.jsonl 里的纠正信号。
    主动：git log 和 git diff，找反复出现的修复模式。

[文件结构]
    evolution-engine/
    └── SKILL.md  # 本文件，无 references / templates

[消化标准]
    只处理通用、真实的失败模式，项目专属的标出来归用户记忆。
    每条判断：删了对应规则能不能复现这个失败。能才值得立规则。
    最小干预：能用一个反例说清的就别写长规则，能改现有规则的就别新建 skill。
    强制抽象：信号常裹着产品专属名词——产品名、领域术语、具体数值。消化时全剥成类别词，规则只留产品无关的通用规律：把"某产品的某能力不该这么做"提炼成"哪类能力、在哪类处境、该怎么办"。一把尺子——换个八竿子打不着的产品这条还成立吗，不成立就是没抽够。剥下来的产品专属实例归用户记忆，不进规则。
    升格判断：只有确认规则、Skill、hook、Sub-Agent 对多个项目通用，且用户明确同意升格时，才建议主 Agent 进入 agent-pack promote --patch 或 agent-pack promote --replace 升格流程；脚本不负责判断通用性。
    升格方式：像信号进化一样先提案，写清为什么跨项目通用、影响哪些文件、准备升格哪些片段。用户确认后由主 Agent 生成相对能力包根目录的最小 patch，再用 agent-pack promote --patch 应用到能力包。

[双向扫描]
    加：通用且证据确凿的失败模式，提议立规则。
    退：扫现有规则，找你已内化的、从不触发的、和别条重复的，提议删。净规则量往下走。

[产出]
    建议写进 [进化目录]/proposals.md 的 ## 待审阅 区，每条一行 - 开头。
    每条带：依据信号、归类成改规则或退休规则或改 Skill 或建新 Skill、落到哪个文件和位置、改动摘要。落点按规则管什么走，[共享规则文件]、平台专属 [主控文件]、对应 SKILL.md、对应 hook，不堆给某一个文件。
    如果建议升格到能力包，必须单独写明为什么跨项目通用、需要用户确认后执行哪个 agent-pack promote --patch 命令；只有新文件或用户明确批准整文件替换时，才建议 agent-pack promote --replace。
    消费掉的 signal 从 signals.jsonl 移走，不重复消化。

[确认后执行]
    evolution-runner 只消化和提议。主 Agent 在 session 启动时同步消化、逐条问用户，按回应落地：
    同意：把规则改进建议指定的文件，涉及建新 Skill 就调 skill-builder。
    升格：只有用户明确同意升格时，主 Agent 才执行 agent-pack promote --patch；不要自动升格，不要默认整文件替换。
    全盘否定：这条 signal 和 proposal 一起删，什么都不改。
    一半一半：按用户认可的部分改，其余删。
    用户回应只对本次当场展示的这批建议有效，不跨 compact、resume、goal continuation 或下一次信号消化复用；后续再次触发消化时必须重新逐条问用户。
    落地后建议从 proposals.md 移走。

[初始化]
    触发就 drain signals.jsonl 加扫 git，产出建议写进 proposals.md。
