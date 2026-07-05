# 产品需求规范：Claude Code Desktop

## 0. AI 使用说明

- 本文档是产品功能、范围、行为和验收标准的事实来源。
- AI MUST 优先实现 P0。
- AI MUST NOT 实现"不在本版本范围"中明确排除的内容。
- AI MUST 根据"验收标准"判断功能是否完成。
- 如果信息不明确，AI MUST 使用"假设"中的假设；如果仍无法判断，应记录到"待确认问题"，而不是自行扩展需求。

---

## 1. 产品上下文

### 1.1 产品摘要

Claude Code Desktop 是 Anthropic Claude 桌面应用内的 "Code" 标签页——一个原生桌面图形工作区（macOS + Windows），把和终端 CLI 完全相同的 Claude Code agentic-coding 引擎包进可视化界面：多窗格、可拖拽、可并行会话，带可视化 diff 审查、内嵌预览、分级权限门禁。它面向付费档开发者，让他们不开终端就能看见、引导并审批一个自主编码 agent 的工作。

### 1.2 用户问题

Claude Code 的 agentic 编码能力过去只活在终端 CLI 里，这带来两个具体痛点。

第一，终端把不用命令行的开发者挡在门外，且让多会话工作难以可视化审查：没有图形方式预览产物、管理多个 Git 隔离的并行会话、监控 CI/PR，或在细粒度上审批动作。一个开发者想同时跑三个改动、左右排开看 diff、点开预览验证页面效果，在终端里要么做不到，要么要自己写脚本拼凑。

第二，朴素的"每次都问"权限门禁制造审批疲劳——实测用户会批准约 93% 的权限弹窗，于是审批沦为机械点确认，agent 赖以生存的真实人类监督被侵蚀。开发者要么被弹窗淹没失去判断，要么直接关掉所有门禁裸奔。

### 1.3 目标用户

| 用户类型 | 描述 | 核心需求 |
|---|---|---|
| GUI-first 付费开发者 | Pro / Max / Team / Enterprise 档、想不开终端就用编码 agent 的 macOS/Windows 开发者（免费档无 Code 标签访问权） | 在图形界面里看见 diff、预览产物、驱动 agent，不碰命令行 |
| 多会话并行开发者 | 同时跑多个改动、想在一个窗口里排开窗格并行管理的开发者 | 并行会话靠 Git worktree 自动隔离，侧栏统一管理，可视化审查改动而非写脚本 |
| 监督型 power user | 想在自己掌控的粒度上中断、改向、审批自主 agent 的开发者 | 按改动 / 按应用 / 按模式控制 agent 自主度，随时 Esc 打断改向 |
| Team / Enterprise 管理员 | 在组织层面治理 agent 自主度的管理员 | 用管理控制台开关、托管设置、MDM、SSO、SSH 白名单统一管控（注意 computer use 在 Team/Enterprise 不可用） |
| 迁移中的 CLI 用户 | 想把进行中的 CLI 会话搬进 GUI 的终端用户 | 在 CLI 里 `/desktop` 一键把会话存档并搬进 Desktop |

### 1.4 核心价值

可视化地看见、引导、信任一个自主编码 agent——把 Claude Code 引擎的全部能力装进原生桌面 GUI，配可视化 diff 审查、内嵌预览、Git 隔离的并行会话，以及"放行安全的、拦住危险的"分级权限门禁，让开发者不必忍受终端摩擦或审批疲劳就能保住真实监督权。

### 1.5 成功标准

| 判断标准 | 目标 / 信号 |
|---|---|
| 终端门槛被移除 | 付费档开发者在 Code 标签内无需任何终端命令即可完成"提需求 → agent 改代码 → 看 diff → 审批 → 验证"全链路 |
| 审批疲劳被缓解 | Auto 模式让安全动作自动放行、危险动作走命名逃生舱，开发者审批的是真正需要判断的少数动作，而非机械批准 ~93% 的弹窗 |
| 并行会话真隔离 | 多个并行会话各自跑在独立 Git worktree，并发会话从不共享未提交改动 |
| 自主可被收回 | 用户可随时 Esc 打断、改向（改向被当作正常输入而非失败）；Auto 模式在 3 次连续或 20 次累计拦截后自动暂停转回询问 |

---

## 2. 范围

### 2.1 本版本范围

| 编号 | 内容 | 优先级 | 备注 |
|---|---|---|---|
| SCOPE-001 | 多窗格可拖拽工作区：8 个命名窗格（chat / diff / preview / terminal / file / plan / tasks / subagent），从 Views 菜单打开、拖头部、边缘缩放 | P0 | 需 Claude Desktop v1.2581.0+ |
| SCOPE-002 | 持久左侧会话侧栏：列活跃 + 近期归档会话、新建会话、按状态/项目/环境过滤、按项目分组、状态指示器与 Dispatch/bg 徽章、split-view | P0 | 一个窗口管多会话 |
| SCOPE-003 | 可视化 diff 审查：文件列表 + 逐文件改动、点击行开内联评论框、"Review code" 按钮让 Claude 评审 diff 留内联评论、Ask 模式逐改动 Accept/Reject | P0 | 2026 年 4 月重建的 diff viewer |
| SCOPE-004 | 内嵌预览窗格：跑本地 dev server，打开 HTML/PDF/图片/视频；autoVerify 默认开，Claude 截图、查 DOM、点元素、填表单并迭代自查 | P0 | 由 `.claude/launch.json` 配置 |
| SCOPE-005 | 分级权限模式（Desktop 暴露 5 种）：Ask permissions（默认）、Auto accept edits、Plan mode、Auto（分类器中介，research preview）、Bypass permissions（默认关、Settings 开） | P0 | CLI 第 6 种 `dontAsk` 不在 Desktop |
| SCOPE-006 | Agentic 编码循环：收集上下文 → 采取动作 → 验证结果，跨数十次链式工具调用自我纠错直到完成；会话持久化为 `~/.claude/projects/` 下 JSONL | P0 | 引擎与 CLI 同源 |
| SCOPE-007 | 五类内建工具：文件操作、搜索、执行、Web、代码智能（编辑后类型错误、跳转定义、查引用，经 language-server 插件） | P0 | |
| SCOPE-008 | Checkpoints / Rewind：每次编辑前快照文件内容，Esc-Esc 或 `/rewind` 回滚文件 + 对话（会话本地、独立于 git） | P0 | 不能撤销 DB/API/部署副作用 |
| SCOPE-009 | 集成终端窗格（Ctrl+\`，仅本地会话）+ 内嵌文件编辑器窗格（Save/Discard、磁盘改动警告，本地 + SSH） | P0 | |
| SCOPE-010 | Plan mode：只用只读工具探索，给出计划并提供 approve/accept-edits/manual/keep-planning/refine 选项，任何源码编辑前先批计划 | P0 | plan 窗格是可拖拽窗格非模态 |
| SCOPE-011 | Side chat（Cmd/Ctrl+; 或 `/btw`）：分支对话读主线上下文但不写回，探索性提问不污染主会话 | P1 | |
| SCOPE-012 | Subagents：隔离 worker，独立上下文窗口/prompt/工具/权限；内建 Explore（Haiku 只读）、Plan（只读）、General-purpose；自定义经 `.claude/agents/` Markdown+YAML；不可嵌套，只回摘要 | P1 | |
| SCOPE-013 | MCP 服务集成：HTTP/SSE(已弃)/stdio/WebSocket 传输，local/project/user 配置域，Tool Search 默认开（启动只载工具名、schema 延迟到近零空闲成本），`/mcp` 看状态/token 成本/OAuth | P1 | |
| SCOPE-014 | Hooks：确定性事件驱动脚本（command/http/mcp_tool/prompt/agent 类型）覆盖 30 个生命周期事件；PreToolUse 为主安全检查点（deny/allow/ask/defer，exit code 2 拦截） | P1 | |
| SCOPE-015 | 记忆：CLAUDE.md（用户写，4 域，root→cwd 拼接，@import 至多 4 跳，建议 <200 行）+ Auto-memory（Claude 写，AutoDream 跨会话整合） | P1 | |
| SCOPE-016 | CI/PR 监控：PR 开后状态栏经 GitHub CLI 轮询检查，Auto-fix（Claude 读失败迭代）与 Auto-merge（全绿后 squash）开关，CI 完成 OS 通知 | P1 | |
| SCOPE-017 | CLI 共享配置与迁移：Desktop 与 CLI 共享 CLAUDE.md/`~/.claude.json`/`.mcp.json`/hooks/skills/settings.json（会话历史分开）；CLI `/desktop` 把会话搬进 Desktop | P1 | 不支持 API-key/Bedrock/Vertex/Foundry 鉴权 |
| SCOPE-018 | 上下文管理：usage ring 显示单会话上下文 + plan 用量；近上限自动压缩（先清旧工具输出再摘要）；`/compact` 提前手动压缩可带 focus | P1 | |
| SCOPE-019 | 后台工作：`/background` 分离会话、`/tasks` 列会话内后台工作、`/workflows` 看/暂停/恢复、`/batch` 把大改动拆成 5–30 个隔离 worktree 子 agent 各开 PR；后台会话在 `/resume` 标 'bg' | P1 | |
| SCOPE-020 | Extended thinking 默认开（Opus 4.6/Sonnet 4.6 自适应推理）；effort 级别 low/medium(默认)/high/xhigh/max/ultracode 经 `/effort`；`MAX_THINKING_TOKENS=0` 关 | P1 | |
| SCOPE-021 | 企业管控：管理控制台开关（Code 标签/web 会话/Remote Control）、托管设置、MDM（macOS Jamf/Kandji 经 com.anthropic.Claude；Windows 注册表 SOFTWARE\Policies\Claude）、SSO（SAML/OIDC）、SSH 主机白名单 | P1 | |

### 2.2 不在本版本范围

| 编号 | 内容 | 原因 |
|---|---|---|
| OUT-001 | Linux 桌面客户端 | Desktop 仅 macOS + Windows，Linux 开发者必须用 CLI；不在本产品形态内 |
| OUT-002 | 脚本化 / headless 执行（`--print`、`--output-format`、`dontAsk` 模式、per-session `--allowedTools`/`--disallowedTools` flag） | Desktop 是交互式专属；脚本化与 CI 场景归 CLI |
| OUT-003 | CLI 专属交互命令 `/permissions`、`/config`、`/agents`、`/doctor` | 这些是 CLI 终端命令，Code 标签内不提供 |
| OUT-004 | 第三方模型 provider（Bedrock / Vertex / Foundry） | Desktop 与这些鉴权方式不兼容；归 CLI |
| OUT-005 | Agent teams（peer-to-peer 会话经共享任务表互发消息） | 实验性、默认关、CLI only，token 成本更高；本版本不进 Desktop |
| OUT-006 | Computer use 在 Team / Enterprise | Computer use 仅 Pro/Max，Team/Enterprise 明确不可用 |
| OUT-007 | 内建 Eval / 回归测试 harness、单任务美元成本上限 / 熔断 | 调研未发现这两个原语为内建能力；记为未提供而非编造（见 §11.5 / §11.6） |
| OUT-008 | 远程会话中的 @mention、plugins、Ask 模式、Bypass 模式 | 远程会话能力受限，这几项不可用 |

---

## 3. 用户任务

| 编号 | 用户任务 | 用户类型 | 优先级 |
|---|---|---|---|
| TASK-001 | 不开终端、用自然语言驱动 agent 完成一个编码任务并审批其改动 | GUI-first 付费开发者 | P0 |
| TASK-002 | 可视化审查 agent 产生的 diff，逐改动接受/拒绝，必要时让 Claude 自评审 | GUI-first 付费开发者 | P0 |
| TASK-003 | 同时跑多个互不干扰的并行会话，在一个窗口里排开管理 | 多会话并行开发者 | P0 |
| TASK-004 | 内嵌预览跑起来的产物，让 agent 自查页面/接口效果并迭代 | GUI-first 付费开发者 | P0 |
| TASK-005 | 选择并随时切换 agent 的自主级别，把危险动作挡在门外 | 监督型 power user | P0 |
| TASK-006 | 中断跑偏的 agent、改向，或回滚到改动前状态 | 监督型 power user | P0 |
| TASK-007 | 让 agent 先出计划再动手，批准后才允许改源码 | 监督型 power user | P1 |
| TASK-008 | PR 开后监控 CI，失败自动修、全绿自动合 | GUI-first 付费开发者 | P1 |
| TASK-009 | 在组织层面统一管控成员的 agent 自主度与可用能力 | Team / Enterprise 管理员 | P1 |
| TASK-010 | 把 CLI 里进行中的会话迁进 Desktop GUI 继续 | 迁移中的 CLI 用户 | P1 |

---

## 4. 用户流程

### FLOW-001: 单会话编码并审批（Ask 模式默认路径）

**关联任务：** TASK-001、TASK-002、TASK-005、TASK-006
**优先级：** P0
**目标：** 让开发者在 GUI 里用自然语言驱动 agent 改代码，逐改动审批，全程不碰终端。

**入口：**
Claude Desktop 窗口顶部中央三标签（Chat / Cowork / Code）中点 Code；侧栏点 "+ New session"（Cmd/Ctrl+N）建会话。

**主路径：**
1. 用户在底部 chat 窗格输入自然语言需求（可拖拽文件/图片、`@` 触发文件自动补全）。
2. Agent 收集上下文：读文件、搜索代码库，工具调用按当前 view mode（Normal/Verbose/Summary）渲染进 transcript。
3. Agent 提出动作（编辑、命令、工具调用），受当前 Ask 模式门禁——读操作自动放行，文件编辑与 shell 命令前弹审批。
4. 用户在 diff 窗格审查改动（左文件列表、右逐文件改动、header 显示 `+12 -1` 类 diff-stats），逐改动 Accept/Reject。
5. Agent 验证：跑测试，预览窗格 autoVerify 截图查 DOM 自查。
6. 完成，transcript 出最终结果，侧栏会话状态转 finished。

**分支路径：**
- 用户点 "Review code" 按钮 → Claude 评审 diff 并留内联评论 → 用户据评论决定接受/拒绝。
- 用户点 diff 某行 → 开内联评论框（Cmd/Ctrl+Enter 提交多行）→ agent 据评论改向。
- 用户按 Cmd/Ctrl+; 开 side chat 问探索性问题 → 不写回主线，不污染主会话。

**边界情况：**
- Agent 跑偏：用户按 Esc 停止响应，输入改向指令——改向被当作正常输入而非失败。
- 改坏了：Esc-Esc 或 `/rewind` 回滚文件 + 对话到改动前；但若已写 DB/调 API/部署，这些副作用无法回滚。
- 无活跃付费档：Code 标签返回 403。
- Windows 未装 Git for Windows 或未重启：Code 标签不工作。

**完成状态：**
改动落盘（用户接受的部分），transcript 留完整记录，会话持久化为 `~/.claude/projects/` 下 JSONL，侧栏会话标 finished。

---

### FLOW-002: 多会话并行开发（worktree 隔离）

**关联任务：** TASK-003
**优先级：** P0
**目标：** 让开发者同时跑多个改动、各自 Git 隔离、在一个窗口排开管理。

**入口：**
侧栏 "+ New session" 建第二、第三个会话；每个会话自动隔离在独立 Git worktree（默认 `<project-root>/.claude/worktrees/`）。

**主路径：**
1. 用户连续建多个会话，各给不同需求。
2. 每个会话在自己的 worktree 里跑，并发会话从不共享未提交改动。
3. 用户在侧栏按状态/项目/环境过滤、按项目分组，Ctrl+Tab 循环切换。
4. 用户 Cmd/Ctrl-click 第二个会话开 split-view，左右排开同时看两个会话。
5. 各会话独立推进，侧栏状态指示器实时反映（running / needs approval / finished / PR created）。

**分支路径：**
- 用户 `/batch` 把一个大改动拆成 5–30 个隔离 worktree 子 agent，各自开一个 PR。
- 用户 `/background` 把某会话分离到后台 headless 跑，释放前台；后台会话在侧栏和 `/resume` 标 'bg'。
- Dispatch（Cowork 标签）派生的会话在侧栏得 'Dispatch' 徽章。

**边界情况：**
- 远程会话：跑在 Anthropic 云 VM，应用关闭后继续，可从 claude.ai/code 或 iOS 监控。
- Cmd/Ctrl+\ 关闭聚焦窗格，不误关整个会话。

**完成状态：**
多个会话各自完成，互不污染对方的未提交改动；用户在一个窗口内完成了原本要多终端拼凑的并行工作。

---

### FLOW-003: Plan 模式先批计划再动手

**关联任务：** TASK-007、TASK-005
**优先级：** P1
**目标：** 高风险改动前，让 agent 只读探索出计划，用户批准后才允许改源码。

**入口：**
mode 选择器（Cmd+Shift+M）或 Shift+Tab 循环切到 Plan mode。

**主路径：**
1. 用户切到 Plan mode 并提需求。
2. Agent 只用只读工具探索代码库，不做任何源码编辑。
3. Agent 在 plan 窗格（可拖拽窗格，非模态）给出计划。
4. 用户选择：approve / accept-edits / manual / keep-planning / refine。
5. 批准后 agent 切到执行模式开始改源码。

**分支路径：**
- 用户选 refine → 给修改意见 → agent 重出计划。
- 用户选 keep-planning → agent 继续探索深化计划。

**边界情况：**
- Plan 模式下 agent 试图编辑源码 → 被模式拦住，因为只读工具不含写。

**完成状态：**
用户对计划有数后才放行执行，避免 agent 在未对齐时直接改一大片。

---

### FLOW-004: Auto 模式分类器中介的自主执行

**关联任务：** TASK-005、TASK-006
**优先级：** P1（research preview）
**目标：** 让安全动作自动放行、危险动作走命名逃生舱，缓解审批疲劳同时保住对危险操作的控制。

**入口：**
mode 选择器切到 Auto（research preview；Opus 4.6+/Sonnet 4.6；仅 Anthropic API，不支持 Bedrock/Vertex）。

**主路径：**
1. 用户切到 Auto 模式提需求。
2. 服务端独立分类器逐个动作评审。
3. 放行：本地文件操作、依赖安装、只读 HTTP、推到 Claude 起始的那个分支。
4. 拦截：`curl|bash`、密钥外泄、生产部署/迁移、批量删除、IAM 变更、force-push/push-to-main。
5. 输入层 prompt-injection 探针在工具输出进入上下文前扫描。
6. Agent 在边界内自主推进直到完成。

**分支路径：**
- 用户在对话里说 "don't push" → Auto 模式把它当会话内边界执行，直到解除。
- 子 agent 工作也在 spawn、执行中、完成后被分类器检查。

**边界情况：**
- 累计 3 次连续或 20 次总拦截 → 分类器自动暂停，转回询问，防失控。
- 危险动作被拦 → 转人确认，不静默执行。

**完成状态：**
开发者审批的是真正需判断的少数动作，安全动作自动完成，危险动作有命名逃生舱兜底。

---

### FLOW-005: PR 开后 CI 监控与自动修/合

**关联任务：** TASK-008
**优先级：** P1
**目标：** PR 开后自动盯 CI，失败自动修、全绿自动合，减少手动盯检查。

**入口：**
会话产生的改动开出 PR 后，CI 状态栏自动出现。

**主路径：**
1. PR 开后，状态栏经 GitHub CLI 轮询检查结果。
2. 用户打开 Auto-fix 开关 → CI 失败时 Claude 读失败信息并迭代修。
3. 用户打开 Auto-merge 开关 → 全部检查通过后 squash 合并。
4. CI 完成时触发 OS 通知。

**分支路径：**
- Auto-fix 修不动 → 转人，用户介入。

**边界情况：**
- 无 PR 时不显示 CI 状态栏。

**完成状态：**
PR 检查从开到合在状态栏可见可控，用户不必手动逐个盯检查。

---

### FLOW-006: 组织层面治理 agent 自主度

**关联任务：** TASK-009
**优先级：** P1
**目标：** 让 Team/Enterprise 管理员在组织层面统一管控成员的 agent 自主度与可用能力，而非靠每个成员各自设置。

**入口：**
管理员在 Anthropic 管理控制台进入 Claude Code 治理设置。

**主路径：**
1. 管理员用管理控制台开关启停 Code 标签 / web 会话 / Remote Control。
2. 管理员下发托管设置（managed settings），统一约束成员的权限模式与可用能力。
3. 管理员经 MDM（macOS Jamf/Kandji 经 com.anthropic.Claude；Windows 注册表 SOFTWARE\Policies\Claude）做设备管理与 SSH 预配。
4. 管理员配 SSO（SAML/OIDC）接入企业身份。
5. 管理员维护 SSH 主机白名单，限定成员可连的远程机器。

**分支路径：**
- 管理员经 MDM 预配 SSH 连接，成员无需手动填主机即可连。
- 管理员在管理控制台禁用 Bypass permissions，成员无法自行开启裸奔模式。

**边界情况：**
- Computer use 在 Team/Enterprise 不可用，管理员无需也无法为其配权（见 OUT-006）。
- 托管设置（managed-policy 作用域）优先级最高，成员的 user/project/local 作用域不能覆盖它。

**完成状态：**
组织内成员的 agent 自主度与可用能力受管理员统一治理，越权能力被集中禁用，设备与身份经 MDM/SSO 纳管。

---

### FLOW-007: 把 CLI 进行中的会话迁进 Desktop

**关联任务：** TASK-010
**优先级：** P1
**目标：** 让终端用户把一个进行中的 CLI 会话无缝搬进 Desktop GUI 继续，不必重开会话或丢上下文。

**入口：**
用户在 CLI 的进行中会话里执行 `/desktop`。

**主路径：**
1. 用户在 CLI 会话中输入 `/desktop`。
2. CLI 保存当前会话并把它移入 Desktop 应用（macOS/Windows）。
3. 用户在 Desktop 的 Code 标签侧栏看到该会话，点开继续。
4. 共享配置（CLAUDE.md / `~/.claude.json` / `.mcp.json` / hooks / skills / settings.json）在两端一致，迁移后行为连贯；会话历史本身两端独立。

**分支路径：**
- 迁移后用户在 Desktop 用窗格 / diff / 预览等 GUI 能力继续推进原 CLI 会话。

**边界情况：**
- 以 API-key / Bedrock / Vertex / Foundry 鉴权运行时，`/desktop` 迁移不支持（见 OUT-004）。
- Windows 未装 Git for Windows 或未重启应用时，Code 标签不工作，迁移目标不可用（见 FLOW-001 边界、DEP-002）。

**完成状态：**
原 CLI 会话出现在 Desktop 侧栏并可继续，用户从终端无缝转入图形工作区，配置一致、上下文不丢。

---

## 5. 功能需求

### REQ-001: 分级权限模式

**优先级：** P0
**关联任务：** TASK-005
**关联流程：** FLOW-001、FLOW-003、FLOW-004

**用途：**
朴素"每次都问"制造审批疲劳（用户批准约 93% 弹窗），侵蚀真实监督。分级模式让自主度成为一个可调档的旋钮：从每次都问到分类器中介自动放行，并为破坏性操作保留命名逃生舱。

**行为：**
Desktop 暴露 5 种模式（CLI 共 6 种）。规则求值顺序固定 deny → ask → allow，首个匹配生效，所以 deny 永远在前（默认最小权限，在具体文件/目录上授权，只对例行命令加白名单）。

- **Ask permissions（默认）**：文件编辑与 shell 命令前弹审批；读自动放行；用户看 diff + 逐改动 Accept/Reject。
- **Auto accept edits**：工作目录内文件编辑 + 常见文件系统命令（mkdir/touch/rm/rmdir/mv/cp/sed）自动放行；其他 shell 命令仍弹。
- **Plan mode**：只读工具，不改源码，先批计划。
- **Auto（research preview；Opus 4.6+/Sonnet 4.6；仅 Anthropic API）**：服务端分类器逐动作评审，详见 REQ-002。
- **Bypass permissions**：禁用全部检查立即执行；默认关，Settings → Claude Code 开；等价于 `--dangerously-skip-permissions`（显式命名以示意图）；以 root/sudo 跑时被拦；企业管理员可禁用。

受保护路径（.git、.gitconfig、.bashrc 等）除 bypassPermissions 外从不自动放行。

**规则：**
- MUST 默认进入 Ask permissions 模式。
- MUST 按 deny → ask → allow 顺序求值，首个匹配生效。
- MUST 在受保护路径上拒绝自动放行（除非 Bypass）。
- MUST 在 root/sudo 下拦住 Bypass permissions。
- MUST 支持会话中途切换：mode 选择器（Cmd+Shift+M）或 Shift+Tab 循环（default→acceptEdits→plan）。
- MUST NOT 在 Desktop 暴露 CLI-only 的 `dontAsk` 模式。
- MUST NOT 在远程会话提供 Ask 模式与 Bypass 模式。
- SHOULD 在 Bypass 默认关闭状态下要求用户显式去 Settings 开启。

**输入：**

| 字段 | 类型 | 必填 | 校验规则 |
|---|---|---:|---|
| 当前权限模式 | 枚举（Ask/AcceptEdits/Plan/Auto/Bypass） | Yes | 远程会话排除 Ask 与 Bypass；Auto 仅 Opus 4.6+/Sonnet 4.6 + Anthropic API |
| 切换触发 | 快捷键 / 菜单 | No | Cmd+Shift+M 或 Shift+Tab |

**输出 / 结果：**
- 当前模式在 prompt 区的权限-模式选择器显示。
- 按模式决定动作是自动放行、弹审批、还是被拒。

**状态：**
- 默认：Ask permissions。
- 加载：模式切换即时生效，无加载态。
- 空状态：N/A（始终有一个生效模式）。
- 错误：以 root/sudo 试开 Bypass → 被拦并提示。
- 成功：模式切换后，后续动作按新模式门禁。

**验收标准：**
- [ ] AC-001: Given 新建会话, when 用户未切换模式, then 当前模式为 Ask permissions 且文件编辑前弹审批、读操作不弹。
- [ ] AC-002: Given 远程会话, when 用户打开模式选择器, then 选项中不含 Ask 与 Bypass。
- [ ] AC-003: Given 一条 deny 规则与一条 allow 规则同时匹配某动作, when 求值, then deny 生效、动作被拒。
- [ ] AC-004: Given 以 root/sudo 运行, when 用户试图启用 Bypass permissions, then 被拦截且不执行。

---

### REQ-002: Auto 模式分类器

**优先级：** P1（research preview）
**关联任务：** TASK-005、TASK-006
**关联流程：** FLOW-004

**用途：**
让一个服务端独立模型替代人类审批者去放行安全动作，同时为破坏性动作保留命名逃生舱——既缓解审批疲劳，又不在危险操作上失控。

**行为：**
仅 Opus 4.6+/Sonnet 4.6、仅 Anthropic API（不支持 Bedrock/Vertex）。分类器逐个动作评审：
- **放行**：本地文件操作、依赖安装、只读 HTTP、推到 Claude 起始的那个分支。
- **拦截**：`curl|bash`、密钥外泄、生产部署/迁移、批量删除、IAM 变更、force-push/push-to-main。
- **会话内边界**：执行用户在对话里下达的边界（如 "don't push"）直到解除。
- **输入层防注入**：prompt-injection 探针在工具输出进入上下文前扫描。
- **子 agent**：子 agent 工作在 spawn、执行中、完成后均被分类器检查。

**规则：**
- MUST 放行本地文件操作、依赖安装、只读 HTTP、推到起始分支。
- MUST 拦截 `curl|bash`、密钥外泄、生产部署/迁移、批量删除、IAM 变更、force-push/push-to-main。
- MUST 在 3 次连续或 20 次累计拦截后自动暂停并转回询问。
- MUST 在工具输出进入上下文前运行 prompt-injection 探针。
- MUST 对子 agent 在 spawn / 执行中 / 完成后三个时点检查。
- MUST 执行会话内口头边界（如 "don't push"）直到用户解除。
- SHOULD 把被拦动作转人确认而非静默丢弃。

**输入：**

| 字段 | 类型 | 必填 | 校验规则 |
|---|---|---:|---|
| 待评审动作 | 动作描述（文件操作/命令/HTTP/git 等） | Yes | 逐个评审 |
| 工具输出 | 文本 | Yes | 入上下文前过防注入探针 |
| 会话内边界 | 自然语言约束 | No | 解除前持续生效 |

**输出 / 结果：**
- 每个动作得到 放行 / 拦截 / 转人 的判定。
- 连续 3 次或累计 20 次拦截后会话转入 Auto-mode paused 态。

**状态：**
- 默认：分类器对每个动作做判定。
- 加载：服务端评审有短延迟（[待补充] 具体延迟数值，调研未给）。
- 空状态：N/A。
- 错误：分类器不可用时降级行为 [待补充]（调研未明确）。
- 成功：安全动作自动完成，危险动作被命名逃生舱拦下。

**验收标准：**
- [ ] AC-001: Given Auto 模式, when agent 试图 `curl|bash` 或 force-push/push-to-main, then 该动作被拦截。
- [ ] AC-002: Given Auto 模式, when 累计达到 3 次连续或 20 次总拦截, then 分类器自动暂停并转回询问。
- [ ] AC-003: Given 用户在对话里说 "don't push", when agent 后续尝试 push, then push 被拦直到用户解除该边界。
- [ ] AC-004: Given 某工具输出含 prompt-injection, when 它将进入上下文, then 输入层探针先扫描。

---

### REQ-003: 可视化 Diff 审查

**优先级：** P0
**关联任务：** TASK-002
**关联流程：** FLOW-001

**用途：**
终端里 diff 难以可视化逐改动审查。重建的 diff viewer 让开发者点击审查、内联评论、并可让 Claude 自评审。

**行为：**
diff 窗格左列文件列表、右侧逐文件改动；点击行开内联评论框（Cmd/Ctrl+Enter 提交多行）；"Review code" 按钮让 Claude 评审 diff 并留内联评论；Ask 模式下逐改动 Accept/Reject；会话 header 显示 `+12 -1` 类 diff-stats 指示器。

**规则：**
- MUST 提供文件列表 + 逐文件改动视图。
- MUST 支持点击行打开内联评论框，Cmd/Ctrl+Enter 提交多行评论。
- MUST 提供 "Review code" 让 Claude 评审 diff 并留内联评论。
- MUST 在 Ask 模式提供逐改动 Accept/Reject。
- SHOULD 在会话 header 显示 diff-stats（如 `+12 -1`）。

**输入：**

| 字段 | 类型 | 必填 | 校验规则 |
|---|---|---:|---|
| 内联评论 | 文本（可多行） | No | Cmd/Ctrl+Enter 提交 |
| 逐改动决定 | Accept / Reject | Yes（Ask 模式） | 仅 Ask 模式逐改动 |

**输出 / 结果：**
- 用户接受的改动落盘，拒绝的不落盘。
- Claude 评审产生的内联评论显示在对应行。

**状态：**
- 默认：diff 窗格展示当前会话改动。
- 加载：[待补充]（diff 渲染加载态视觉，调研未给）。
- 空状态：无改动时 diff 窗格为空。
- 错误：[待补充]。
- 成功：改动按用户决定落盘/丢弃。

**验收标准：**
- [ ] AC-001: Given diff 窗格有改动, when 用户点某行, then 打开内联评论框且 Cmd/Ctrl+Enter 可提交多行评论。
- [ ] AC-002: Given Ask 模式有多处改动, when 用户对某处点 Reject, then 该处不落盘、其余处不受影响。
- [ ] AC-003: Given 用户点 "Review code", when Claude 评审完成, then 在相关行留下内联评论。
- [ ] AC-004: Given 会话有 12 行新增 1 行删除, when 查看会话 header, then diff-stats 显示 `+12 -1`。

> 注：diff viewer 是 unified 还是 split/side-by-side 未在官方文档确认（一份第三方评测称 unified-only），记为 [待补充]，见 §10.2 Q-002。

---

### REQ-004: 内嵌预览与 autoVerify

**优先级：** P0
**关联任务：** TASK-004
**关联流程：** FLOW-001

**用途：**
没有图形方式预览产物，开发者无法直观验证页面/接口效果。内嵌预览跑本地 server 并让 agent 自查迭代。

**行为：**
预览窗格跑本地 dev server，打开 HTML/PDF/图片/视频；autoVerify 默认开时，Claude 截图、查 DOM、点元素、填表单，并对发现的问题迭代；经 `.claude/launch.json` 配置；窗格有 start/stop + "Persist sessions" 开关；Cmd+Shift+S 在预览里选元素。

**规则：**
- MUST 支持跑本地 dev server 并打开 HTML/PDF/图片/视频。
- MUST 在 autoVerify 开（默认）时让 Claude 截图、查 DOM、点元素、填表单并迭代。
- MUST 由 `.claude/launch.json` 配置。
- SHOULD 提供 start/stop 与 "Persist sessions" 开关。

**输入：**

| 字段 | 类型 | 必填 | 校验规则 |
|---|---|---:|---|
| launch.json 配置 | 文件 | Yes | 位于 `.claude/launch.json` |
| autoVerify 开关 | 布尔 | No | 默认开 |

**输出 / 结果：**
- 预览窗格渲染产物。
- autoVerify 开时 agent 自查并对发现的问题迭代。

**状态：**
- 默认：autoVerify 开，预览窗格可跑 server。
- 加载：dev server 启动中。
- 空状态：无配置/未启动时预览窗格空。
- 错误：[待补充]（server 启动失败态视觉，调研未给）。
- 成功：产物正确渲染，agent 自查通过。

**验收标准：**
- [ ] AC-001: Given `.claude/launch.json` 配好 dev server, when 用户启动预览, then 预览窗格渲染该 server 的页面。
- [ ] AC-002: Given autoVerify 开, when Claude 完成一处页面改动, then 它截图并查 DOM 自查，对发现的问题迭代。
- [ ] AC-003: Given 预览中, when 用户按 Cmd+Shift+S, then 可在预览里选元素。

---

### REQ-005: Checkpoints / Rewind

**优先级：** P0
**关联任务：** TASK-006
**关联流程：** FLOW-001

**用途：**
agent 改坏时需要撤销。Checkpoints 在每次编辑前快照，Rewind 回滚文件 + 对话。

**行为：**
Claude 每次编辑前快照文件内容；Esc-Esc 或 `/rewind` 回滚文件 + 对话；会话本地、独立于 git；不能撤销 DB/API/部署副作用。

**规则：**
- MUST 在每次编辑前快照文件内容。
- MUST 支持 Esc-Esc 或 `/rewind` 回滚文件与对话到改动前。
- MUST 与 git 独立、会话本地。
- MUST NOT 声称能撤销 DB/API/部署等外部副作用。

**输入：**

| 字段 | 类型 | 必填 | 校验规则 |
|---|---|---:|---|
| 回滚触发 | Esc-Esc 或 `/rewind` | Yes | 会话内 |

**输出 / 结果：**
- 文件与对话回到选定 checkpoint。
- 外部副作用（DB/API/部署）不被回滚。

**状态：**
- 默认：每次编辑前自动快照。
- 加载：回滚即时。
- 空状态：无 checkpoint 时无可回滚点。
- 错误：试图回滚已发生的外部副作用 → 不生效（只回文件+对话）。
- 成功：文件+对话回到改动前。

**验收标准：**
- [ ] AC-001: Given agent 已做若干编辑, when 用户 Esc-Esc 或 `/rewind`, then 文件与对话回到所选 checkpoint。
- [ ] AC-002: Given agent 已调用外部 API 产生副作用, when 用户回滚, then 文件+对话回滚但该 API 副作用不被撤销。

---

### REQ-006: 多窗格可拖拽工作区

**优先级：** P0
**关联任务：** TASK-001、TASK-002、TASK-003、TASK-004
**关联流程：** FLOW-001、FLOW-002

**用途：**
单一聊天视图不够，开发者要把 diff、预览、终端、计划等排开。多窗格工作区提供 8 个命名窗格自由拖拽缩放。

**行为：**
8 个命名窗格——chat（transcript）、diff、preview、terminal、file（editor）、plan、tasks、subagent——从 Views 菜单打开、拖头部、边缘缩放。中央 chat 窗格流式 transcript，工具调用细节由三种 view mode（Normal/Verbose/Summary）经 Transcript-view 下拉或 Ctrl+O 控制；点击文件路径在 file/preview 窗格打开；右键路径 → Attach as context / Open in（VS Code, Cursor, Zed）/ Show in Finder-Explorer / Copy path。窗格布局 + 终端 + 文件编辑器 + view modes 需 Claude Desktop v1.2581.0+。

**规则：**
- MUST 提供 8 个命名窗格，可从 Views 菜单打开、拖头部、边缘缩放。
- MUST 在中央 chat 窗格流式渲染 transcript。
- MUST 用三种 view mode（Normal/Verbose/Summary）控制工具调用细节，经 Transcript-view 下拉或 Ctrl+O 切换。
- MUST 在窗格布局/终端/文件编辑器/view modes 上要求 v1.2581.0+。
- SHOULD 支持点击文件路径在 file/preview 窗格打开、右键路径上下文菜单。

**输入：**

| 字段 | 类型 | 必填 | 校验规则 |
|---|---|---:|---|
| view mode | 枚举（Normal/Verbose/Summary） | No | Ctrl+O 循环或下拉选 |
| 窗格操作 | 打开/拖拽/缩放 | No | 需 v1.2581.0+ |

**输出 / 结果：**
- 用户自定义的窗格布局。
- transcript 按选定 view mode 渲染（Normal 折叠成摘要 / Verbose 每步 / Summary 仅最终响应+改动）。

**状态：**
- 默认：chat 窗格 + Normal view mode。
- 加载：流式 transcript token-by-token 渲染。
- 空状态：新会话 transcript 空。
- 错误：低于 v1.2581.0 时窗格布局等不可用。
- 成功：窗格按用户布局排列。

**验收标准：**
- [ ] AC-001: Given v1.2581.0+, when 用户从 Views 菜单打开 diff 窗格并拖动头部, then 该窗格可移动并在边缘缩放。
- [ ] AC-002: Given transcript 有工具调用, when 用户 Ctrl+O 切到 Summary, then 仅显示最终响应 + 改动，不显示每步工具调用。
- [ ] AC-003: Given transcript 有文件路径, when 用户右键该路径, then 出现 Attach as context / Open in / Show in Finder-Explorer / Copy path 菜单项。

---

### REQ-007: 会话侧栏与 Git worktree 隔离

**优先级：** P0
**关联任务：** TASK-003
**关联流程：** FLOW-002

**用途：**
多会话需统一管理且互不污染。侧栏管会话，worktree 保证并发会话不共享未提交改动。

**行为：**
持久左侧侧栏列活跃 + 近期归档会话，"+ New session"（Cmd/Ctrl+N），按状态/项目/环境过滤，按项目分组，Ctrl+Tab 循环；每会话状态指示器（running / needs approval / finished / PR created），Dispatch 与 'bg' 徽章；Cmd/Ctrl-click 第二会话开 split-view（Cmd/Ctrl+\ 关闭聚焦窗格）。每个会话自动隔离在独立 Git worktree（默认 `<project-root>/.claude/worktrees/`）。

**规则：**
- MUST 为每个会话自动创建独立 Git worktree，并发会话不共享未提交改动。
- MUST 在侧栏列活跃 + 近期归档会话并显示状态指示器。
- MUST 支持按状态/项目/环境过滤、按项目分组。
- MUST 给 Dispatch 派生会话标 'Dispatch' 徽章、后台会话标 'bg'。
- SHOULD 支持 Cmd/Ctrl-click 开 split-view。

**输入：**

| 字段 | 类型 | 必填 | 校验规则 |
|---|---|---:|---|
| 过滤维度 | 状态 / 项目 / 环境 | No | |
| split-view 触发 | Cmd/Ctrl-click | No | |

**输出 / 结果：**
- 侧栏列出会话及其状态/徽章。
- 各会话在独立 worktree 跑，改动互不串。

**状态：**
- 默认：侧栏列当前会话。
- 加载：会话状态实时更新。
- 空状态：无会话时侧栏空、提示新建。
- 错误：[待补充]。
- 成功：多会话各自隔离推进。

**验收标准：**
- [ ] AC-001: Given 两个并发会话, when 会话 A 有未提交改动, then 会话 B 的工作目录看不到该改动（各在独立 worktree）。
- [ ] AC-002: Given 会话进入等待审批, when 查看侧栏, then 该会话显示 needs approval 状态指示器。
- [ ] AC-003: Given 一个 Dispatch 派生会话与一个后台会话, when 查看侧栏, then 前者有 'Dispatch' 徽章、后者标 'bg'。

---

### REQ-008: 集成终端与文件编辑器窗格

**优先级：** P0
**关联任务：** TASK-001
**关联流程：** FLOW-001

**用途：**
GUI 内需要在会话工作目录里跑命令、直接改文件，不切出去。

**行为：**
集成终端窗格（Ctrl+\`，仅本地会话）在会话工作目录打开、共享 Claude 的环境、支持多 tab；内嵌文件编辑器窗格（本地 + SSH）有 Save/Discard 与磁盘改动警告；header 路径点击复制绝对路径。

**规则：**
- MUST 终端窗格仅本地会话可用，开在会话工作目录、共享 Claude 环境。
- MUST 文件编辑器窗格提供 Save/Discard，并在文件于磁盘上被改时警告。
- MUST 文件编辑器仅本地 + SSH 会话可用。
- SHOULD 支持终端多 tab、header 路径点击复制绝对路径。

**输入：**

| 字段 | 类型 | 必填 | 校验规则 |
|---|---|---:|---|
| 终端命令 | 文本 | No | 仅本地会话 |
| 文件编辑 | 文本 | No | 本地 + SSH；Save/Discard |

**输出 / 结果：**
- 终端在工作目录执行命令。
- 编辑器保存或丢弃改动，磁盘改动时警告。

**状态：**
- 默认：窗格可从 Views 打开。
- 加载：N/A。
- 空状态：未打开文件时编辑器空。
- 错误：文件于磁盘被改 → 警告。
- 成功：命令执行 / 文件保存。

**验收标准：**
- [ ] AC-001: Given 本地会话, when 用户 Ctrl+\` 开终端, then 终端开在该会话工作目录且共享 Claude 环境。
- [ ] AC-002: Given 远程会话, when 用户尝试开集成终端, then 终端不可用（仅本地会话）。
- [ ] AC-003: Given 编辑器中文件于磁盘上被外部改动, when 用户欲保存, then 系统警告磁盘改动。

---

### REQ-009: Plan 模式

**优先级：** P1
**关联任务：** TASK-007
**关联流程：** FLOW-003

**用途：**
高风险改动前让 agent 只读探索出计划，用户批准后才改源码，避免未对齐就动一大片。

**行为：**
Claude 只用只读工具探索，在 plan 窗格（可拖拽窗格，非模态）给出计划，提供 approve/accept-edits/manual/keep-planning/refine 选项，任何源码编辑前先批计划。

**规则：**
- MUST 在 Plan 模式只用只读工具、不做任何源码编辑。
- MUST 在源码编辑前先呈现计划并等批准。
- MUST 提供 approve/accept-edits/manual/keep-planning/refine 选项。
- SHOULD 在 plan 窗格（可拖拽、非模态）呈现计划。

**输入：**

| 字段 | 类型 | 必填 | 校验规则 |
|---|---|---:|---|
| 计划决定 | 枚举（approve/accept-edits/manual/keep-planning/refine） | Yes | |

**输出 / 结果：**
- plan 窗格显示计划。
- 批准后切执行模式，否则继续规划/精修。

**状态：**
- 默认：Plan 模式下只读探索。
- 加载：计划生成中。
- 空状态：未提需求时无计划。
- 错误：Plan 模式下试图改源码 → 被只读工具集拦住。
- 成功：计划获批、切执行。

**验收标准：**
- [ ] AC-001: Given Plan 模式, when agent 探索代码库, then 它只用只读工具、不产生源码编辑。
- [ ] AC-002: Given 计划已呈现, when 用户选 approve, then agent 切到执行模式开始改源码。
- [ ] AC-003: Given 计划已呈现, when 用户选 refine 并给意见, then agent 重出计划。

---

### REQ-010: CI / PR 监控

**优先级：** P1
**关联任务：** TASK-008
**关联流程：** FLOW-005

**用途：**
PR 开后手动盯检查费力。状态栏自动轮询 CI，提供 Auto-fix 与 Auto-merge。

**行为：**
PR 开后状态栏经 GitHub CLI 轮询检查；Auto-fix 开关（Claude 读失败迭代修）；Auto-merge 开关（全绿后 squash）；CI 完成 OS 通知。

**规则：**
- MUST 在 PR 开后显示 CI 状态栏并经 GitHub CLI 轮询检查。
- MUST 提供 Auto-fix 开关，开时 Claude 读失败并迭代修。
- MUST 提供 Auto-merge 开关，开时全部检查通过后 squash 合并。
- SHOULD 在 CI 完成时触发 OS 通知。

**输入：**

| 字段 | 类型 | 必填 | 校验规则 |
|---|---|---:|---|
| Auto-fix | 布尔 | No | 默认 [待补充] |
| Auto-merge | 布尔 | No | 默认 [待补充] |

**输出 / 结果：**
- 状态栏显示检查结果。
- Auto-fix 修失败、Auto-merge 全绿后合。

**状态：**
- 默认：无 PR 时不显示状态栏。
- 加载：轮询检查中。
- 空状态：PR 无检查时状态栏空。
- 错误：Auto-fix 修不动 → 转人。
- 成功：检查全绿，Auto-merge 开则 squash 合。

**验收标准：**
- [ ] AC-001: Given PR 已开, when 检查运行, then 状态栏经 GitHub CLI 轮询并显示检查结果。
- [ ] AC-002: Given Auto-merge 开, when 全部检查通过, then PR 被 squash 合并。
- [ ] AC-003: Given Auto-fix 开, when 某检查失败, then Claude 读失败信息并迭代修。
- [ ] AC-004: Given 检查运行结束, when 完成, then 触发 OS 通知。

---

### AI 能力规格（每个 AI 功能必填）

| AI 功能 | 能力类型 | 质量条 | 触发方式 | 不确定时 | 服务降级 |
|---|---|---|---|---|---|
| Agentic 编码循环（收集上下文→动作→验证，自我纠错） | agent | 跨数十次链式工具调用直到完成；预览 autoVerify 与测试作为内建自查关 | 自动（用户给目标后 agent 自驱，受当前权限模式门禁） | 改向被当作正常输入；用户可 Esc 打断重导 | 受权限模式收敛自主度；Auto 模式 3 连/20 总拦截后暂停转询问 |
| Auto 模式分类器（逐动作安全评审） | agent（守门） | 放行白名单动作、拦截命名破坏性动作；3 连/20 总拦截自动暂停 | 自动（Auto 模式下逐动作触发，服务端独立模型） | 危险动作转人确认，不静默执行 | 分类器不可用时降级 [待补充]；暂停后回退到 Ask 风格询问 |
| "Review code" 代码自评审 | 理解 | 评审 diff 并在相关行留可执行内联评论 | 建议（用户点 "Review code" 按钮手动触发） | 评审是建议性，最终接受/拒绝由用户逐改动定 | 不可用时退回纯人工逐改动 Accept/Reject |
| 预览 autoVerify（截图/查 DOM/点元素/填表单自查） | agent（验证） | 对发现的问题迭代直到自查通过 | 自动（autoVerify 默认开，改动后触发） | 自查发现问题继续迭代；无法解决转人 | 可关 autoVerify 退回纯人工预览 |
| Extended thinking（自适应推理） | 生成（推理） | Opus 4.6/Sonnet 4.6 自适应；effort low/medium(默认)/high/xhigh/max/ultracode | 自动默认开；`/effort` 调级 | N/A（推理深度调节，非判定输出） | `MAX_THINKING_TOKENS=0` 关闭 |

**AI 护栏（绝不能做）：**
- 绝不在 Auto 模式放行命名破坏性操作：`curl|bash`、密钥外泄、生产部署/迁移、批量删除、IAM 变更、force-push/push-to-main——即便看似能加速任务。
- 绝不绕过受保护路径（.git、.gitconfig、.bashrc 等）的保护，除非用户显式启用 Bypass permissions。
- 绝不以 root/sudo 运行时启用 Bypass permissions。
- 绝不无视用户在对话里下达的会话内边界（如 "don't push"），直到用户明确解除。
- 绝不在工具输出未过 prompt-injection 探针前让其进入上下文（Auto 模式输入层）。
- 绝不声称 Rewind 能撤销 DB/API/部署等外部副作用——它只回文件 + 对话。
- 最贵的错：在生产环境执行不可逆破坏（删库、生产迁移、push-to-main、force-push）。防法：deny → ask → allow 默认拒、分类器命名拦截、PreToolUse hook 确定性兜底、3 连/20 总拦截自动暂停。
- 绝不让自主循环无限跑而不暴露循环状态：长任务必须经 tasks 窗格 / plan 窗格 / `/workflows` / CI 状态栏可见可停（针对 Ralph-Wiggum 自主循环这类极端，必须 surfacing 循环状态/迭代/完成条件，让用户能停掉失控）。

> AI 是概率性的，质量条、触发方式、护栏不钉死，开发就是在赌。

---

## 6. 数据模型

### 6.1 核心实体

| 实体 | 描述 | 关键字段 |
|---|---|---|
| Session（会话） | 一次工作的单元，独立上下文，持久化为 JSONL | session ID、状态（running/awaiting approval/finished/PR created）、环境（本地/云/SSH）、所属项目、worktree 路径、徽章（Dispatch/bg） |
| Checkpoint | 每次编辑前的文件内容快照 | 关联 session、快照前文件内容、时间点 |
| CLAUDE.md（用户记忆） | 用户写的项目/全局指令 | 作用域（managed-policy/user/project/local）、@import（至多 4 跳）、内容（建议 <200 行） |
| Auto-memory | Claude 写的记忆，AutoDream 跨会话整合 | 存于 `~/.claude/projects/<repo>/memory/MEMORY.md` |
| Permission rule | 决定动作放行/询问/拒绝的规则 | 类型（deny/ask/allow）、匹配范围（文件/目录/命令） |
| Subagent | 隔离 worker，独立上下文/prompt/工具/权限 | 类型（Explore/Plan/General-purpose/自定义）、定义（`.claude/agents/` Markdown+YAML）、返回（仅摘要） |
| MCP server | 外部工具集成 | 传输（HTTP/SSE/stdio/WebSocket）、配置域（local/project/user）、OAuth 状态、token 成本 |

### 6.2 实体关系

| 关系 | 描述 |
|---|---|
| Checkpoint belongs to Session | 每个快照属于产生它的会话，回滚是会话本地的 |
| Session runs in a Git worktree | 每个会话自动隔离在独立 worktree，并发会话不共享未提交改动 |
| Subagent spawned by Session | 子 agent 由会话派生，独立上下文、不可嵌套、只回摘要给主会话 |
| Permission rule evaluated per action | 每个动作按 deny → ask → allow 顺序匹配规则，首个匹配生效 |
| CLAUDE.md concatenated root→cwd | 多作用域 CLAUDE.md 从 root 到 cwd 拼接，@import 至多 4 跳 |
| Config shared between Desktop and CLI | CLAUDE.md/`~/.claude.json`/`.mcp.json`/hooks/skills/settings.json 共享，会话历史分开 |

### 6.3 数据规则

- 会话创建即分配独立 Git worktree（默认 `<project-root>/.claude/worktrees/`），持久化为 `~/.claude/projects/` 下 JSONL。
- 每次文件编辑前自动写 Checkpoint；回滚只动文件 + 对话，不触外部副作用。
- CLAUDE.md 由用户编写、4 作用域拼接、建议 <200 行；Auto-memory 由 Claude 编写、AutoDream 在会话间整合。
- Desktop 与 CLI 共享配置文件，但会话历史各自独立。
- 远程会话跑在 Anthropic 云 VM，应用关闭后继续；其能力受限（无 @mention、plugins、Ask、Bypass）。
- 受保护路径（.git 等）除 Bypass 外从不自动放行。

---

## 7. 外部依赖

| 编号 | 依赖 | 用途 | 是否必需 | 备注 |
|---|---|---|---:|---|
| DEP-001 | 付费 Claude 计划（Pro/Max/Team/Enterprise） | Code 标签访问授权 | Yes | 无活跃付费档返回 403 |
| DEP-002 | Git（macOS 预装；Windows 需装 Git for Windows） | worktree 隔离、版本控制 | Yes | Windows 装完需重启应用，Code 标签才工作 |
| DEP-003 | GitHub CLI | CI/PR 监控轮询检查 | Yes（CI 监控功能） | Auto-fix / Auto-merge 依赖 |
| DEP-004 | Anthropic API（Auto 模式分类器、模型推理） | Auto 模式服务端分类器 | Yes（Auto 模式） | Auto 仅 Anthropic API，不支持 Bedrock/Vertex/Foundry |
| DEP-005 | Opus 4.6+ / Sonnet 4.6 模型 | Auto 模式、自适应 extended thinking | Yes（Auto 模式与自适应推理） | |
| DEP-006 | MCP 服务（GitHub/Slack/Linear/Notion/Google Calendar 等经 Connectors） | 外部工具/连接器集成 | No | HTTP/SSE(已弃)/stdio/WebSocket 传输 |
| DEP-007 | language-server 插件 | 代码智能（类型错误、跳转定义、查引用） | No | 五类工具之一的代码智能 |
| DEP-008 | Anthropic 云 VM | 远程会话执行 | No | 远程会话应用关闭后继续，可 claude.ai/code 或 iOS 监控 |
| DEP-009 | MDM（macOS Jamf/Kandji、Windows 注册表） | 企业设备管理与 SSH 预配 | No | com.anthropic.Claude / SOFTWARE\Policies\Claude |
| DEP-010 | SSO（SAML/OIDC） | 企业身份 | No | 企业管控 |

---

## 8. 非功能需求

| 类别 | 要求 | 优先级 |
|---|---|---|
| 性能 | Auto 模式分类器逐动作评审延迟应足够低以不打断自主循环（具体阈值 [待补充]）；Tool Search 启动只载工具名、schema 延迟到近零空闲上下文成本 | P0 |
| 安全 | deny → ask → allow 默认最小权限；Auto 分类器拦截命名破坏性操作并在工具输出入上下文前过 prompt-injection 探针；受保护路径除 Bypass 外不自动放行；root/sudo 下禁 Bypass；PreToolUse hook 作确定性兜底（exit code 2 拦截） | P0 |
| 隐私 | 会话/配置存本地（`~/.claude/projects/`、`~/.claude.json` 等）；远程会话数据跑在 Anthropic 云 VM；企业可经 MDM/管理控制台管控数据边界 | P0 |
| 兼容性 | macOS（universal Intel+Apple Silicon）+ Windows（x64；ARM64 安装器）；Linux 不支持（CLI only）；窗格布局/终端/文件编辑器/view modes 需 Claude Desktop v1.2581.0+ | P0 |
| 可靠性 | 每次编辑前 checkpoint 可回滚文件+对话；Auto 模式 3 连/20 总拦截自动暂停防失控；远程会话应用关闭后继续；近上限自动压缩（先清旧工具输出再摘要），CLAUDE.md 压缩后重注入 | P0 |
| 可访问性 | 大量键盘快捷键覆盖核心操作（Cmd+/ 全部快捷键、新建/关闭/会话导航/停止/各窗格切换/模式与 effort 菜单等）；Windows 用 Ctrl 替 Cmd（除特别说明） | P1 |

---

## 9. 完成定义

MVP 完成条件：

- [ ] 所有 P0 requirements 已实现
- [ ] 所有 P0 acceptance criteria 已通过
- [ ] 所有 P0 user flows 可以端到端完成
- [ ] 主要错误状态、空状态、加载状态已处理
- [ ] Product Spec 和 Design Spec 中的 P0 内容保持一致

---

## 10. 假设与待确认问题

### 10.1 假设

| 编号 | 假设 | 假设依据 | 错误风险 |
|---|---|---|---|
| ASM-001 | Code 标签核心功能已 GA，Auto 模式/computer use/Routines/Agent teams 处 research preview | 调研日期锚定 2026 年中、2026-04-14 redesign 已发布当前窗格工作区 | 若某项实际状态变化，范围与 research-preview 标注需更新 |
| ASM-002 | Desktop 不提供 CLI 的脚本化/headless 能力，定位为交互式专属 | 调研明确 Desktop 是 interactive-only，无 `--print`/`--output-format` 等 | 若误把 CLI 脚本能力当 Desktop 需求，会做出范围外功能 |
| ASM-003 | 暗色主题为纯黑背景 + 饱和蓝用户气泡 + 亮蓝强调（GitHub issue #48158 称偏刺眼） | issue #48158 给出新旧配色对比 | 若据此钉死设计 token 而无官方 token 表，视觉可能与官方不符；具体 token 留 Design Brief [待补充] |
| ASM-004 | 远程会话能力受限（无 @mention/plugins/Ask/Bypass），与本地会话不对等 | 调研明确远程会话这些项不可用 | 若误以为远程会话能力对等，远程流程设计会错 |

### 10.2 待确认问题

| 编号 | 问题 | 是否阻塞 | 备注 |
|---|---|---:|---|
| Q-001 | 工具调用卡片折叠 vs 展开的确切视觉（边框/图标/展开三角）未公开 | No | 留 Design Brief [待补充]，不编造 |
| Q-002 | diff viewer 是 unified 还是 split/side-by-side | No | 一份第三方评测称 unified-only，官方未确认；见 REQ-003 注 |
| Q-003 | extended-thinking 块在 transcript 的渲染方式（可折叠 'thinking…' 块 vs 独立窗格 vs 内联）未公开 | No | runtime state "Thinking" 的视觉 |
| Q-004 | Code 标签是否有 light mode 或跟随 OS 偏好 | No | chat/cowork 标签有 light/dark 切换，Code 标签继承情况未知 |
| Q-005 | 是否存在独立 file-tree/project-browser 窗格，还是只有 diff viewer 内的文件列表 | No | MindStudio "file structure panel" 说法未确认，可能与 diff 文件列表混淆 |
| Q-006 | 单个审批弹窗卡片的确切视觉（布局/按钮样式/风险指示）未公开 | No | 公开截图未给 |
| Q-007 | Auto 分类器评审延迟数值、分类器不可用时的降级行为 | No | 性能与降级两处 [待补充] |
| Q-008 | CI Auto-fix / Auto-merge 开关默认值 | No | REQ-010 输入默认 [待补充] |

---

## 11. Agent 系统规格（仅 agent 产品填）

> Claude Code 是围绕 Claude 模型的 agentic harness——其代码库约 98.4% 是 harness/运营基础设施、仅约 1.6% 是 AI 决策逻辑（透明性是结构性的）。设计哲学是人类监督与透明优先于完全自主（"监督悖论"：过度依赖会让监督所需的技能退化）。

### 11.1 自主性与人在回路

| 动作类别 | 自主级别 | 审批 / 回滚 |
|---|---|---|
| 文件读取 / 搜索 | 自动 | 各模式下读自动放行，无需审批 |
| 文件编辑 | 建议确认（Ask）/ 工作目录内自动（AcceptEdits、Auto）/ 禁止（Plan） | Ask 弹 diff 逐改动 Accept/Reject；编辑前 checkpoint，可 Rewind |
| 常见文件系统命令（mkdir/touch/rm/mv 等） | Ask 弹 / AcceptEdits 工作目录内自动 / Auto 放行本地 | 受模式与分类器门禁 |
| 依赖安装 / 只读 HTTP / 推到起始分支 | Auto 模式自动放行 | 分类器放行 |
| 破坏性操作（curl\|bash、密钥外泄、生产部署/迁移、批量删除、IAM 变更、force-push/push-to-main） | 禁止（Auto 拦截）/ Bypass 才放行 | Auto 命名拦截、转人；Bypass 默认关、Settings 开、root/sudo 禁 |
| 子 agent 派生与执行 | 自动（受分类器在 spawn/执行/完成三点检查） | 只回摘要给主会话，不可嵌套 |
| 计划生成（Plan 模式） | 建议确认 | 源码编辑前先批计划 |

人在回路收口：图形分级权限模式（Ask→Plan→Auto accept edits→Auto→Bypass）设定多少动作免审批；Auto 分类器在安全动作上替代人类审批者、为破坏性动作保留命名逃生舱；Checkpoints/Rewind 给文件+对话状态撤销（不含 DB/API/部署副作用）；用户可随时 Esc 打断、改向，改向被当作正常输入而非失败（MIT 索引：随时可暂停/停止）。

### 11.2 工具与能力集

| 工具 / 能力 | 用途 | 权限级别 | 扩展机制 |
|---|---|---|---|
| 文件操作（Read/Edit/create/rename） | 读写源码 | 读自动 / 写受门禁 | 内建 |
| 搜索（pattern/regex/codebase） | 定位代码 | 读 | 内建 |
| 执行（shell/servers/tests/git） | 跑命令/服务/测试/git | 执行（受门禁） | 内建 |
| Web（search/fetch docs） | 联网查文档 | 读（只读 HTTP Auto 放行） | 内建 |
| 代码智能（编辑后类型错误、跳转定义、查引用） | 编辑后校验与导航 | 读 | language-server 插件 |
| 编排（派子 agent、问用户） | 拆活/求澄清 | 执行 | 内建 |
| MCP 服务 | 外部工具/连接器 | 视工具而定 | MCP（HTTP/SSE/stdio/WebSocket，Tool Search 默认开） |
| Skills / 自定义 slash 命令 | 复用提示与流程 | 视内容而定 | `~/.claude/skills/`（用户）/ `.claude/skills/`（项目），描述启动载、全文按需载 |
| Plugins | 打包 skills+hooks+subagents+MCP | 视内容而定 | UI 可装、命名空间命令 |
| Hooks | 确定性事件门禁 | 由脚本定 | 30 生命周期事件，PreToolUse deny/allow/ask/defer、exit code 2 拦截 |
| Computer use | 控屏/桌面应用 | 分级（View only/Click only/Full control） | research preview，仅 Pro/Max，宽影响应用额外警告 |

### 11.3 上下文与记忆

- 单任务上下文上限与超限处理：每会话一个上下文窗口，含对话/文件/输出/CLAUDE.md/auto-memory/skills/系统指令；近上限自动压缩（先清旧工具输出再摘要），`/compact` 提前手动压缩可带 focus，CLAUDE.md 压缩后重注入；`/context` 网格可视化（CLI）；usage ring 显示单会话上下文 + plan 用量。Tool Search 默认开：启动只载工具名、schema 延迟到近零空闲成本。
- 跨会话记忆：CLAUDE.md（用户写，4 作用域 managed-policy/user/project/local，root→cwd 拼接，@import 至多 4 跳，建议 <200 行）+ Auto-memory（Claude 写，存 `~/.claude/projects/<repo>/memory/MEMORY.md`，AutoDream 在会话间后台整合）。

### 11.4 编排与多 agent

- 默认单会话 agent 扛到底；可派子 agent 处理可隔离工作。子 agent 隔离（独立上下文窗口/prompt/工具/权限）、不可嵌套、只回摘要给主会话；内建 Explore（Haiku 只读）、Plan（只读）、General-purpose，自定义经 `.claude/agents/` Markdown+YAML。
- `/batch` 把一个大改动扇出成 5–30 个隔离 worktree 子 agent，各自开一个 PR。
- Agent teams（peer-to-peer 会话经共享任务表互发消息）是实验性、默认关、CLI only、token 成本更高——不在 Desktop 本版本（见 OUT-005）。

### 11.5 评估与可观测（Eval）

- 评估方式：调研未发现内建 Eval / 回归测试 harness——记为未提供而非编造（见 OUT-007）。质量自查靠预览 autoVerify 与测试执行，但这不是回归评估集。
- 可观测：transcript + 三种 view mode（Normal/Verbose/Summary）；tasks 窗格（子 agent/后台 shell/workflows）、plan 窗格、`/workflows` 进度、CI 状态栏；每会话 JSONL trace 存 `~/.claude/projects/`；hooks 30 生命周期事件提供事件级 tracing。
- 质量退化怎么发现：靠 transcript/view mode 人工观察 + hooks 事件 + JSONL trace 回溯；无自动回归基线，退化主要靠人发现（这是已知缺口）。

### 11.6 成本与预算

- 单任务成本上限与熔断：调研未发现单任务美元成本上限 / 熔断（circuit-breaker）——记为未提供（见 OUT-007）。最接近的控制是 Auto 模式 3 连/20 总拦截自动暂停（行为熔断，非成本熔断）与 usage ring 用量可视。
- 模型路由：effort 级别（low/medium 默认/high/xhigh/max/ultracode 经 `/effort`）路由推理深度；子 agent 可指定模型（如 Explore 用 Haiku 做只读探索省成本）；usage ring 显示单会话上下文 + plan 用量（跨 surface 共享）。

### 11.7 失败与卡死

- 循环 / 卡死检测与兜底：Auto 模式在 3 次连续或 20 次累计分类器拦截后自动暂停并转回询问；自主循环（Ralph-Wiggum 模式：跑到完成条件、拦截 stop、再喂）这类极端必须 surfacing 循环状态/迭代/完成条件，让用户能停掉失控——长任务经 tasks 窗格 / plan 窗格 / `/workflows` / CI 状态栏可见可停。
- 交回人的条件：危险动作被分类器拦截 → 转人确认；Auto 模式累计拦截达阈值 → 暂停转询问；Auto-fix 修不动 CI → 转人；agent 跑偏 → 用户 Esc 打断改向（改向是正常输入）。

### 11.8 会话与状态

- 中断恢复 resume：每个会话是一个带 fresh 上下文的 JSONL；`/resume` 重开同一 ID，`/branch`（`/fork`）复制历史；并行会话经 git worktree；远程会话应用关闭后继续；后台会话（`/background`）在 `/resume` picker 标 'bg'。
- 历史 / transcript：留——每会话 transcript + JSONL trace 存 `~/.claude/projects/`；Desktop 与 CLI 会话历史各自独立（配置共享、历史分开）；transcript 按 view mode 呈现给操作用户；远程会话可从 claude.ai/code 或 iOS 监控。

---
