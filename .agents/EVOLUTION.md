[职责]
    进化引擎让系统越用越准。你提意见就抓成信号，开 session 时扫一遍、当场问你、同意就改，点头即生效，绝不背着你改规则。

[流程]
    一、采集
        你表达不满或纠正，detect-feedback-signal hook 即时抓一行进 [进化目录]/signals.jsonl。措辞隐晦 hook 没抓到的，主 Agent 识别后自己补记一条。这一步瞬时、无感。
    二、消化加询问
        hook 只负责采集和提示，不负责后台消化和落地。
        每次 session 启动，主 Agent 第一件事：signals.jsonl 有货就派 evolution-runner 扫它、加扫 git 历史，逐条消化成改动建议写进 proposals.md，消费掉的 signal 从 signals.jsonl 移走。消化轻量、尽快还给用户。runner 返回后主 Agent 当场把建议逐条摆给你，问同不同意，不等你手动调用 evolution-engine 技能。只要 signals.jsonl 或 proposals.md 有待处理项，主 Agent 不得直接修改建议落点文件，不得自行删除或改写 signal，不得把待升格事项当普通修复处理。
    三、按你的回应落地
        同意：主 Agent 立刻把规则改进对应文档。共享编排改 [共享规则文件]；平台专属适配只改对应 [主控文件]；技能行为进对应 SKILL.md；确定性门禁进对应 hook。若本批建议涉及升格，先给可审核升格预览，等你明确确认后才进入 agent-pack promote。
        只有你明确同意“升格到能力包”时，主 Agent 才进入 agent-pack promote --patch 或 agent-pack promote --replace 升格流程。升格必须先给出理由、影响文件和最小 patch 摘要；脚本只负责应用已确认 patch，不负责判断是否值得升格。
        全盘否定：这条 signal 和 proposal 一起删，什么都不改。
        一半一半：按你认可的那部分改，其余删。
        你的回应只对本次当场展示的这批建议有效，不跨 compact、resume、goal continuation 或下一次信号消化复用。后续再次触发消化时，即使内容相似，主 Agent 也必须重新逐条问你。
        改完即生效，没有中间缓冲。

[两个触发源]
    被动：你的纠正信号入队。
    主动：runner 消化时扫 git 历史，找反复出现的错误和修复模式。

[改什么]
    双向：该加的规则加，该退的退。已内化、从不触发、和别条重复的规则提议删，净规则量往下走。
    最小干预：例子优于规则，规则优于改 Skill，改 Skill 优于新建 Skill。
    落到对应文档：每条建议归一类，改规则、退休规则、改 Skill、建新 Skill，并写明落到哪个文件。通用规律才进进化，项目专属的归用户记忆。
    升格另行判断：多项目通用但不到个人全局的规则，先在当前项目落地；确认稳定、可复用，并且你明确同意后，才用 agent-pack promote --patch 升格确认过的片段。只有新文件或你明确批准整文件替换时，才用 agent-pack promote --replace。

[建新 Skill 的门槛]
    最后手段。模式反复出现、现有 Skill 全覆盖不了、用例子或规则或调现有 Skill 都接不住，三条都满足才提议建新 Skill，确认后主 Agent 调 skill-builder。

[文件]
    [进化目录]/signals.jsonl   待处理的纠正信号，消费即移走
    [进化目录]/proposals.md    待你拍板的改动建议，同意即改、否定即删
