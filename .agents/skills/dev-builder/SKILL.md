---
name: dev-builder
description: 当 DEV-PLAN.md 就绪、用户说要开始写代码或继续开发下一个 Phase 时使用。新项目搭建骨架，已有项目按 Phase 逐步实现功能。
---

[任务]
    初始化模式：无代码 + 有 DEV-PLAN.md → 搭建项目骨架，装依赖，配好环境，完成 Phase 1。
    持续开发模式：有代码 + 有 DEV-PLAN.md → 按 Phase 逐步开发，每个 Phase 走 per-Task review→fix 循环和四步走验证。
    规划与执行方式见 [主控文件] 的 [规划与执行]。

[依赖检测]
    Skill 启动第一步执行。
    必需：Product-Spec.md、DEV-PLAN.md、DEV-PLAN 技术栈表里列的系统工具和运行时。缺了提示先补。
    可选，缺了标降级模式继续：Design-Brief.md、设计工具 MCP、gh CLI、playwright、openspec/changes/<change>/、CONTEXT.md、CONTEXT-MAP.md、docs/adr/。
    必需依赖缺失你自己判断装法直接装；要用户权限或认证才提示用户。
    进已有项目或脚手架先读它自带的 agent 约定文件，比如 AGENTS.md、CLAUDE.md，按项目规矩来，不用自己的默认覆盖。

[文件结构]
    dev-builder/
    └── SKILL.md  # 本文件，无 references / templates

[第一性原则]
    修改纪律：改代码前先评估影响范围，改完回归验证。改之前想清楚，不改坏已有功能。删或改一个被多处引用的东西（常量、导出、内置数据、组件、接口、文案）前，先 grep 全项目列出所有引用点、一次性同步清理，别只改定义留下游脱节。改完跑全测试套件（typecheck + 单测 + e2e），不靠 typecheck 一条路就判定通过——typecheck 过不代表单测/e2e 不挂。连带不止代码引用：删/改的东西若会被持久化（写进 db、本地存储、缓存、配置文件），还要管已经落库的存量数据——加启动迁移清理或做兼容，否则老用户的旧数据会变成「代码已删、却顽固显示、还删不掉」的僵尸。删持久化数据的改动配一条迁移测试。
    模板纪律：修改模板或既有文档时优先局部补丁，保留原有合理结构、命名和措辞；除非用户明确要求重写或原结构阻碍目标，不做等价改写增加审阅成本。
    复用优先：先复用 Design-Brief 和现有公共样式、组件，禁止自造命名空间 override。同语义有多套体系时选更高级的那套。
    范围纪律：只动当前 Phase 和 Task 范围内的东西。范围外的不碰，哪怕 Spec 里写了该行为。
    测试隔离：测试和脚本跑真实 app（electron launch、起 server、连真实 db 等）必须用独立数据目录（--user-data-dir、独立临时库/配置），绝不写用户生产数据库/配置。新增跑 app 的测试入口要对齐既有隔离写法，一个入口漏隔离，测试垃圾就写进用户的生产库。隔离用的临时目录在收尾里清掉，别只 close 不删、长期堆成垃圾山。
    文档回写：开发中用户改了需求或设计，先回 product-spec-builder 或 design-brief-builder 更新文档再继续写，不让代码和文档脱节。
    真实优先：按钮、数字、卡片必须代表真实数据和真实行为。不写死、不假数据、不留装饰。引入项目里零先例的 UI 元素前先 grep 全项目确认先例，零先例默认不引入。
    SDK-First：框架和 SDK 已有的能力不重复造，用之前先确认。
    联网优先：用外部库、API 前先联网搜索或查官方文档确认当前版本用法，不靠过期记忆。
    验证即证据：完成声明必须在同一条消息里带上刚跑的验证命令和输出。"之前编译过了"无效，没有当场验证就没有完成。
    测试接缝：新增测试前先确认要测试的公开 seam，也就是用户行为、API、CLI、模块接口或持久化边界。测试只穿过公开 seam，不测私有实现；没有合适 seam 时先用更高层验证锁住行为，并把缺 seam 作为设计风险报告。
    代码精简：三行直白代码好过一个过度抽象。
    喂模型不截断：喂给模型/Agent 的工具结果与给人看的 UI 摘要是两份产物——UI 可截断省略，喂模型的绝不能截断；处理长文档/长输入时保证数据全量进模型，扛不住就改精简格式（如逐条压成单行）而不是砍条数。

[输出风格]
    像资深工程师汇报：简洁、准确、有数据。完成就说完成，有问题就说有问题。
    绝不用"应该没问题""大概率通过""看起来正确"替代验证。要么验证通过说通过，要么没验证说未验证。
    改动前先说影响范围，改完说回归结果。遇阻塞说清原因和需要什么。

[开发规则清单]
    [代码规范]
        - 文件规模按有效代码行审查，空行、纯注释行、docstring/块注释说明行不计硬门槛；JS/TS/Python/Ruby/Go/Rust 默认 300 行触发拆分审查，500 行以上默认必须拆；Java/C#/Kotlin/Swift 默认 500 行触发拆分审查，1000 行以上默认必须拆
        - 生成文件、协议/表格集中定义、schema/migration、测试矩阵、解析器/状态机等高内聚文件可例外，但必须写明不拆理由；必要注释不能为了过线删除，无信息注释要删或压缩
        - 注释、docstring、测试说明和脚本说明按 [共享规则文件] 的通用注释纪律执行：默认使用项目主语言，不得无故把既有符合项目主语言的注释改成另一种语言；必要的标识符、协议字段、API 名称、CLI 参数、schema 字段、错误码和行业固定术语可保留英文。注释服务于维护意图、复杂控制流、数据形状、约束、边界、风险、设计取舍、兼容/迁移逻辑、测试夹具意图和项目规范要求；API、worker、迁移、并发、安全边界、测试夹具、公开 seam 等非显然函数和复杂代码块，必须说明维护意图、边界或验证目的；避免机械复述和 AI 式自述，但不能为了少写注释、压行数或过审而省掉维护者需要的说明
        - TypeScript strict，不用 any，用 unknown + 类型守卫
        - 命名：组件 PascalCase，函数变量 camelCase，文件 kebab-case，常量 UPPER_SNAKE_CASE
        - 每个文件单一职责，副作用隔离到 hooks 或 API 层
        - 跟随已有代码库的风格，不强推个人偏好，不做无关重构
        - YAGNI：不为假想的未来需求写代码

    [视觉与复用]
        - 动 UI 前先读 Design-Brief 和现有公共样式、组件，列出可复用项
        - 新页面继承相邻同类页面的语义、交互、危险操作规则，不一页单造一套
        - 复用对齐不止看组件名，要打开邻居基准页面实际对比渲染效果
        - 改完 UI 主动自检渲染边界，不等用户指出：按钮/文字不超容器、窄面板不挤爆（多按钮一行先估宽度，挤就拆行或图标化）、危险操作从主操作行分离并按 Design Brief 加确认。这类溢出/截断/挤压自己抓

    [质量门槛]
        每个功能要有：正常流程、错误提示、加载态、空状态、基本输入校验。无敏感信息硬编码。

    [环境与安全]
        - 浏览器可见的前缀变量不放密钥，AI 调用走服务端
        - .env.example 进 Git，实际值进 .gitignore
        - 不硬编码密钥、绝对路径、个人信息

    [数据库规范]
        - 表名字段名 snake_case，每表有 id、created_at、updated_at
        - migration 用 ALTER TABLE，执行前查列或表是否已存在
        - 参数化查询防注入，不裸拼 SQL

    [Git 工作流]
        - 原子提交：每完成一个独立功能就 commit，一个 commit 一个逻辑变更
        - commit message 用 feat、fix、refactor、chore 前缀
        - 提交门槛：编译通过才能 commit，不过编译不提交
        - push 由 hook 处理，保护分支不自动推

[设计参照]
    有设计工具 MCP 连接时，每个 Task 前读取涉及页面和组件的精确数值：宽高、间距、字号、字重、颜色、圆角、阴影。每个 Task 都重新读，不凭记忆。编码后读代码实际值逐项对照，有偏差先修再提交。设计稿与 Design-Brief 冲突时以设计稿为准。
    无设计工具时以 Design-Brief 为参照；无 Brief 时继承项目既有页面先例，不自由发挥。

[外部契约输入]
    OpenSpec change：如果用户指定 change、分支名或任务上下文能对应到 openspec/changes/<change>/，编码前读取该 change 下存在的 proposal.md、specs/**/*.md、design.md、tasks.md、README.md 和 .openspec.yaml。proposal/specs 说明本次行为契约，design 说明实现取舍，tasks 可作为 Task 拆分参考；它们不替代 Product-Spec.md 和 DEV-PLAN.md。发现 OpenSpec 与 Product-Spec.md、DEV-PLAN.md、Design-Brief.md 或设计稿冲突时，先列出冲突和影响，等用户拍板后再改。
    OpenSpec artifact 语言与复审门禁：本轮创建或修改 proposal.md、specs/**/*.md、design.md、tasks.md、README.md 时，标题、正文和验收说明默认使用项目主语言；必要的 MUST/SHALL/WHEN/THEN、字段名、命令、路径、schema、协议关键字保留英文。写完后先做语言自检，再运行 `openspec validate <change> --type change --strict` 做格式和契约可解析自检，并派 code-reviewer 按 OpenSpec artifact review 范围审查 proposal/specs/design/tasks 到 PASS；`openspec validate` 不能替代 code-reviewer，PASS 前不得进入实现。
    OpenSpec 增量切片：仓库存在 openspec/ 且用户目标是开发完整 DEV-PLAN Phase 时，进入实现前先为本轮最小行为增量创建或选定一个窄 OpenSpec change。这个 change 必须有 proposal/specs/design/tasks，范围只覆盖一个可审查、可验证、可提交的行为切片；不要把整段 Phase 塞进一个巨型 change。契约草案完成后派 code-reviewer 审查，按审查意见迭代到通过，再开始编码。
    OpenSpec 验证：涉及 OpenSpec change 时自动跑开发期验证门禁。change 草案完成后运行 `openspec validate <change> --type change --strict`；实现完成且 tasks 全勾后再次运行同一严格校验。CLI 不可用时说明未运行原因，并用文件结构和 delta spec 格式做静态检查，不得宣称已验证。不要自动 archive；`openspec archive <change>` 是 OpenSpec 原生的验证、同步主规格、归档收口命令，只在整体任务或 Phase 收口后提示用户可选执行；在支持 OPSX/OpenSpec 命令的 Agent 会话中，同时提示可用 `/opsx:archive <change>`。archive 不作为每个 change 的交互门槛。
    领域语言：存在 CONTEXT-MAP.md 时，先按映射读取与本次任务相关的 CONTEXT.md；否则有根目录 CONTEXT.md 就读。存在 docs/adr/ 或上下文目录 docs/adr/ 时，只读与本次改动相关的 ADR。缺失时静默降级，不要求用户补。
    使用边界：CONTEXT.md 只约束命名和领域边界，ADR 只约束既有决策；需求、范围、验收仍以 Product-Spec.md、DEV-PLAN.md、Design-Brief.md、设计稿和 OpenSpec change 为准。

[Phase 执行流程]
    Plan：进入 Phase 先读 DEV-PLAN 该 Phase 章节和 Spec 相关章节的原文，写出 Task 拆分。每个页面、组件、功能一个 Task。若本仓库有 openspec/ 且当前目标是完整 Phase，先把 Phase 拆成一个或多个窄 OpenSpec change；每轮只实现当前 change 覆盖的行为切片。
    每个 Task 走 review→fix 循环：
        编码前读 DEV-PLAN 交付清单、Spec 功能描述、Design-Brief 视觉方向，以及相关 OpenSpec change、CONTEXT.md、ADR 的原文，不凭记忆
        涉及 OpenSpec change 时，按该 change 的 tasks 和 delta spec 先写或更新公开 seam 测试，再实现；测试接缝必须来自用户行为、API、CLI、模块接口或持久化边界
        写测试前列出本 Task 的公开 seam 和验证路径，新增测试必须绑定其中一个 seam
        编码后自检：代码实际值对照设计数值，行为对照 Spec
        派发 code-reviewer 两阶段审查
        Stage 1 失败补实现，重新派 code-reviewer
        Stage 2 失败：质量和重构问题自己按修改纪律修，确属缺陷或安全漏洞才调 bug-fixer，重新派 code-reviewer
        两阶段都过 → echo clean > .agents/.needs-review → 完成当前 change 的严格验证和提交 → 回到 DEV-PLAN Phase 验收清单核对剩余缺口 → 下一个 Task 或下一个窄 change
        .agents/.needs-review 只是 stop hook 的本地门禁状态文件；写入 clean 或被 stop hook 删除不算产品代码、规则正文、契约文档、测试或配置改动，不因此重派 code-reviewer。
    用户强调某环节是追加要求，不替换基础流程，review 闭环照常走。

[Phase 完成度判断]
    单个 OpenSpec change 完成不等于 Phase 完成。每个 change 完成、审查、严格验证和提交后，必须回到 DEV-PLAN 该 Phase 的交付清单逐项核对；仍有缺口就继续下一个窄 change，直到 Phase 清单全部满足。不要在每个 change 后停下来询问是否 archive。整体任务或 Phase 收口时，可以提示用户按需运行 `openspec archive <change>`，或在 Agent 会话中使用 `/opsx:archive <change>`；若用户选择 archive，提醒 archive 后运行 `openspec list --json` 确认 active changes 状态，并运行 `openspec validate --all --strict` 确认主规格有效。
    每个 OpenSpec change 收口、Phase 验收通过、出现阻塞或返工时，必须同步 DEV-PLAN 的当前进度、剩余工作、风险/阻塞和下一步；这些状态区缺失时只补最小必要区块，不改写无关内容。
    所有 Task 完成后过四步走，全通过才算 Phase 完成：
        一、Code Review：对照 DEV-PLAN 交付清单逐项确认，检查有无超范围改动
        二、测试完整性：计划的功能都实现无半成品，且测试真覆盖到交互层和故障路径，不止纯函数和顺畅路径。每条「绿」要能证明行为真的对——核对用例前提与生产一致（量纲、单位、输入合法性、断言方向），用假前提或不可达输入把缺陷盖成预期的等于没测。功能声明的错误态、空态、边界要有用例真的走到，不只在实现里留分支
        三、编译验证：tsc --noEmit 零错误
        四、功能测试：启动 dev server 无错，新功能可用，现有功能未破坏；有 Playwright 测核心流程，无则 curl 查 API 返回再提醒用户看 UI
    每步附当场跑的证据。中间发生实质性代码、规则、契约、测试或配置改动，四步重新来；仅 .agents/.needs-review 的 clean 写入或删除不算实质改动。
    通过后向用户汇报附证据，用户确认后 Phase 完成。修验证中发现的问题用 fix: 提交。

[自驱整个 Phase]
    要把整个 Phase 交给 /goal 自驱，用 goal-creator 技能生成指令。生成 /goal 时先识别任务类型，不内联整套规则；只写入最小硬约束：执行前必须读取并在最终输出中列出本任务适用的规则来源、适用/不适用判断和自证证据。开发类 goal 至少引用 [共享规则文件]、dev-builder 和 code-review；涉及 OpenSpec change 时还要引用对应 proposal/specs/design/tasks，并要求输出 `openspec validate` 结果和 code-reviewer verdict。非开发类 goal 只引用对应任务类型的规则和 Skill，不把开发约束硬塞进去。完成条件仍以四步走验收为准，比如交付清单逐项贴出、编译输出已贴、code-reviewer 两阶段 PASS 已贴。

[初始化模式]
    无代码时搭骨架：
    - 项目代码放在以项目名命名的子文件夹，不平铺根目录，规划文档留根目录
    - 按 DEV-PLAN 技术栈表配置，TypeScript strict，装依赖，配环境变量
    - git init，.gitignore 排除规划文档、环境变量、构建产物，建 private 远程仓库，首次 commit
    - 完成后进入 Phase 1 的 Phase 执行流程

[初始化]
    检测项目状态路由：无代码 + 有 DEV-PLAN → 初始化模式；有代码 + 有 DEV-PLAN → 持续开发模式；无 DEV-PLAN → 提示 /dev-planner；无 Product-Spec → 提示 /product-spec-builder。
