# 设计规范：Claude Code Desktop

## 0. AI 使用说明

- 本文档定义产品的界面结构、视觉系统、组件、状态和交互规则。
- Product Spec（`product-spec-builder/examples/claude-code-desktop.md`）是功能范围的事实来源；本文档不得新增 Product Spec 没有定义的核心功能。
- 本产品是**叠加形态**：桌面端界面型 + 对话式 Agent。两轨都填——界面型走 §2 信息架构、§3 页面规格、§5 视觉系统、§8.3 设备响应式；Agent 维度走 §A。§A.5 终端渲染相关项（渲染基元 / ANSI 色彩深度 / 列宽 degrade）标 N/A，因为这是桌面 GUI 不是终端，但多 surface 一致性照填。
- AI MUST 优先设计 P0 flows 和 P0 screens，与 Spec 的 P0 对齐。
- AI MUST 覆盖默认、加载、空、错误、成功等关键状态。
- AI MUST 保持页面、组件、交互和视觉 tokens 的一致性。
- 大量精确视觉值在公开来源不可考（见 §10.2），这些标 [待补充] 交设计工具裁决，绝不编造。

---

## 1. 设计方向

### 1.0 产品形态

| 维度 | 方向 |
|---|---|
| 形态 | **对话式 Agent + 桌面端界面型（叠加）**。本体是一个对话式编码 agent，外壳是一个原生桌面图形应用（Claude Desktop 窗口的 Code 标签） |
| 本体 | 界面型走页面与组件：窗口外壳、会话侧栏、8 个可拖拽窗格（chat/diff/preview/terminal/file/plan/tasks/subagent）、macOS/Windows 窗口边框。Agent 走会话流、交互范式与运行态呈现：消息轮、工具调用卡、代码 diff、计划/待办、思考、运行态、授权 prompt |
| 落地分轨 | 界面型用 §2 §3 §5 §8.3；对话式 Agent 用 §A。§A.5 终端渲染项标 N/A，多 surface 一致性照填；§5 不标 N/A——桌面 GUI 有真实 CSS 视觉，但具体 token 多为 [待补充] |

### 1.1 设计目标

让开发者在一个桌面窗口里**看见、引导、信任**一个自主编码 agent——把 Claude Code 引擎的全部能力装进可视化的多窗格工作区，diff 可点、产物可预览、并行会话可排开、危险动作被拦住，使监督本身不再有终端摩擦或审批疲劳。设计要服务于一个核心张力：agent 越自主，监督的接口越要清晰可信，否则自主就是失控。

### 1.2 产品气质

| 维度 | 方向 |
|---|---|
| 语气 | 专业、工具型、克制。这是给开发者整天盯着的生产力工具，不是 demo，不卖萌不炫技 |
| 信息密度 | 中等偏紧凑。多窗格并排意味着每块面积都珍贵，但 transcript 与 diff 要留出可读的呼吸感，避免代码挤成一团 |
| 视觉风格 | 工具型 IDE × 现代深色 SaaS。接近 VS Code / Linear 的专业暗色调，但比传统 IDE 更收敛、更聚焦于"会话 + 改动"两件事 |
| 情绪目标 | 掌控感与信任感。用户应感到"我看得见 agent 在干什么、我随时能叫停、危险的它不会偷偷做"，而不是"它在黑箱里跑我只能祈祷" |

### 1.3 设计原则

- DP-001: **透明优先于自主**。agent 的每个动作、每次工具调用、每处改动都要可见可追溯（受 view mode 调节详略），透明性是结构性的，不是事后补的日志。对应 Spec §11 的"监督悖论"立场。
- DP-002: **危险动作必须有视觉摩擦，安全动作必须无摩擦**。放行安全的、拦住危险的——这条权限哲学要落到视觉：破坏性操作（删库、push-to-main、Bypass）要有显式的红色/警告语义和确认步骤；读操作和工作目录内编辑在 AcceptEdits/Auto 下要悄无声息。对应 REQ-001 / REQ-002。
- DP-003: **会话是第一公民，窗格是可调的取景器**。一切围绕"会话"组织：侧栏管会话、状态指示器反映会话、worktree 隔离会话。8 个窗格只是观察同一个会话的不同镜头，用户自由拖拽组合，布局不强加。对应 REQ-006 / REQ-007。
- DP-004: **可中断、可回滚是一等交互，不是隐藏功能**。Esc 打断、Esc-Esc/Rewind 回滚要随手可达、状态清晰；改向被当作正常输入而非错误，不能让用户觉得"打断 agent 是在搞破坏"。对应 REQ-005 / FLOW-001。
- DP-005: **深色为本**。当前形态是深色工作区（纯黑背景 + 蓝色强调）。light mode 是否存在未确认（见 DQ-004），深色是设计基准。

### 1.4 参考与反参考

**正向参考：**

| 参考产品 | 具体喜欢哪一点（拆成属性） | 必须保留 |
|---|---|---|
| VS Code | 多面板可拖拽缩放的工作区骨架；深色工具型质感；点路径跳转的导航直觉 | Yes |
| Linear | 克制的深色 SaaS 美学；状态指示器的语义清晰；键盘优先、快捷键覆盖核心操作 | Yes |
| GitHub PR review | diff 的文件列表 + 逐文件改动结构；点行留内联评论的审查心智模型 | Yes（对应 REQ-003） |
| 终端 CLI（同源引擎） | 会话即工作单元、transcript 即真相、agent 自驱的本体心智——只是换上图形外壳 | Yes |

**反参考：**
- **过度拟物 / 装饰性 IDE 皮肤**：渐变、阴影堆砌、拟物按钮——工具型产品里这些是噪声，抢走对代码和 diff 的注意力。
- **黑箱式 AI 助手（只给结论不给过程）**：把 agent 的动作藏起来、只甩一个"已完成"——直接违背 DP-001，用户失去监督抓手。
- **每次都问到底的朴素权限弹窗**：实测用户批准 ~93% 弹窗（Spec §1.2），机械确认侵蚀真实监督。反参考它，正是 Auto 模式分级门禁要解决的（REQ-001/002）。

> 「像 VS Code」是症状不是规格——真正 load-bearing 的是"多窗格可拖拽 + 深色工具质感 + 点路径跳转"这三条属性，不是它的全部。反参考里的"黑箱 AI"与"朴素权限弹窗"是这个产品存在的理由，当边界 fence 死守。

---

## 2. 信息架构

> 界面型轨。覆盖桌面 GUI 外壳、会话侧栏、8 个窗格的结构与关系。

### 2.1 导航结构

三层结构：

1. **应用顶层**：Claude Desktop 窗口顶部中央三标签（Chat / Cowork / Code）。Code 是本产品的 agentic 编码 surface，本文档只规格 Code 标签内部。
2. **会话层（持久左侧侧栏）**：Code 标签内一条常驻左侧栏，是导航主轴——列活跃 + 近期归档会话，"+ New session"（Cmd/Ctrl+N），按状态/项目/环境过滤，按项目分组，Ctrl+Tab 循环切换。会话是导航的第一公民。
3. **窗格层（可拖拽工作区）**：选中一个会话后，右侧是多窗格工作区，8 个命名窗格从 Views 菜单打开、拖头部移动、边缘缩放。Cmd/Ctrl-click 第二个会话开 split-view 左右排开。

驱动方式叠加：自然语言（底部 chat 输入）为主，60+ slash 命令、`@` 文件补全、大量键盘快捷键为辅。不是传统的菜单树导航，而是"会话侧栏 + 自由窗格 + 命令/快捷键"的工具型导航。

### 2.2 页面清单

> 桌面 GUI 没有传统"页面"，这里的 SCREEN 指**会话工作区的视图与窗格组合**。SCREEN-001 是承载一切的工作区外壳，SCREEN-002~009 是各命名窗格，SCREEN-010 是设置。

| 页面编号 | 页面名称 | 页面目的 | 关联流程 / 需求 | 优先级 |
|---|---|---|---|---|
| SCREEN-001 | 工作区外壳（侧栏 + 窗格容器） | 承载会话侧栏与多窗格工作区，是一切的骨架 | FLOW-001 / FLOW-002 / REQ-006 / REQ-007 | P0 |
| SCREEN-002 | Chat 窗格（transcript + prompt 区） | agent 对话主场：流式 transcript、底部 prompt 输入、view mode 控制 | FLOW-001 / REQ-006 | P0 |
| SCREEN-003 | Diff 窗格 | 可视化审查改动：文件列表 + 逐文件 diff、内联评论、逐改动 Accept/Reject | FLOW-001 / REQ-003 | P0 |
| SCREEN-004 | Preview 窗格 | 内嵌预览产物并让 agent autoVerify 自查 | FLOW-001 / REQ-004 | P0 |
| SCREEN-005 | Terminal 窗格 | 在会话工作目录跑命令（仅本地会话） | FLOW-001 / REQ-008 | P0 |
| SCREEN-006 | File（editor）窗格 | 内嵌改文件：Save/Discard、磁盘改动警告（本地 + SSH） | FLOW-001 / REQ-008 | P0 |
| SCREEN-007 | Plan 窗格 | 呈现 Plan 模式计划，提供 approve/refine 等选项（可拖拽窗格，非模态） | FLOW-003 / REQ-009 | P1 |
| SCREEN-008 | Tasks 窗格 | 列后台工作：子 agent、后台 shell、动态 workflows | FLOW-002 后台分支 / SCOPE-019（REQ-006 的 P1 后台镜头） | P1 |
| SCREEN-009 | Subagent 窗格 | 展示单个后台 worker 的输出 | FLOW-002 后台分支 / SCOPE-019（REQ-006 的 P1 后台镜头） | P1 |
| SCREEN-010 | Settings → Claude Code | 开关 Bypass permissions、企业管控等设置 | REQ-001 | P1 |

### 2.3 页面关系

- **侧栏 ↔ 工作区**：侧栏选会话，工作区切到该会话的窗格组合。会话是主路径的轴心，所有窗格都是"看这个会话"。
- **chat 窗格是中心**：transcript 里点击文件路径 → 在 file/preview 窗格打开；右键路径 → Attach as context / Open in（VS Code, Cursor, Zed）/ Show in Finder-Explorer / Copy path。chat 是其他窗格内容的来源。
- **diff ↔ chat**：agent 在 chat 里产生改动，diff 窗格审查；diff 里留的内联评论回流到 chat 让 agent 改向。
- **plan → 执行**：Plan 模式下 plan 窗格出计划，approve 后 agent 切执行模式，改动出现在 diff 窗格（FLOW-003）。
- **tasks → subagent**：tasks 窗格点某个后台 worker → subagent 窗格看其输出。
- **CI 状态栏**：PR 开后在会话上方/相关位置出现（非独立窗格），是 FLOW-005 的载体，无 PR 时不显示。
- **split-view**：Cmd/Ctrl-click 第二会话，左右排开两个会话各自的工作区（FLOW-002）；Cmd/Ctrl+\ 关闭聚焦窗格而非整个会话。
- **主路径**：SCREEN-001 → SCREEN-002（提需求）→ SCREEN-003（审 diff）→ 审批落盘。辅助路径：preview/terminal/file/plan/tasks/subagent 按需打开。

---

## 3. 页面规格

> 每个有设计决策空间的窗格写布局 / 内容层级 / 用的组件 / 六态 / 响应式。窗格视觉的精确 token 多为 [待补充]。

### SCREEN-001: 工作区外壳（侧栏 + 窗格容器）

**页面目的：**
承载会话侧栏与多窗格工作区，是用户进入 Code 标签后看到的一切骨架。对应 REQ-006 / REQ-007。

**主要操作：**
新建会话（Cmd/Ctrl+N）、在会话间切换（Ctrl+Tab）、打开/拖拽/缩放窗格（Views 菜单）。

**次要操作：**
- 按状态/项目/环境过滤会话、按项目分组
- Cmd/Ctrl-click 第二会话开 split-view
- Cmd/Ctrl+\ 关闭聚焦窗格

**布局结构：**
1. 顶部区域：应用三标签（Chat / Cowork / Code）；macOS/Windows 原生窗口边框
2. 左侧固定：持久会话侧栏（活跃 + 归档会话列表、"+ New session"、过滤器、状态指示器、Dispatch/bg 徽章）
3. 主内容区：多窗格工作区——默认 chat 窗格占主，其余窗格按需从 Views 打开，自由拖拽缩放排布
4. 底部 / 固定操作区：chat 窗格内底部的 prompt 输入区（见 SCREEN-002）

**内容层级：**

| 优先级 | 元素 | 说明 |
|---|---|---|
| 1 | 当前会话的 transcript + prompt 区 | 用户主要在这里和 agent 交互 |
| 2 | 会话侧栏与状态指示器 | 多会话管理、随时知道哪个会话在等审批/跑完 |
| 3 | 其余窗格（diff/preview/terminal 等） | 按需调出的观察镜头 |

**使用的组件：**

| 组件编号 | 组件名称 | 用途 |
|---|---|---|
| CMP-001 | 会话条目（Session Item） | 侧栏里一个会话的表示，带状态指示器与徽章 |
| CMP-002 | 状态指示器（Status Indicator） | running / needs approval / finished / PR created |
| CMP-003 | 窗格容器（Pane） | 可拖拽缩放的命名窗格框 |
| CMP-009 | usage ring | 单会话上下文 + plan 用量环 |

**必需状态：**
- 默认：侧栏列当前会话，主区为 chat 窗格 + Normal view mode。
- 加载：会话状态实时更新；流式 transcript token-by-token 渲染。
- 空状态：无会话时侧栏空，提示"+ New session"新建。
- 错误：低于 Claude Desktop v1.2581.0 时窗格布局/终端/文件编辑器/view modes 不可用；无活跃付费档 Code 标签返回 403（FLOW-001 边界）。
- 成功：窗格按用户拖拽的布局排列，会话各自隔离推进。
- 禁用 / 权限受限：远程会话不提供集成终端、@mention、plugins、Ask、Bypass（相关入口置灰或不显示）。

**响应式表现：**
- 移动端：N/A——Desktop 仅 macOS + Windows 桌面（见 §8.3 与 OUT-001）。
- 平板端：N/A。
- 桌面端：窗口可缩放，窗格随窗口尺寸重排；窄窗口下建议自动收起部分窗格（具体折叠策略 [待补充]）。

**可访问性说明：**
- 核心操作全部有键盘快捷键（Cmd+/ 列出全部）；侧栏会话可键盘导航（Ctrl+Tab / Ctrl+Shift+Tab）。
- 状态指示器不能只靠颜色——running/needs approval/finished/PR created 需配文字或图标（见 §8.2）。

---

### SCREEN-002: Chat 窗格（transcript + prompt 区）

**页面目的：**
agent 对话主场，承载 FLOW-001 的整轮交互：用户提需求 → agent 收集上下文 → 提动作 → 审批 → 验证。对应 REQ-006，是 §A 各呈现单元与运行态的容器。

**主要操作：**
在底部 prompt 区输入自然语言需求并发送。

**次要操作：**
- 切 view mode（Normal/Verbose/Summary，Ctrl+O 循环或下拉）
- `@` 触发文件自动补全（本地/SSH）、拖拽文件/图片进输入区
- `+` 调出文件附件 / skills/slash 命令 / connectors / plugins
- 切模型（Cmd+Shift+I）、切权限模式（Cmd+Shift+M）、切 effort（Cmd+Shift+E）、切环境
- Esc 打断 agent、Esc-Esc 或 `/rewind` 回滚

**布局结构：**
1. 顶部区域：会话 header——会话名、diff-stats 指示器（如 `+12 -1`）、模型/模式/环境信息
2. 主内容区：流式 transcript——消息轮、工具调用卡、代码块/diff 摘要、思考块、运行态（详见 §A.2 / §A.3）
3. 辅助区域：CI 状态栏（PR 开后才出现）
4. 底部 / 固定操作区：prompt 输入区——文本框、发送、`+`、模型下拉、权限-模式选择器、环境下拉、Transcript-view 下拉、usage ring

**内容层级：**

| 优先级 | 元素 | 说明 |
|---|---|---|
| 1 | prompt 输入区 + 最新 agent 回复 | 用户当下在做的事 |
| 2 | transcript 历史（消息轮、工具调用、diff 摘要） | agent 干了什么的完整记录，受 view mode 调详略 |
| 3 | 权限-模式选择器 + usage ring | 随时知道当前自主级别与上下文用量 |

**使用的组件：**

| 组件编号 | 组件名称 | 用途 |
|---|---|---|
| CMP-004 | 消息轮（Message Turn） | 用户消息气泡 / agent 流式回复 |
| CMP-005 | 工具调用卡（Tool-Call Card） | 一次工具调用的呈现，受 view mode 折叠/展开 |
| CMP-006 | 代码 / diff 块 | transcript 内的代码与 diff 摘要 |
| CMP-007 | 授权 prompt 卡（Approval Card） | Ask 模式逐改动 Accept/Reject、computer use 分级授权 |
| CMP-008 | 权限-模式选择器 | 切 Ask/AcceptEdits/Plan/Auto/Bypass |
| CMP-009 | usage ring | 上下文 + plan 用量 |

**必需状态：**
- 默认：Normal view mode，transcript 展示当前会话，prompt 区可输入。
- 加载：流式输出中（token-by-token）、思考中、跑工具中——详见 §A.3。
- 空状态：新会话 transcript 空，prompt 区有 placeholder 引导提需求。
- 错误：错误 / 堆栈在 transcript 渲染（与正常输出的视觉区分 [待补充]，见 DQ-003/Q）；agent 跑偏由用户 Esc 打断改向。
- 成功：transcript 出最终结果，会话状态转 finished。
- 禁用 / 权限受限：远程会话下 `@` 补全、plugins、Ask/Bypass 模式不可用。

**响应式表现：**
- 桌面端：transcript 随窗格宽度回流；窄窗格下代码块可横向滚动而非挤压。移动/平板 N/A。

**可访问性说明：**
- 流式输出对屏幕阅读器：新增内容应可被感知（aria-live 区域，具体实现 [待补充]）。
- prompt 输入框有明确可聚焦态；Esc 打断要有键盘可达性。

---

### SCREEN-003: Diff 窗格

**页面目的：**
可视化逐改动审查 agent 产生的 diff，是 FLOW-001 第 4 步与 REQ-003 的载体。

**主要操作：**
审查改动，在 Ask 模式下逐改动 Accept / Reject。

**次要操作：**
- 点击某行 → 开内联评论框（Cmd/Ctrl+Enter 提交多行）
- 点 "Review code" 让 Claude 评审 diff 并留内联评论
- Cmd+Shift+D 打开 diff 窗格

**布局结构：**
1. 顶部区域：窗格头 + "Review code" 按钮
2. 主内容区：左列文件列表，右侧逐文件改动
3. 辅助区域：内联评论框（点行展开）
4. 底部 / 固定操作区：Ask 模式下逐改动 Accept/Reject 控件

**内容层级：**

| 优先级 | 元素 | 说明 |
|---|---|---|
| 1 | 逐文件 diff 改动 | 用户审查的主体 |
| 2 | 文件列表 + Accept/Reject | 在多文件间切换、决定每处取舍 |
| 3 | 内联评论 + "Review code" | 留意见让 agent 改向、或让 Claude 自评审 |

**使用的组件：**

| 组件编号 | 组件名称 | 用途 |
|---|---|---|
| CMP-006 | 代码 / diff 块 | diff 的着色渲染 |
| CMP-010 | 内联评论框（Inline Comment Box） | 点行留多行评论 |
| CMP-011 | "Review code" 按钮 | 触发 Claude 自评审 |

**必需状态：**
- 默认：diff 窗格展示当前会话改动（unified 还是 split 未确认，见 DQ-002/Q-002）。
- 加载：diff 渲染加载态 [待补充]（Spec REQ-003 状态未给）。
- 空状态：无改动时 diff 窗格为空，提示"暂无改动"。
- 错误：[待补充]（Spec 未给）。
- 成功：用户接受的改动落盘、拒绝的不落盘；Claude 评审的内联评论显示在对应行。
- 禁用 / 权限受限：非 Ask 模式下逐改动 Accept/Reject 不出现（AcceptEdits/Auto 自动放行）。

**响应式表现：**
- 桌面端：窄窗格下文件列表可折叠为下拉，diff 区横向滚动。移动/平板 N/A。

**可访问性说明：**
- diff 的增/删不能只靠红绿色——需配 `+`/`-` 前缀或图标（见 §8.2）。
- 内联评论框可键盘聚焦，Cmd/Ctrl+Enter 提交。

---

### SCREEN-004: Preview 窗格

**页面目的：**
内嵌预览跑起来的产物并让 agent autoVerify 自查，对应 FLOW-001 第 5 步与 REQ-004。

**主要操作：**
启动/停止本地 dev server 并预览产物（HTML/PDF/图片/视频）。

**次要操作：**
- Cmd+Shift+S 在预览里选元素
- 切 "Persist sessions" 开关
- Cmd+Shift+P 打开预览窗格

**布局结构：**
1. 顶部区域：start/stop 控件、"Persist sessions" 开关、地址/目标栏
2. 主内容区：内嵌浏览器预览区
3. 辅助区域：选元素提示（Cmd+Shift+S 激活时）

**内容层级：**

| 优先级 | 元素 | 说明 |
|---|---|---|
| 1 | 预览产物渲染区 | 看产物长什么样 |
| 2 | start/stop + autoVerify 状态 | 控制 server 与 agent 自查 |

**使用的组件：**

| 组件编号 | 组件名称 | 用途 |
|---|---|---|
| CMP-003 | 窗格容器 | 承载预览 |
| CMP-012 | 开关（Toggle） | autoVerify / Persist sessions |

**必需状态：**
- 默认：autoVerify 开，预览窗格可跑 server（由 `.claude/launch.json` 配置）。
- 加载：dev server 启动中。
- 空状态：无配置/未启动时预览窗格空，提示配置 `.claude/launch.json`。
- 错误：[待补充]（server 启动失败态视觉，Spec REQ-004 未给）。
- 成功：产物正确渲染，autoVerify 开时 agent 截图查 DOM 自查并对发现的问题迭代。
- 禁用 / 权限受限：autoVerify 关时退回纯人工预览（Spec AI 能力规格的服务降级）。

**响应式表现：**
- 桌面端：预览区随窗格缩放；可模拟不同视口（[待补充] 是否提供视口预设）。移动/平板 N/A。

**可访问性说明：**
- 选元素（Cmd+Shift+S）应有键盘可达入口与清晰的选中反馈。

---

### SCREEN-005: Terminal 窗格

**页面目的：**
在会话工作目录跑命令，共享 Claude 环境，对应 REQ-008。仅本地会话。

**主要操作：**
输入并执行 shell 命令。

**次要操作：**
- 开多个终端 tab
- Ctrl+\` 打开终端窗格

**布局结构：**
1. 顶部区域：终端 tab 栏
2. 主内容区：终端输出/输入区（开在会话工作目录）

**内容层级：**

| 优先级 | 元素 | 说明 |
|---|---|---|
| 1 | 终端输入/输出 | 跑命令看结果 |
| 2 | 多 tab 切换 | 并行多个终端会话 |

**使用的组件：**

| 组件编号 | 组件名称 | 用途 |
|---|---|---|
| CMP-003 | 窗格容器 | 承载终端 |

**必需状态：**
- 默认：终端可从 Views 打开（仅本地会话）。
- 加载：N/A。
- 空状态：新终端等待输入。
- 错误：远程会话下终端不可用（Spec REQ-008 AC-002）。
- 成功：命令在工作目录执行并返回输出。
- 禁用 / 权限受限：远程会话不显示集成终端入口。

**响应式表现：**
- 桌面端：等宽渲染，随窗格缩放列宽自适应。移动/平板 N/A。

**可访问性说明：**
- 终端输出对屏幕阅读器可访问；颜色之外保留文本语义（终端通常自带）。

---

### SCREEN-006: File（editor）窗格

**页面目的：**
内嵌改文件，Save/Discard，磁盘改动警告，对应 REQ-008。本地 + SSH 会话。

**主要操作：**
编辑文件并 Save 或 Discard。

**次要操作：**
- header 路径点击复制绝对路径
- 由 chat/diff 里点文件路径打开

**布局结构：**
1. 顶部区域：文件 header（路径可点击复制）、Save/Discard 按钮
2. 主内容区：代码编辑区

**内容层级：**

| 优先级 | 元素 | 说明 |
|---|---|---|
| 1 | 代码编辑区 | 改文件 |
| 2 | Save/Discard + 磁盘改动警告 | 保存或丢弃，防覆盖磁盘上的外部改动 |

**使用的组件：**

| 组件编号 | 组件名称 | 用途 |
|---|---|---|
| CMP-006 | 代码 / diff 块 | 代码高亮 |
| CMP-013 | 警告条（Warning Banner） | 磁盘改动警告 |

**必需状态：**
- 默认：窗格可从 Views 打开（本地 + SSH）。
- 加载：N/A。
- 空状态：未打开文件时编辑器空。
- 错误：文件于磁盘被外部改动 → 警告（Spec REQ-008 AC-003）。
- 成功：文件保存或改动被丢弃。
- 禁用 / 权限受限：非本地/SSH 会话不可用。

**响应式表现：**
- 桌面端：编辑区随窗格缩放，长行横向滚动。移动/平板 N/A。

**可访问性说明：**
- 编辑区键盘可达；磁盘改动警告不能只靠颜色，需文字说明（见 §8.2）。

---

### SCREEN-007: Plan 窗格

**页面目的：**
呈现 Plan 模式计划并提供决定选项，对应 FLOW-003 / REQ-009。可拖拽窗格，非模态。

**主要操作：**
对计划做决定：approve / accept-edits / manual / keep-planning / refine。

**次要操作：**
- refine 时给修改意见
- Cmd+Shift+M 切到 Plan mode

**布局结构：**
1. 顶部区域：窗格头（标"Plan"）
2. 主内容区：Claude 的计划内容（步骤/方案）
3. 底部 / 固定操作区：approve/accept-edits/manual/keep-planning/refine 选项

**内容层级：**

| 优先级 | 元素 | 说明 |
|---|---|---|
| 1 | 计划内容 | 用户要看懂 agent 打算怎么做 |
| 2 | 决定选项 | approve 才放行执行 |

**使用的组件：**

| 组件编号 | 组件名称 | 用途 |
|---|---|---|
| CMP-003 | 窗格容器 | 承载计划（非模态） |
| CMP-014 | 计划决定控件（Plan Decision） | approve/refine 等五选项 |

**必需状态：**
- 默认：Plan 模式下只读探索。
- 加载：计划生成中。
- 空状态：未提需求时无计划。
- 错误：Plan 模式下 agent 试图改源码 → 被只读工具集拦住（Spec REQ-009 AC-001）。
- 成功：计划获批，agent 切执行模式。
- 禁用 / 权限受限：非 Plan 模式下窗格无内容/不强调。

**响应式表现：**
- 桌面端：可拖拽缩放的普通窗格。移动/平板 N/A。

**可访问性说明：**
- 五个决定选项键盘可达、焦点清晰；计划内容结构化（标题/列表）便于阅读。

---

### SCREEN-008: Tasks 窗格 & SCREEN-009: Subagent 窗格

**页面目的：**
Tasks 窗格列后台工作（子 agent / 后台 shell / 动态 workflows）；Subagent 窗格展示单个后台 worker 输出。对应 FLOW-002 分支与 REQ-006、Spec §11.7 长任务可见可停。

**主要操作：**
查看后台工作进度，点某项 → 在 subagent 窗格看输出或停止它。

**布局结构：**
1. Tasks 窗格：后台工作列表（每项带状态、可点击/可停止）
2. Subagent 窗格：选中 worker 的输出流

**内容层级：**

| 优先级 | 元素 | 说明 |
|---|---|---|
| 1 | 后台工作列表 + 状态 | 长任务可见可停（防 Ralph-Wiggum 失控） |
| 2 | 单 worker 输出 | 深入看某个后台 worker 在干什么 |

**使用的组件：**

| 组件编号 | 组件名称 | 用途 |
|---|---|---|
| CMP-002 | 状态指示器 | 后台工作的运行/完成态 |
| CMP-003 | 窗格容器 | 承载列表与输出 |

**必需状态：**
- 默认：列当前会话的后台工作（子 agent/shell/workflows）。
- 加载：后台工作进行中，进度实时更新。
- 空状态：无后台工作时列表空。
- 错误：后台工作失败 → 状态反映、可查输出定位。
- 成功：后台工作完成，状态转 finished。

**响应式表现：**
- 桌面端：列表与输出窗格各自可缩放。移动/平板 N/A。

**可访问性说明：**
- 后台工作状态不只靠颜色；可停止控件键盘可达（对应"长任务必须可停"护栏）。

---

### SCREEN-010: Settings → Claude Code

**页面目的：**
开关 Bypass permissions 等设置；企业管控入口。对应 REQ-001（Bypass 默认关、Settings 开）。

**主要操作：**
启用/禁用 Bypass permissions（默认关）。

**布局结构：**
1. 设置项列表，含 Bypass permissions 开关及其风险说明
2. 企业管控相关设置（受管理控制台/MDM 约束时置灰并提示）

**必需状态：**
- 默认：Bypass permissions 关。
- 错误：以 root/sudo 运行时试开 Bypass → 被拦并提示（Spec REQ-001 AC-004）。
- 成功：用户显式开启 Bypass 后生效。
- 禁用 / 权限受限：企业管理员禁用 Bypass 时开关置灰并说明被组织策略锁定。

**可访问性说明：**
- Bypass 这类高危开关需有明确风险文案与二次确认，不能只靠颜色暗示危险（见 §8.2、DP-002）。

---

## 4. 组件规格

> 桌面 GUI 组件套用变体 + 七态 + 内容规则 + 可访问性。对话式 Agent 的可复用单元（授权 prompt、slash 命令、流式呈现块）状态以 §A.3 运行态为准。组件视觉的精确 token 多为 [待补充]。

### CMP-002: 状态指示器（Status Indicator）

**组件用途：**
让用户在侧栏一眼看出每个会话的状态，是多会话管理的核心信号。对应 REQ-007。

**使用位置：**
SCREEN-001（侧栏会话条目）、SCREEN-008（后台工作）。

**变体：**

| 变体 | 用途 |
|---|---|
| running | 会话正在跑 |
| needs approval | 等待用户审批（Ask 模式弹审批 / Auto 暂停） |
| finished | 会话完成 |
| PR created | 已开出 PR |

**状态：**

| 状态 | 视觉表现 | 交互表现 |
|---|---|---|
| 默认 | 状态点/标签，颜色 + 文字/图标双编码 [具体色 待补充] | 点会话条目进入该会话 |
| 悬停 | 轻微高亮，可显示状态全称 tooltip | 显示更多信息 |
| 聚焦 | 清晰焦点环 | 键盘可达 |
| 激活 | 当前选中会话高亮 | 显示在工作区 |
| 禁用 | 归档会话弱化 | 仍可点开查看历史 |
| 加载 | running 态可带动态指示（脉冲/旋转，克制） | 不可强行结束（用 Esc 在会话内停） |
| 错误 | 失败态用 danger 语义 + 文字说明 | 点开看错误详情 |

**内容规则：**
- 状态文案用动词/状态名：running / needs approval / finished / PR created；不用模糊词。
- Dispatch 派生会话标 'Dispatch' 徽章、后台会话标 'bg'（Spec REQ-007）。

**可访问性要求：**
- 不只靠颜色——每个状态配文字或形状不同的图标（§8.2）。
- 状态变化（如转 needs approval）对屏幕阅读器可感知。

---

### CMP-004: 消息轮（Message Turn）

**组件用途：**
transcript 里一轮对话的呈现：用户消息或 agent 回复。是 §A.2 各呈现单元的容器单位。

**使用位置：**
SCREEN-002（chat 窗格 transcript）。

**变体：**

| 变体 | 用途 |
|---|---|
| 用户消息 | 用户输入的 prompt（可含拖拽文件/图片、@mention） |
| Agent 回复 | agent 流式生成的回复，markdown 渲染 |
| 系统/状态 | 运行态提示（思考中、被中断等，见 §A.3） |

**状态：**

| 状态 | 视觉表现 | 交互表现 |
|---|---|---|
| 默认 | 用户消息用饱和蓝气泡 [待补充 精确色]；agent 回复无气泡或弱背景，markdown 渲染 | 可选中复制 |
| 悬停 | 可显露操作（复制/引用，[待补充] 是否提供） | — |
| 聚焦 | 键盘可聚焦到消息块 | 屏幕阅读器可逐轮朗读 |
| 激活 | — | — |
| 禁用 | — | — |
| 加载 | agent 回复流式 token-by-token 渲染（见 §A.3 流式态） | 可 Esc 打断 |
| 错误 | 错误/堆栈在轮内渲染，与正常输出区分 [待补充 视觉差异]（DQ-003） | 可据错误改向 |

**内容规则：**
- 用户消息原样呈现；agent 回复 markdown 渲染（代码块、列表、标题）。
- 气泡/背景区分发起方，但不过度拟物。

**可访问性要求：**
- 区分发起方不只靠颜色（位置/标签辅助）。
- 流式新增内容 aria-live 可感知（[待补充] 实现）。

---

### CMP-005: 工具调用卡（Tool-Call Card）

**组件用途：**
呈现 agent 的一次工具调用（读文件、搜索、跑命令、调 MCP 等），是透明性（DP-001）的核心载体。详略受 view mode 调节。对应 REQ-006。

**使用位置：**
SCREEN-002（chat 窗格 transcript）。

**变体：**

| 变体 | 用途 |
|---|---|
| Normal（折叠摘要） | 折叠成一行摘要，默认 |
| Verbose（展开全步） | 每次调用/读取/步骤全展开 |
| Summary（仅结果） | 只显示最终响应 + 改动，不显示每步工具调用 |

**状态：**

| 状态 | 视觉表现 | 交互表现 |
|---|---|---|
| 默认 | 按 view mode 呈现；折叠态显示工具名 + 摘要 [待补充：边框/图标/展开三角的精确视觉，DQ-001/Q-001] | 点击展开/折叠（如有 disclosure） |
| 悬停 | 轻微高亮 | 提示可展开 |
| 聚焦 | 焦点环 | 键盘可展开 |
| 激活 | 展开态显示完整调用细节 | — |
| 禁用 | — | — |
| 加载 | "跑工具中"——显示正在跑什么工具（见 §A.3） | 可 Esc 打断 |
| 错误 | 工具失败/错误输出，与正常区分 [待补充] | 可据失败改向 |

**内容规则：**
- 工具调用必须可见（受 view mode 详略控制），不能完全隐藏——这是 DP-001 的硬约束。
- 显示工具名让用户知道 agent 在用什么能力。

**可访问性要求：**
- 折叠/展开有键盘可达的 disclosure 语义。
- 展开态结构清晰，屏幕阅读器可读出工具名与参数。

> 注：折叠 vs 展开的确切视觉（边框/图标/展开三角）公开来源未给，标 [待补充]，见 §10.2 DQ-001。

---

### CMP-006: 代码 / diff 块（Code / Diff Block）

**组件用途：**
transcript、diff 窗格、file 编辑器里的代码与 diff 渲染，是开发者最高频盯着的内容。

**使用位置：**
SCREEN-002、SCREEN-003、SCREEN-006。

**变体：**

| 变体 | 用途 |
|---|---|
| 代码块 | 语法高亮的代码片段 |
| Diff（增删着色） | 行级 diff，增/删分色 + `+`/`-` 前缀 |
| 内联代码 | 句中的等宽代码 |

**状态：**

| 状态 | 视觉表现 | 交互表现 |
|---|---|---|
| 默认 | 等宽字体、语法高亮；diff 增行绿底/删行红底 + 前缀符号 [待补充 精确色] | diff 行可点开内联评论（SCREEN-003） |
| 悬停 | diff 行悬停高亮，提示可评论 | — |
| 聚焦 | 行可键盘聚焦 | Cmd/Ctrl+Enter 提交评论 |
| 激活 | 选中行高亮 | — |
| 禁用 | — | — |
| 加载 | [待补充]（diff 渲染加载态，Spec REQ-003 未给） | — |
| 错误 | [待补充] | — |

**内容规则：**
- diff 增删不只靠红绿——必须有 `+`/`-` 前缀或图标（§8.2、色盲友好）。
- 等宽字体保证代码对齐。

**可访问性要求：**
- 增删用符号 + 颜色双编码。
- 长代码横向滚动而非挤压，行可键盘导航。

---

### CMP-007: 授权 prompt 卡（Approval Card）

**组件用途：**
让用户审批/拒绝 agent 的动作，是 DP-002（危险动作有视觉摩擦）与 REQ-001 的关键落点。

**使用位置：**
SCREEN-002（Ask 模式逐改动审批、computer use 分级授权）。

**变体：**

| 变体 | 用途 |
|---|---|
| Primary（常规审批） | Ask 模式下文件编辑/命令的 Accept/Reject |
| Danger（高危动作） | 破坏性操作的审批，强 danger 语义 + 额外说明 |
| Tiered（分级授权） | computer use 的 View only / Click only / Full control 三档，宽影响应用额外警告 |

**状态：**

| 状态 | 视觉表现 | 交互表现 |
|---|---|---|
| 默认 | 显示待审动作 + Accept/Reject 控件 [待补充 精确布局/按钮样式/风险指示，DQ-006/Q-006] | 点 Accept/Reject 或键盘选 |
| 悬停 | 按钮高亮 | — |
| 聚焦 | 焦点环，危险按钮聚焦时不误触发 | 键盘可达 |
| 激活 | 选定后动作放行或拒绝 | — |
| 禁用 | 远程会话下 Ask 模式不出现此卡 | — |
| 加载 | 等待用户决定（会话标 needs approval） | 阻塞该动作直到决定 |
| 错误 | 高危动作（Bypass on root/sudo）被拦 → danger 提示 | 不放行 |

**内容规则：**
- 清楚说明"要批准什么动作"，高危动作（删库、push-to-main、Bypass）显式标红并说明后果。
- computer use 分级授权三档语义清晰；宽影响应用（终端、Finder/Explorer、System Settings）额外警告。

**可访问性要求：**
- 危险按钮不只靠红色——配文字（如 "Reject"/"Allow once"）和图标。
- 默认焦点不落在危险动作上，防误触；键盘可完整操作。

> 注：单个审批卡的确切视觉（布局/按钮样式/风险指示）公开截图未给，标 [待补充]，见 §10.2 DQ-006。

---

### CMP-008: 权限-模式选择器（Permission-Mode Selector）

**组件用途：**
切换 agent 自主级别，是分级权限（REQ-001）在 UI 上的旋钮，DP-002 的核心入口。

**使用位置：**
SCREEN-002（prompt 区）。

**变体：**

| 变体 | 用途 |
|---|---|
| Ask permissions（默认） | 每次问，最安全 |
| Auto accept edits | 工作目录内编辑自动放行 |
| Plan mode | 只读探索先批计划 |
| Auto（research preview） | 分类器中介自动放行 |
| Bypass permissions | 禁用全部检查（默认关、高危） |

**状态：**

| 状态 | 视觉表现 | 交互表现 |
|---|---|---|
| 默认 | 显示当前模式名 [待补充 精确样式] | Cmd+Shift+M 开菜单或 Shift+Tab 循环（default→acceptEdits→plan） |
| 悬停 | 高亮 | 显示可选模式 |
| 聚焦 | 焦点环 | 键盘可选 |
| 激活 | 选中模式后即时生效（无加载态） | 后续动作按新模式门禁 |
| 禁用 | 远程会话下 Ask 与 Bypass 不可选；Auto 仅 Opus 4.6+/Sonnet 4.6 + Anthropic API 可选 | 不满足条件的模式置灰 |
| 加载 | N/A（切换即时） | — |
| 错误 | root/sudo 下试选 Bypass → 拦截提示 | 不切换 |

**内容规则：**
- 模式名清晰；Auto 标 research preview；Bypass 标高危。
- 当前自主级别始终可见，让用户随时知道"agent 现在有多大权限"。

**可访问性要求：**
- 模式不只靠颜色区分；Bypass 高危态有明确文字标识。
- 完整键盘可操作（Cmd+Shift+M / Shift+Tab / 数字键 1–9 选）。

---

### CMP-009: usage ring

**组件用途：**
显示单会话上下文 + plan 用量，对应 Spec §11.6 成本可视、REQ（上下文管理 SCOPE-018）。

**使用位置：**
SCREEN-001 / SCREEN-002（model picker 旁）。

**变体：**

| 变体 | 用途 |
|---|---|
| Primary | 常规用量显示 |
| 近上限 | 接近上下文上限时（将触发自动压缩） |

**状态：**

| 状态 | 视觉表现 | 交互表现 |
|---|---|---|
| 默认 | 小圆环按填充度反映用量 | 点击展开单会话上下文 + plan 用量明细 |
| 悬停 | 显示精确百分比/数值 | — |
| 聚焦 | 焦点环 | 键盘可展开 |
| 激活 | 展开明细面板 | — |
| 禁用 | — | — |
| 加载 | 实时反映上下文填充 | — |
| 错误 | 近上限时变色/提示（将自动压缩，先清旧工具输出再摘要） | — |

**内容规则：**
- 用量数值清晰；近上限给可理解的提示（不只变红）。

**可访问性要求：**
- 环形进度不只靠颜色，配数值/文字。

---

### CMP-010: 内联评论框（Inline Comment Box）

**组件用途：**
在 diff 某行留多行评论让 agent 改向，对应 REQ-003。

**使用位置：**
SCREEN-003（diff 窗格）。

**变体：**

| 变体 | 用途 |
|---|---|
| Primary | 用户写评论 |
| Claude 评审评论 | "Review code" 后 Claude 留的内联评论 |

**状态：**

| 状态 | 视觉表现 | 交互表现 |
|---|---|---|
| 默认 | 点行展开的输入框 | 输入多行 |
| 悬停 | — | — |
| 聚焦 | 输入框聚焦态清晰 | Cmd/Ctrl+Enter 提交 |
| 激活 | 提交后评论挂在该行 | agent 据评论改向 |
| 禁用 | — | — |
| 加载 | 提交中 | — |
| 错误 | [待补充] | — |

**内容规则：**
- 用户评论与 Claude 评审评论视觉可区分（来源标识）。

**可访问性要求：**
- 输入框键盘可达，提交快捷键明确。

---

### CMP-001 / CMP-003 / CMP-011 / CMP-012 / CMP-013 / CMP-014（辅助组件，简表）

| 编号 | 组件 | 用途 | 关键状态/规则 |
|---|---|---|---|
| CMP-001 | 会话条目（Session Item） | 侧栏里一个会话的表示，是 DP-003"会话第一公民"的视觉单元，承载 CMP-002 状态指示器 + Dispatch/bg 徽章 + 会话名/项目（REQ-007） | 变体：本地/远程/Dispatch/后台(bg)；状态：默认/悬停/激活（当前选中高亮）/聚焦（键盘可达，Ctrl+Tab 导航）/禁用（归档会话弱化仍可点开）；状态与徽章不只靠颜色配文字/图标（§8.2）；精确视觉 [待补充] |
| CMP-003 | 窗格容器（Pane） | 可拖拽缩放的命名窗格框 | 默认/聚焦（被拖拽）/缩放中；需 v1.2581.0+；header 可拖、边缘可缩放 |
| CMP-011 | "Review code" 按钮 | 触发 Claude 自评审 diff | 默认/悬停/聚焦/加载（评审中）/完成（留下内联评论） |
| CMP-012 | 开关（Toggle） | autoVerify / Persist sessions / Auto-fix / Auto-merge | 开/关双态，状态不只靠颜色配文字；默认值部分 [待补充]（Q-008） |
| CMP-013 | 警告条（Warning Banner） | 磁盘改动警告、高危提示 | warning 语义 + 文字说明，不只靠色；带操作（覆盖/重载） |
| CMP-014 | 计划决定控件（Plan Decision） | approve/accept-edits/manual/keep-planning/refine | 五选项键盘可达，approve 是 primary、refine 触发再规划 |

---

## 5. 视觉系统

> 界面型轨。桌面 GUI 有真实 CSS 视觉，故本段不标 N/A。但当前形态的精确 token 在公开来源不可考——已知方向：纯黑背景 + 饱和蓝用户气泡 + 亮蓝强调（GitHub issue #48158，称偏刺眼）。具体值标 [待补充] 交设计工具裁决，见 §10.2 DQ-005。

### 5.1 色彩变量

| 变量 | 用途 | 值 / 方向 |
|---|---|---|
| color.background | 页面/窗口背景 | 纯黑（issue #48158 称 `#000000` 量级，偏刺眼）；精确值 [待补充] |
| color.surface | 窗格、卡片、面板 | 比背景略亮的深色面（旧版为分层灰，新版更黑）；精确值 [待补充] |
| color.primary | 主操作、强调、用户气泡 | 亮蓝/饱和蓝（issue #48158）；精确值 [待补充] |
| color.danger | 错误、危险操作（删库/push-to-main/Bypass/diff 删行） | 红色语义；精确值 [待补充] |
| color.success | 成功、diff 增行、检查通过 | 绿色语义；精确值 [待补充] |
| color.warning | 警告（磁盘改动、Auto 暂停、近上限） | 黄/橙语义；精确值 [待补充] |
| color.text.primary | 主文本 | 深色背景上的高对比浅色；精确值 [待补充] |
| color.text.secondary | 次级文本（摘要、辅助说明） | 中等对比浅灰；精确值 [待补充] |
| color.border | 窗格分割线、边框 | 低对比深色边；精确值 [待补充] |

> 语义色（success/danger/warning）的存在是确定的（diff 增删、危险动作、警告都需要），具体色值待设计工具据品牌定。issue #48158 反馈纯黑偏刺眼，设计工具可据此考虑略提背景明度或提供层次。

### 5.2 字体排版

| 变量 | 用途 | 字号 / 字重 / 行高 |
|---|---|---|
| text.heading.lg | 会话名 / 窗格主标题 | 方向：清晰但不夸张的标题级；精确值 [待补充] |
| text.heading.md | 区块标题 / 文件名 | 方向：中标题级；精确值 [待补充] |
| text.body | transcript 正文 / UI 文本 | 方向：可长时间阅读的正文级；精确值 [待补充] |
| text.caption | 摘要 / 辅助说明 / diff-stats | 方向：小号辅助；精确值 [待补充] |
| text.mono | 代码 / diff / 终端 / 路径 | 等宽字体，保证代码对齐；具体字族 [待补充] |

> 工具型产品：UI 文本与代码文本分两套——UI 用近似系统无衬线、代码/终端/路径用等宽。

### 5.3 间距系统

| 变量 | 用途 | 值 |
|---|---|---|
| space.xs | 紧密元素（图标与文字、状态点与标签） | [待补充] |
| space.sm | 按钮内部、表单内部 | [待补充] |
| space.md | 卡片/窗格内边距 | [待补充] |
| space.lg | 窗格区块间距 | [待补充] |
| space.xl | 大区块间距 | [待补充] |

> 信息密度中等偏紧凑（§1.2）——间距方向应偏收敛以容纳多窗格并排，但 transcript 与代码区留足行间呼吸。

### 5.4 形状与层级

| 变量 | 用途 | 值 / 方向 |
|---|---|---|
| radius.sm | 小组件（按钮、徽章、状态点） | 方向：小圆角，工具型不过度圆润；[待补充] |
| radius.md | 卡片、窗格、输入框 | 方向：中圆角；[待补充] |
| radius.lg | 弹窗、大容器 | 方向：略大圆角；[待补充] |
| shadow.sm | 轻微层级（悬浮窗格、下拉） | 深色 UI 阴影克制，多靠明度分层而非投影；[待补充] |
| shadow.md | 弹窗、浮层 | 同上，克制；[待补充] |

> 深色工具型：层级更多靠 surface 明度差与边框，而非重投影。反参考里的"阴影堆砌"在此 fence 死。

### 5.5 图标与图像风格

- 图标：线性、统一、工具型，与 VS Code/Linear 一脉的克制图标系；语义图标（运行/审批/完成/PR/警告/危险）需与颜色双编码。
- 插画：极少。空状态可用极简线性图标或纯文字提示，不用花哨插画（反参考"装饰性"）。
- 头像：N/A（开发者工具，无社交头像场景）。
- 截图/预览：preview 窗格渲染真实产物（HTML/PDF/图片/视频），不做美化加工。

---

## 6. 交互与反馈规则

> 界面型全局交互规则。对话式 Agent 的运行态反馈（流式/思考/等授权/被中断）见 §A.3。

### 6.1 全局交互规则

| 模式 | 规则 |
|---|---|
| 主操作 | 自然语言 prompt 是主操作，固定在 chat 窗格底部；窗格内主操作（Accept/approve/启动预览）放在窗格显眼处 |
| 次要操作 | slash 命令、快捷键、右键上下文菜单承载次要操作；不挤占主视觉 |
| 危险操作 | 必须确认且有视觉摩擦（DP-002）：删库/push-to-main/force-push 等由 Auto 分类器命名拦截转人；Bypass 默认关、需去 Settings 显式开、root/sudo 禁；高危审批卡标 danger 语义 |
| 保存行为 | 文件编辑器手动 Save/Discard，磁盘改动警告；agent 改动经 diff 审批落盘（Ask 逐改动 Accept/Reject）；会话自动持久化为 JSONL，无需手动存 |
| 导航 | 侧栏切会话、Ctrl+Tab 循环；点路径跳 file/preview 窗格；Esc 打断 agent、改向是正常输入；Esc-Esc/`/rewind` 回滚文件+对话 |
| 弹窗 / 抽屉 | 尽量少用模态——Plan 用可拖拽窗格非模态（REQ-009）；审批用 transcript 内嵌卡而非全屏模态；设置等才用独立面板 |

### 6.2 反馈规则

| 场景 | UI 反馈 | 文案规则 |
|---|---|---|
| 成功 | 改动落盘、会话状态转 finished、CI 全绿、autoVerify 自查通过 | 直接陈述结果（"已完成""已合并"），不夸张庆祝 |
| 错误 | 错误/堆栈在 transcript 渲染（与正常输出区分 [待补充]）；高危被拦给 danger 提示；Auto-fix 修不动转人 | 说清楚出了什么、下一步能做什么，不甩术语不卸责 |
| 警告 | 磁盘改动警告条、Auto 模式累计拦截暂停提示、近上限将自动压缩提示、computer use 宽影响应用额外警告 | 说明风险与影响范围，给用户决定权 |
| 空状态 | 无会话提示新建、无改动 diff 空、无 PR 不显示 CI 栏、新 transcript 有 placeholder | 简短说明 + 下一步（"+ New session""提个需求开始"），不啰嗦不卖萌 |
| 加载 | 流式 token-by-token、思考中、跑工具中、dev server 启动中、CI 轮询中（见 §A.3） | 显示在做什么（"正在读取文件…""正在运行测试…"），不空转 spinner |

### 6.3 动效规则

- 动效**克制**——工具型生产力产品，动效服务于状态感知而非炫技。
- 允许动画：流式输出的渐入、状态指示器的脉冲（running）、窗格拖拽缩放的跟手反馈、压缩/思考的轻量进行态。
- 不应使用动画：大面积转场、装饰性弹跳、抢注意力的高频动效——这些违背"克制"与反参考的"装饰性"。
- 性能优先：长 transcript、流式渲染、多窗格并存时动效不能拖累响应；agent 自主循环跑数十次工具调用时 UI 必须保持流畅可中断。

---

## 7. UX 文案与内容规则

### 7.1 文案语气

清晰、直接、专业、不卖萌。面向开发者，假定用户懂技术术语（diff、worktree、PR、push），不必过度解释基础概念，但危险操作和受限场景必须说人话讲清后果。整体气质与 §1.2 一致：工具型、克制、可靠。

### 7.2 按钮文案规则

- 主按钮：动词开头、说清动作——"Accept""Approve""Run preview""Review code"，不用模糊的"OK/确定"。
- 次按钮：中性动词——"Reject""Discard""Keep planning""Refine"。
- 危险按钮：明确后果，不软化——"Enable Bypass permissions"（配风险说明）、"Force push"（标红）；绝不用诱导性措辞淡化危险（DP-002）。

### 7.3 表单文案规则

- label：直白名词/短语，与开发者既有词汇一致（如 "Working directory""launch.json"）。
- placeholder：示例或引导，不替代 label；prompt 输入区 placeholder 引导提需求。
- helper text：解释配置项作用与约束（如 "仅本地会话可用""需 v1.2581.0+"）。
- error message：说清错误 + 下一步，关联到具体字段/动作（如 "以 root 运行时无法启用 Bypass permissions"）。

### 7.4 空状态文案规则

- 解释原因：简短说明为什么空（"暂无改动""还没有会话"）。
- 给下一步：明确引导（"+ New session 开始""提个需求让 agent 动手"）。
- 不卖萌：不用鼓励性煽情文案，工具型产品保持克制（与反参考"装饰性"一致）。

---

## 8. 可访问性与响应式要求

### 8.1 可访问性目标

基础可访问性，向 WCAG 2.2 AA 看齐——这是开发者整天使用的生产力工具，键盘可操作性与对比度尤其关键（Spec §8 把"大量键盘快捷键覆盖核心操作"列为可访问性要求）。

### 8.2 必需可访问性规则

- [x] 所有可交互元素可通过键盘访问（Cmd+/ 列全部快捷键；新建/关闭/会话导航/停止/各窗格切换/模式与 effort 菜单/数字键选项均有快捷键）
- [x] 焦点状态清晰可见（尤其危险按钮聚焦时不误触发）
- [x] 表单字段有明确 label（配置项、设置开关）
- [x] 错误信息与对应字段/动作关联（如 Bypass on root 提示）
- [x] 不只依赖颜色表达状态（状态指示器、diff 增删、危险动作、开关均需文字/图标/符号双编码——这是硬约束，对应 DP-001/DP-002）
- [x] 正文和背景有足够对比度（纯黑背景下浅色文本需达 AA；issue #48158 反馈纯黑偏刺眼，设计工具应校准对比度避免过曝）
- [x] 图片和图标有合适替代文本或被标记为装饰性（语义图标配 aria-label）
- [x] 页面标题和区块标题结构清晰（窗格标题、transcript 消息结构、计划结构化）

### 8.3 响应式规则

> 桌面端界面型，不走移动断点。Desktop 仅 macOS + Windows 桌面（OUT-001，Linux = CLI only）。窄屏取舍走窗格折叠，多 surface 一致性走 §A.5。

| 断点 / 设备 | 规则 |
|---|---|
| 移动端 | N/A——无移动 Desktop 客户端（移动靠 iOS app 监控远程会话，不是本产品 GUI） |
| 平板端 | N/A |
| 桌面端 | macOS（universal Intel+Apple Silicon）+ Windows（x64；ARM64 安装器）；窗口可缩放，窗格随窗口尺寸重排；窄窗口下部分窗格自动收起/可折叠（具体折叠优先级 [待补充]）；split-view 在足够宽时左右排开两会话；窗格布局需 Claude Desktop v1.2581.0+ |

---

## A. 终端与对话式 Agent 形态规格

> 本产品是对话式 Agent（叠加桌面界面型）。§A 规格 Agent 维度，与界面型的 §2/§3/§5/§8.3 并存而非替代。§A.5 终端渲染相关项标 N/A（这是桌面 GUI 不是终端），多 surface 一致性照填。A.4 透明度与授权是 Agent 形态的核心，必填。

### A.1 交互骨架与模式

| 维度 | 方向 |
|---|---|
| 驱动方式 | **三样叠加**：自然语言（底部 chat 输入，主）+ slash 命令（60+ 内建，含 bundled skills/workflows）+ 键盘快捷键（大量，Cmd+/ 列全）+ `@` 文件补全（本地/SSH）。自然语言是主驱动，命令与快捷键是加速器 |
| 模式 | **5 种权限模式（Desktop 暴露）**：Ask permissions（默认）→ Auto accept edits → Plan mode → Auto（research preview）→ Bypass permissions。叠加后台/计划等运行模式。默认 **Ask permissions**。模式即"agent 自主度旋钮"，对应 REQ-001。CLI 第 6 种 `dontAsk` 不在 Desktop（OUT-002） |
| 一轮交互 | 用户在 chat 底部输入需求 → agent 收集上下文（读文件/搜索，工具调用按 view mode 渲染进 transcript）→ 提出/采取动作（编辑/命令/工具），受当前权限模式门禁 → 用户审 diff 逐改动 Accept/Reject 或 Esc 打断改向（改向是正常输入）→ agent 验证（跑测试、预览 autoVerify）→ 出结果，会话状态转 finished。对应 FLOW-001 |

### A.2 呈现单元与技术内容

| 呈现单元 | 呈现方向 |
|---|---|
| 用户消息 | chat 窗格底部 prompt 输入，发送后入 transcript；用饱和蓝气泡 [待补充 精确色]；支持拖拽文件/图片、`@` 补全（CMP-004） |
| Agent 回复 | 流式 token-by-token 渲染进中央 transcript；markdown 渲染（代码块/列表/标题）；无气泡或弱背景区分于用户消息（CMP-004） |
| 工具调用 | 按 view mode 呈现——Normal 折叠成摘要 / Verbose 每步全展 / Summary 仅最终响应+改动（Ctrl+O 切换）。折叠 vs 展开的精确视觉（边框/图标/展开三角）[待补充]（CMP-005，DQ-001） |
| 代码 / diff | 高亮：是，等宽渲染；diff 分行着色（增绿删红）+ `+`/`-` 前缀双编码；transcript 内是 diff 摘要，完整审查在 diff 窗格（CMP-006 / REQ-003）。unified vs split [待补充]（DQ-002） |
| 命令输出 | 终端窗格承载（SCREEN-005，仅本地会话）；长输出折叠策略 [待补充]；transcript 内的命令输出按 view mode 详略 |
| 思考 / 计划 / 待办 | **思考**：extended thinking 默认开，在 transcript 的渲染方式（可折叠 'thinking…' 块 vs 独立窗格 vs 内联）[待补充]（DQ-003）。**计划**：Plan 模式输出在 plan 窗格（可拖拽，非模态，默认展开），不是 transcript 内联（SCREEN-007 / REQ-009）。**待办/后台**：tasks 窗格列子 agent/后台 shell/workflows（SCREEN-008） |
| 错误 / 堆栈 | 在 transcript 渲染；与正常输出的精确视觉区分 [待补充]（DQ-003 同类缺口）。方向：应有 danger 语义区分，让用户一眼看出是错误 |

### A.3 Agent 运行态

| 运行态 | 显示方向 |
|---|---|
| 流式输出中 | agent 回复 token-by-token 渲染进 transcript；可随时 Esc 打断（CMP-004 加载态） |
| 思考中 | extended thinking 进行态；渲染方式 [待补充]（DQ-003）。方向：应有可感知的"思考中"指示，不留白屏 |
| 跑工具中 | **显示在跑什么工具**——工具调用卡显示工具名 + 进行态（CMP-005）。这是透明性（DP-001）的硬要求：用户必须知道 agent 此刻在用什么能力 |
| 等待授权 | 会话标 **needs approval**（侧栏状态指示器 CMP-002）；Ask 模式在 transcript/diff 弹审批卡（CMP-007）逐改动 Accept/Reject；阻塞该动作直到用户决定（FLOW-001） |
| 被中断 | Esc 停止 agent 响应；**改向被当作正常输入而非失败**（DP-004）——不报错、不甩"已取消"的负面态，用户输入新指令即继续。随时可暂停/停止 |
| 长任务进度 | 多处可见可停：tasks 窗格（子 agent/后台 shell/workflows）、plan 窗格（计划进度）、`/workflows` 进度视图、CI 状态栏（PR 检查）；CI 完成触发 OS 通知。针对 Ralph-Wiggum 自主循环这类极端，**必须 surfacing 循环状态/迭代/完成条件让用户能停掉失控**（Spec §11.7 护栏） |
| 后台 / 分离 | `/background` 分离会话到后台 headless 跑，侧栏与 `/resume` 标 'bg'；远程会话应用关闭后继续（FLOW-002） |
| 压缩中 | 近上限自动压缩（先清旧工具输出再摘要），usage ring 反映上下文填充；`/compact` 可提前手动压缩带 focus；CLAUDE.md 压缩后重注入（SCOPE-018） |
| Plan 模式（只读） | agent 只用只读工具探索、不改源码，等计划批准；试图改源码被只读工具集拦住（REQ-009 / FLOW-003） |
| Auto 模式暂停 | 累计 3 次连续或 20 次总拦截 → 分类器自动暂停、转回询问，给 warning 提示（REQ-002 / FLOW-004） |

### A.4 透明度与授权

> Agent 形态核心，必填。对应 Spec §11.1 自主性与人在回路、REQ-001/REQ-002。

| 维度 | 规则方向 |
|---|---|
| 推理展示 | **可展开**。工具调用、动作过程随 view mode 调详略（Normal 折叠/Verbose 全展/Summary 仅结果）；extended thinking 默认开。透明性是结构性的（Spec §11：harness 约 98.4%、AI 决策约 1.6%），不藏过程 |
| 动作可回溯 | **留完整动作记录**。每会话 transcript + JSONL trace 存 `~/.claude/projects/`；view mode 控制呈现详略；hooks 30 生命周期事件提供事件级 tracing；diff-stats 在会话 header 显示累计改动。回溯靠 transcript + JSONL + Rewind |
| 授权粒度 | **分级可调**：每次问（Ask）→ 工作目录内自动（AcceptEdits）→ 只读先批计划（Plan）→ 分类器自动放行（Auto）→ 全放行（Bypass）。粒度可到单改动（Ask 逐改动 Accept/Reject）、单应用（computer use View/Click/Full 三档）、单会话（"don't push" 会话内边界）。对应 REQ-001 |
| 动作分级 | **读自动放行，写受门禁，破坏性拦截**：文件读取/搜索全模式自动放行；文件编辑/命令按模式（Ask 弹/AcceptEdits 工作目录内自动/Plan 禁）；依赖安装/只读 HTTP/推到起始分支在 Auto 放行；破坏性操作 Auto 拦截转人（Spec §11.1 表） |
| 危险动作拦截 | **命名逃生舱 + 多层兜底**。Auto 分类器命名拦截：`curl\|bash`、密钥外泄、生产部署/迁移、批量删除、IAM 变更、force-push/push-to-main → 转人确认，不静默执行；受保护路径（.git/.gitconfig/.bashrc）除 Bypass 外不自动放行；Bypass 默认关、Settings 显式开、root/sudo 禁、企业可禁；PreToolUse hook 确定性兜底（exit code 2 拦截）；deny → ask → allow 默认拒。**最贵的错是生产环境不可逆破坏**——这是视觉与流程都要堆摩擦的地方（DP-002 / REQ-002 / Spec AI 护栏） |
| 可中断 | **运行中随时可打断、可改向**。Esc 停 agent 响应；改向被当作正常输入而非失败（DP-004）；Esc-Esc/`/rewind` 回滚文件+对话到改动前（但不撤销 DB/API/部署副作用，REQ-005）；Auto 模式累计拦截阈值自动暂停转询问。随时可暂停/停止（FLOW-001 边界、Spec §11.7） |

### A.5 终端视觉系统与多 surface

> 本产品是桌面 GUI 不是终端——终端渲染相关项标 N/A，走 §5 的 CSS 口径。多 surface 一致性照填。

| 维度 | 方向 |
|---|---|
| 渲染基元 | **N/A**——桌面 GUI 用真实 CSS/组件渲染（窗格、卡片、按钮），非 ASCII/box-drawing 终端绘制。视觉走 §5 |
| 色彩深度 | **N/A**——非 ANSI 终端色彩，用 CSS 全色域（深色主题，纯黑 + 蓝强调，见 §5.1） |
| 语义色 | 走 §5.1 CSS 语义色：success（绿，diff 增/检查通过）、danger（红，diff 删/危险动作）、warning（黄橙，磁盘改动/Auto 暂停/近上限）、primary（蓝，强调/用户气泡）。精确值 [待补充] |
| 字体 | 双套：UI 文本近似系统无衬线，代码/diff/终端/路径用等宽（§5.2 text.mono）。非"纯等宽终端" |
| 列宽 degrade | **N/A（终端列宽语义）**——桌面 GUI 走窗口缩放与窗格折叠（§8.3），非 80/120 列 degrade。窄窗口下窗格自动收起/可折叠（折叠优先级 [待补充]） |
| 多 surface | **Desktop 是本产品（primary）**。siblings 共享同源引擎与配置（CLAUDE.md/`~/.claude.json`/`.mcp.json`/hooks/skills/settings.json；会话历史各自独立）：**CLI**（原始 surface，加脚本化/headless/dontAsk/agent teams/第三方 provider/Linux，OUT-002~005 归 CLI）、**claude.ai/code 网页**（spawn/监控会话、Remote Control 驱动本地机）、**iOS**（监控/引导远程会话）、**VS Code/JetBrains 扩展**（IDE 内嵌）、**Slack/GitHub Actions**（聊天与流水线内嵌）。**一致边界**：配置、CLAUDE.md、权限规则、hooks/skills 跨 surface 一致。**可变边界**：Desktop 是交互式专属图形 surface（无 `--print`/`--output-format`，无 CLI-only 命令 `/permissions`、`/config`、`/agents`、`/doctor`，OUT-003）；远程会话能力受限（无 @mention/plugins/Ask/Bypass，OUT-008）；CLI `/desktop` 把会话搬进 Desktop（不支持 API-key/Bedrock/Vertex/Foundry 鉴权，SCOPE-017）。三标签内 Code 是 agentic 编码 surface，与 Chat/Cowork 标签共享窗口外壳但本文档只规格 Code 标签 |

---

## 9. 实现说明

- 前端技术栈：原生桌面应用（Electron 级别，macOS + Windows）；本文档不钉死具体框架，由实现方决定，但视觉 token 走 CSS 口径（§5）。
- 已有组件库：作为 Claude Desktop 内的一个标签，应继承 Claude Desktop 既有的设计系统与组件库——无设计稿时按"继承既有页面和组件的先例，不自由发挥"原则。
- 暗色模式：**深色为基准形态**（纯黑 + 蓝强调）。light mode 是否存在 / 是否跟随 OS 偏好未确认（DQ-004）——若实现 light mode 需补一套色彩 token。
- 品牌资产：继承 Anthropic / Claude 品牌（蓝色强调与 Claude Desktop 视觉一脉）。
- 必用设计模式：多窗格可拖拽工作区、会话侧栏、点路径跳转、内联 diff 评论、分级权限选择器、状态双编码（色+文字/图标）。
- 避免设计模式：黑箱式 AI（藏过程）、装饰性堆砌（渐变/重阴影/拟物）、朴素每次问到底的权限疲劳设计、危险动作无视觉摩擦——这四条是反参考 fence（§1.4 / DP-001 / DP-002）。
- 版本约束：窗格布局 / 终端 / 文件编辑器 / view modes 需 Claude Desktop v1.2581.0+；Windows 需先装 Git for Windows 并重启；无活跃付费档返回 403。

---

## 10. 假设与待确认问题

### 10.1 假设

| 编号 | 假设 | 假设依据 | 错误风险 |
|---|---|---|---|
| DASM-001 | 当前形态是深色工作区：纯黑背景 + 饱和蓝用户气泡 + 亮蓝强调 | GitHub issue #48158 给出新旧配色对比，称纯黑偏刺眼 | 若据此钉死设计 token 而无官方 token 表，视觉可能与官方不符——故具体值留 [待补充] 交设计工具，方向明确即可 |
| DASM-002 | 状态、diff 增删、危险动作、开关均需"色 + 文字/图标"双编码 | 可访问性硬约束（§8.2）+ DP-001/DP-002；深色纯黑下纯色区分不可靠 | 若只靠颜色，色盲用户与高危场景误判风险高；这是不可让步的设计约束 |
| DASM-003 | 深色为基准，移动/平板 N/A，响应式只走桌面窗口缩放 + 窗格折叠 | Spec OUT-001（仅 macOS+Windows，Linux=CLI）；无移动 Desktop GUI | 若误设计移动断点，做出范围外形态 |
| DASM-004 | 窗格视觉层级靠 surface 明度差 + 边框，而非重投影 | 深色工具型惯例（VS Code/Linear）+ 反参考"阴影堆砌" | 若重投影，与工具型克制气质冲突；可由设计工具微调 |
| DASM-005 | UI 文本与代码文本分两套字体（无衬线 + 等宽） | 工具型 IDE 惯例；代码需等宽对齐 | 若混用，代码对齐与可读性受损 |

### 10.2 待确认问题

> 与 Spec §10.2 同源——这些是公开来源不可考的视觉缺口，对应 Spec 的 Q-001~Q-008，标 [待补充] 不编造。

| 编号 | 问题 | 是否阻塞 | 备注 |
|---|---|---:|---|
| DQ-001 | 工具调用卡折叠 vs 展开的确切视觉（边框/图标/展开三角） | No | 对应 Spec Q-001；CMP-005 留 [待补充] |
| DQ-002 | diff viewer 是 unified 还是 split/side-by-side | No | 对应 Spec Q-002；一份第三方评测称 unified-only，官方未确认；CMP-006/SCREEN-003 留 [待补充] |
| DQ-003 | extended-thinking 块在 transcript 的渲染方式（可折叠块 vs 独立窗格 vs 内联）；错误/堆栈与正常输出的视觉区分 | No | 对应 Spec Q-003；§A.2/§A.3 思考态与错误态留 [待补充] |
| DQ-004 | Code 标签是否有 light mode 或跟随 OS 偏好 | No | 对应 Spec Q-004；chat/cowork 标签有 light/dark 切换，Code 标签继承情况未知；§9 留 [待补充] |
| DQ-005 | 暗色主题确切色彩 token（纯黑背景值、用户气泡蓝、强调蓝、各语义色） | No | 对应 Spec ASM-003/Q；§5.1 全表留 [待补充] 交设计工具 |
| DQ-006 | 单个审批弹窗卡的确切视觉（布局/按钮样式/风险指示） | No | 对应 Spec Q-006；CMP-007 留 [待补充] |
| DQ-007 | 是否存在独立 file-tree/project-browser 窗格，还是只有 diff viewer 内的文件列表 | No | 对应 Spec Q-005；MindStudio "file structure panel" 说法未确认，可能与 diff 文件列表混淆；本文档未单列该窗格，导航靠 @mention/点路径/右键菜单 |
| DQ-008 | 窄窗口下窗格折叠优先级、CI Auto-fix/Auto-merge 开关默认值 | No | 对应 Spec Q-008；§8.3 折叠策略与 CMP-012 默认值留 [待补充] |
| DQ-009 | 流式输出对屏幕阅读器的 aria-live 实现细节 | No | §8.2 可访问性实现层，官方未给 |
