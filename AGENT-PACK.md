# Agent Pack 使用说明

这是一套给 Codex 和 Claude Code 共用的项目级能力包。它把主控规则、Skills、hooks、固定 Sub-Agent 和自进化队列安装到具体项目里，让这些能力随项目进入版本控制，同时保留把通用改动升格回能力包的流程。

## 核心模型

Agent Pack 是能力包源码目录，维护通用的 `AGENTS.md`、`.agents/skills/`、`.codex/`、`.claude/` 和安装脚本。

项目是实际工程目录。安装后，项目里会得到一份本地副本：

```text
project/
├── AGENT-PACK.md
├── agent-pack
├── AGENTS.md
├── .gitignore
├── .agents/
│   ├── EVOLUTION.md
│   ├── RELATED-PROJECTS.md        # 可选：多项目协作关系，可提交
│   ├── agent-pack.lock.json
│   ├── evolution/
│   ├── templates/
│   └── skills/
├── .codex/
└── .claude/
```

项目代码和工程文件按项目自己的语言、框架、团队规范放置。Agent Pack 不规定 `src/`、`package.json`、`pyproject.toml` 或其他工程结构。

`.agents/evolution` 是自进化队列源目录；`.codex/evolution` 和 `.claude/evolution` 都是指向它的 symlink。

`.agents/evolution/signals.jsonl`、`.agents/evolution/proposals.md`、`.agents/related-projects.local.json` 和 `.agents/.needs-review` 是本地运行状态，不提交。

日常规则变化先改项目本地文件。只有确认某条规则、Skill、hook 或 Sub-Agent 对多个项目通用，并且用户明确同意升格时，才用 `agent-pack promote` 写回能力包。

## 脚本分工

能力包里的 `agent-pack`、`agent-pack.ps1`、`agent-pack.cmd` 都是薄入口。它们从脚本所在目录识别 Agent Pack，从当前执行目录识别项目，然后转发到 `.agents/cli/agent_pack_cli.py`。

项目根目录里的 `agent-pack`、`agent-pack.ps1`、`agent-pack.cmd` 是安装时生成的 launcher。它们读取 `.agents/agent-pack.lock.json`，再转发到真实能力包脚本；这样日常可以在项目里直接执行 `./agent-pack status`、PowerShell 下 `./agent-pack.ps1 status`，或 cmd 下 `agent-pack.cmd status`。

`install.sh`、`install.ps1`、`install.cmd` 是远程 bootstrap。它们先 clone 或 pull 能力包，再转交给对应平台的 `agent-pack` 入口。

## 平台支持

当前支持分三档，不要混着吹：

| 平台 | 状态 | 说明 |
| --- | --- | --- |
| macOS | 支持 | 主要开发和验证环境。CLI 核心走 Python；Bash 入口、`install.sh` 和 hooks 已验证。 |
| Linux | 支持 | 需要 Git、Python 3；Bash 入口和 `install.sh` 可用，端口清理依赖 `lsof`，缺失时只跳过清理，不阻塞其他 hook。 |
| Windows WSL | 支持 | 按 Linux 路径使用，建议 repo 放在 WSL 文件系统内。 |
| Windows Git Bash/MSYS | 兼容 | Bash 入口和 `install.sh` 转发到同一个 Python CLI；路径转换仍受 Git Bash/MSYS 规则影响。 |
| Windows PowerShell/cmd 原生 | 支持入口，需 Windows 实机验证 | `install.ps1`、`install.cmd`、`agent-pack.ps1`、`agent-pack.cmd`、hook `.ps1`/`.cmd` 包装都转发到同一个 Python core；install/update 会把 hook 配置渲染为 Windows 可执行命令。本仓库在 macOS 上做静态和集成验证，Windows 原生运行需在 Windows 10/11 上复验。 |

跨平台原则：

- CLI 业务逻辑只写在 `.agents/cli/agent_pack_*.py`，Bash、PowerShell、cmd 入口和 bootstrap 只定位 pack 并转发。
- hook 的业务逻辑只写在 `.agents/hooks/agent_pack_hook*.py`，Bash、PowerShell、cmd 只做薄包装，并把 hook name 与 agent 来源传给 runner。
- 新增 hook 行为必须同时能被 `.sh`、`.ps1`、`.cmd` 路径调用，不在 shell 包装里复制业务判断。
- macOS/Linux/WSL 按 POSIX hook 配置走 `.sh`；Windows 原生安装和更新会生成调用 PowerShell `.ps1` wrapper 的 `.codex/hooks.json` 和 `.claude/settings.json`。`.cmd` wrapper 同步安装，供 cmd 入口和需要时的手动配置使用。
- Windows 10/11 可能通过 Developer Mode、符号链接权限或管理员权限允许 symlink，所以安装逻辑先尝试 symlink。失败时输出明确警告，再对目录用 junction fallback、对文件用 hardlink/copy fallback。

## 首次安装

在目标项目根目录执行：

```bash
/path/to/agent-pack install
```

如果目标项目已经使用 OpenSpec，并且想安装 Agent Pack 风格的 OpenSpec schema，再显式 opt-in：

```bash
/path/to/agent-pack install --with-openspec
```

脚本会：

- 检测脚本所在目录是不是 Agent Pack。
- 询问是否还要同时安装到其他项目路径。
- 如果选择多个项目，询问项目组名称，以及每个项目的 id、角色和说明。
- 如果项目没有 `.gitignore`，按 Agent Pack 模板生成；如果已有 `.gitignore`，只补充 Agent Pack 本地状态忽略项。
- 检测 Python 3；`agent-pack` 安装、lock/hash 和 hook runner 都依赖它。
- 将规则、Skills、hooks、Sub-Agent 配置复制到每个项目。
- 将共享 hook runner 复制到 `.agents/hooks/`；平台专属 hook 脚本只做薄包装。
- 在 Windows 原生环境下，安装/更新会把 `.codex/hooks.json` 和 `.claude/settings.json` 渲染为调用 `.ps1` wrapper 的命令，并把生成后的 hash 记录进 lock，避免 `status` 误报。
- 将能力包 README 复制为项目根目录的 `AGENT-PACK.md`，避免和项目自己的 `README.md` 冲突，也方便直接查看。
- 在项目根目录生成 `agent-pack`、`agent-pack.ps1`、`agent-pack.cmd` launcher，便于日常执行命令；如果项目已有同名 `agent-pack` 且不是 launcher，脚本会跳过，不覆盖。
- 创建 `.agents/agent-pack.lock.json`，记录能力包来源、commit 和文件 hash。
- 多项目安装时创建 `.agents/RELATED-PROJECTS.md`，并创建本机路径表 `.agents/related-projects.local.json`。
- 创建 `.agents/evolution/signals.jsonl` 和 `.agents/evolution/proposals.md`，并通过 `.gitignore` 忽略队列内容。
- 创建 `.codex/evolution`、`.claude/evolution` 到 `.agents/evolution` 的链接；Windows 原生 symlink 不可用时目录使用 junction fallback。
- 创建 `.codex/EVOLUTION.md`、`.claude/EVOLUTION.md` 到 `.agents/EVOLUTION.md` 的链接；Windows 原生 symlink 不可用时文件使用 hardlink/copy fallback。
- 创建 `.claude/skills/*` 到 `.agents/skills/*` 的链接；Windows 原生 symlink 不可用时目录使用 junction fallback。
- 复制能力包里的 `.claude/CLAUDE.md` 到项目。
- 对源文件带可执行位或 shebang 的脚本执行 `chmod u+x`，确保当前用户能运行 hooks 和 Skill 附带脚本。
- 如果显式传入 `--with-openspec` 且目标项目已存在 `openspec/`，复制 `.agents/templates/openspec/schema-agent-pack-product-change/` 到 `openspec/schemas/agent-pack-product-change/`，并在 `openspec/config.yaml` 未设置或仍是 `spec-driven` 时把默认 schema 切到 `agent-pack-product-change`；如果已有其他自定义 schema，只提示不覆盖。如果没有 `openspec/`，只打印引导，不初始化 OpenSpec、不 vendoring OpenSpec。

如果项目里已有普通文件且不是 lock 管理的安装副本，脚本会跳过，不会强行覆盖。旧的 `.codex/evolution` 或 `.claude/evolution` 普通目录会迁移到 `.agents/evolution`，再替换成 symlink/junction。

安装确认默认是 yes，直接回车会继续安装。

## 远程安装

适合第一次在机器上拿到能力包。公开仓库和私有仓库的脚本下载方式不一样，仓库 clone/pull 地址也可以按读写需求选择 HTTPS 或 SSH。

远程安装仍然会把 Agent Pack 安装到当前工作目录，所以先 `cd` 到目标项目根目录再执行。`curl/gh | bash` 会尽量从当前终端继续读取交互输入；没有 TTY 的环境下，安装会按默认值继续：不追加其他项目，并确认安装当前目录。

### 公开仓库

公开仓库可以直接从 `raw.githubusercontent.com` 下载 bootstrap 脚本。

macOS / Linux / WSL / Git Bash：

```bash
curl -fsSL https://raw.githubusercontent.com/wdmcheng/agent-pack/master/install.sh | \
  bash -s -- --repo https://github.com/wdmcheng/agent-pack.git install
```

指定本地能力包目录：

```bash
curl -fsSL https://raw.githubusercontent.com/wdmcheng/agent-pack/master/install.sh | \
  bash -s -- --repo https://github.com/wdmcheng/agent-pack.git --dir ~/.agent-packs/vibe install
```

也可以用环境变量指定默认远端：

```bash
export AGENT_PACK_REMOTE=https://github.com/wdmcheng/agent-pack.git
curl -fsSL https://raw.githubusercontent.com/wdmcheng/agent-pack/master/install.sh | bash
```

Windows PowerShell 原生：

```powershell
$installer = Join-Path $env:TEMP "agent-pack-install.ps1"
Invoke-WebRequest `
  -Uri "https://raw.githubusercontent.com/wdmcheng/agent-pack/master/install.ps1" `
  -OutFile $installer
powershell -ExecutionPolicy Bypass -File $installer --repo https://github.com/wdmcheng/agent-pack.git install
```

Windows cmd 原生：

```bat
curl.exe -fsSL https://raw.githubusercontent.com/wdmcheng/agent-pack/master/install.ps1 -o "%TEMP%\install.ps1"
curl.exe -fsSL https://raw.githubusercontent.com/wdmcheng/agent-pack/master/install.cmd -o "%TEMP%\agent-pack-install.cmd"
"%TEMP%\agent-pack-install.cmd" --repo https://github.com/wdmcheng/agent-pack.git install
```

### 私有仓库

私有仓库不能依赖匿名 `raw.githubusercontent.com`。推荐用已登录且有仓库权限的 GitHub CLI 下载脚本，再用 SSH 地址 clone/pull 能力包。

macOS / Linux / WSL / Git Bash：

```bash
gh api -H "Accept: application/vnd.github.raw" \
  /repos/wdmcheng/agent-pack/contents/install.sh | \
  bash -s -- --repo git@github.com:wdmcheng/agent-pack.git install
```

指定本地能力包目录：

```bash
gh api -H "Accept: application/vnd.github.raw" \
  /repos/wdmcheng/agent-pack/contents/install.sh | \
  bash -s -- --repo git@github.com:wdmcheng/agent-pack.git --dir ~/.agent-packs/vibe install
```

也可以用环境变量指定默认远端：

```bash
export AGENT_PACK_REMOTE=git@github.com:wdmcheng/agent-pack.git
gh api -H "Accept: application/vnd.github.raw" \
  /repos/wdmcheng/agent-pack/contents/install.sh | bash
```

Windows PowerShell 原生：

```powershell
$installer = Join-Path $env:TEMP "agent-pack-install.ps1"
gh api -H "Accept: application/vnd.github.raw" /repos/wdmcheng/agent-pack/contents/install.ps1 |
  Set-Content -Encoding UTF8 $installer
powershell -ExecutionPolicy Bypass -File $installer --repo git@github.com:wdmcheng/agent-pack.git install
```

Windows cmd 原生：

```bat
gh api -H "Accept: application/vnd.github.raw" /repos/wdmcheng/agent-pack/contents/install.ps1 > "%TEMP%\install.ps1"
gh api -H "Accept: application/vnd.github.raw" /repos/wdmcheng/agent-pack/contents/install.cmd > "%TEMP%\agent-pack-install.cmd"
"%TEMP%\agent-pack-install.cmd" --repo git@github.com:wdmcheng/agent-pack.git install
```

如果本机 SSH 走公司代理、跳板机或 `~/.ssh/config`，先确认 `git clone git@github.com:wdmcheng/agent-pack.git` 能成功；bootstrap 不会替你改 SSH 配置。

三个 bootstrap 入口只负责 clone 或 pull 能力包。后续所有项目内操作都用对应平台的 `agent-pack`、`agent-pack.ps1` 或 `agent-pack.cmd`。

## 日常命令

### 查看状态

```bash
./agent-pack status
```

用于判断项目本地副本、lock 和当前 Agent Pack 是否一致。
如果项目存在 `openspec/`，还会显示 OpenSpec 是否存在、active changes、artifact 进度和 `openspec validate --all --json` 摘要。未安装 OpenSpec CLI 时只做目录级检测，不让状态命令失败。

常见状态：

- `clean`：项目副本和能力包一致。
- `local-modified`：项目本地文件相对安装时发生了修改。
- `pack-updated`：能力包里的对应文件已经更新。
- `missing-local`：项目本地文件缺失。
- `missing-pack`：能力包中对应源文件缺失。
- `local-symlink`：本该是普通副本的文件变成了 symlink。

## 可选 OpenSpec 集成

OpenSpec 在 Agent Pack 里是可选的变更契约层，不是第二套产品流程。

三类产物的边界：

- `Product-Spec.md`：产品级真相源，回答产品为什么存在、给谁、范围和验收是什么。
- `DEV-PLAN.md`：阶段级实施计划，回答从当前状态到可发布状态分哪些 Phase 和 Task。
- `openspec/changes/<change>/`：单次变更契约，回答这一次行为变化是什么、哪些 delta specs 要归档、任务怎么验证。

自定义 OpenSpec schema 的作用是控制 OpenSpec change 里的 artifacts、模板和依赖顺序。它会在创建或推进 change 时用到，比如：

```bash
openspec new change add-billing --schema agent-pack-product-change
openspec status --change add-billing --json
openspec validate add-billing --type change --strict
```

`agent-pack install --with-openspec` 会在安全场景下把目标项目默认 schema 设为：

```yaml
schema: agent-pack-product-change
```

当 `openspec/config.yaml` 不存在、没有 `schema:`，或当前是 `schema: spec-driven` 时会自动设置；如果已经是其他自定义 schema，安装脚本不会覆盖，只会提示手动切换命令。之后 `openspec new change <name>` 会自动使用该 schema。它延续 OpenSpec 默认 `spec-driven` 的 proposal/specs/design/tasks artifact 图和 `tasks.md` apply tracking，只在项目级 schema 里做轻量适配，不改变 OpenSpec 内置默认 schema。它生成和约束的是 `proposal.md`、`specs/**/*.md`、`design.md`、`tasks.md` 这些 change artifacts：proposal 集中连接 Product-Spec / DEV-PLAN 等上游来源，spec 只写行为 delta，design 只写实现取舍和测试 seam，tasks 只把真实实现/验证工作写成 checkbox。Agent Pack 的读上下文、TDD、review gate、dev-builder 纪律放在 apply 阶段执行，不作为 tasks group。

Agent Pack 的 OpenSpec 生命周期门禁是开发期自动验证、最终可选归档：change 草案完成后 strict validate；实现完成且 tasks 全勾后再次 strict validate。主规格同步优先交给 OpenSpec 原生命令 `openspec archive <change>`，它负责验证、合并 delta specs 并归档；在 Agent 会话中也可用 `/opsx:archive <change>`。archive 是整体收口后的可选动作，不作为每个窄 change 的等待用户决策点。若用户执行 archive，archive 后应再检查 active changes 并全量 strict validate。

什么时候用：

- 已有 `Product-Spec.md` 和 `DEV-PLAN.md`，现在做一个增量功能或行为变更。
- 这次变更需要留下可归档的 delta spec，或者后续要用 `openspec archive` 合回行为契约。
- 你希望 Agent 在 dev-builder 前先读 proposal/spec/design/tasks，而不是直接从口头描述改代码。

什么时候不用：

- 0-1 需求还没说清，先走 `product-spec-builder`。
- DEV-PLAN 还没拆出来，先走 `dev-planner`。
- 小 bug 或纯内部修复不需要行为归档，直接用 `bug-fixer` 就够。

安装 schema：

```bash
/path/to/agent-pack install --with-openspec
```

已有 `openspec/` 的项目会得到：

```text
openspec/schemas/agent-pack-product-change/
├── schema.yaml
└── templates/
    ├── proposal.md
    ├── spec.md
    ├── design.md
    └── tasks.md
```

没有 `openspec/` 的项目不会被自动初始化。先按 OpenSpec 自己的方式初始化，再重新执行 opt-in 安装。

### 查看差异

```bash
./agent-pack diff
./agent-pack diff AGENTS.md
./agent-pack diff .agents/skills/dev-builder/SKILL.md
```

`diff` 用于审阅项目本地副本和能力包之间的差异。它不会写文件。

不要直接把 `agent-pack diff` 的输出拿去 `promote --patch`。`diff` 是审阅视图，升格 patch 必须由主 Agent 整理成只包含已确认升格内容、且路径相对能力包根目录的最小 patch。

### 更新项目本地副本

```bash
./agent-pack update
```

用于把能力包里的新版本同步到项目。

规则：

- lock 记录为干净的文件会被能力包新版本覆盖。
- 项目本地已修改的文件不会被覆盖。
- 缺失的安装文件会补回。
- evolution 目录和 symlink 会被修正到当前结构。

更新前建议先跑 `status`。更新后再跑一次 `status`，确认哪些文件仍需人工处理。

## 升格流程

升格是把项目中验证过的通用改动写回 Agent Pack。它不是自动同步，也不是默认整文件覆盖。

### 什么时候考虑升格

适合升格：

- 多个项目都会受益的规则。
- 已经稳定复用的 Skill 行为。
- 与具体业务无关的 hook 门禁。
- Codex 和 Claude Code 都需要共享的适配方式。
- 反复出现的通用失败模式。

不适合升格：

- 某个项目的启动命令、技术栈、目录习惯。
- 某个产品的业务规则、领域术语、特定验收标准。
- 临时约束。
- 用户个人长期偏好。个人长期偏好应进入用户记忆或个人全局配置。

### 标准升格路径

1. 主 Agent 或 `evolution-engine` 判断某个改动是否跨项目通用。
2. 主 Agent 给出升格理由、影响文件、最小改动摘要。
3. 用户明确同意升格。
4. 主 Agent 生成相对 Agent Pack 根目录的最小 patch。
5. 执行：

```bash
./agent-pack promote --patch /path/to/promote.patch
```

patch 路径必须类似：

```text
a/AGENTS.md
b/AGENTS.md
a/.agents/skills/dev-builder/SKILL.md
b/.agents/skills/dev-builder/SKILL.md
```

脚本会拒绝绝对路径、`..` 路径、临时目录路径，以及不属于能力包可升格范围的路径。

### 预览候选差异

```bash
./agent-pack promote
./agent-pack promote AGENTS.md
```

默认 `promote` 只提示和预览，不写能力包。

### 整文件替换

```bash
./agent-pack promote --replace .agents/skills/new-skill/SKILL.md
```

只在两种情况使用：

- 新文件整体升格。
- 用户明确批准整文件替换。

不要把普通规则微调用 `--replace` 升格。

## 迁移能力包路径

项目根目录有 `agent-pack` launcher，`.agents/agent-pack.lock.json` 记录真实能力包来源。能力包换路径后，可以用新能力包目录里的脚本执行迁移，也可以用项目 launcher 显式指定新目录。

如果能力包从 A 目录移动到 B 目录，在项目根目录执行：

```bash
/path/to/new-agent-pack/agent-pack migrate
```

`migrate` 默认把项目 lock 更新为当前脚本所在的能力包目录。命令会更新项目的 `.agents/agent-pack.lock.json`，并修正 evolution 目录和 symlink。它不会自动更新业务代码。

如果必须用某个脚本去指定另一个能力包目录，才使用：

```bash
./agent-pack migrate --pack /path/to/target-agent-pack
```

`--pack` 的参数是能力包目录，不是 `agent-pack` 脚本文件。通常迁移到新目录时不需要传它。

## 提交和推送能力包

升格写回能力包后，提交能力包：

```bash
./agent-pack pack-commit -m "promote shared agent rule"
```

推送能力包：

```bash
./agent-pack pack-push
```

`pack-commit` 只提交能力包相关文件，包括 `README.md`、`AGENTS.md`、`.agents/`、`.codex/`、`.claude/`、`agent-pack`、`agent-pack.ps1`、`agent-pack.cmd`、`install.sh`、`install.ps1`、`install.cmd`。

不要在没有审阅 diff 的情况下提交能力包。

## 推荐工作流

### 新项目

1. 创建项目仓库。
2. 在项目根目录运行 `agent-pack install`。
3. 提交生成的 agent 配置。
4. 开始产品流程：`product-spec-builder` → `design-brief-builder` → `design-maker` → `dev-planner` → `dev-builder`。

### 已安装项目更新能力包

1. 在项目根目录运行 `agent-pack status`。
2. 如果只有 `pack-updated` 或 clean 文件，运行 `agent-pack update`。
3. 如果有 `local-modified`，先看 `agent-pack diff`，决定保留项目改动、手动合并，或升格。
4. 验证项目行为。
5. 提交项目内变更。

### 多项目关联

当前端、后端、框架、中间件等被拆在多个仓库，但又属于同一个产品或交付链路时，用关联项目功能。

关联项目会写两个文件：

- `.agents/RELATED-PROJECTS.md`：可提交，记录项目组、当前项目、关联项目、角色和 Agent 读取规则。
- `.agents/related-projects.local.json`：不可提交，记录本机绝对路径。安装时它会进入 `.gitignore`；`relate` 也会把它加入当前仓库的 `.git/info/exclude` 作为额外保护。

多项目首次安装时，`agent-pack install` 会自动进入关联信息录入。

已安装的单项目后期要关联为多项目，在当前项目根目录执行：

```bash
./agent-pack relate
```

如果项目已经从仓库检出并带有 `.agents/RELATED-PROJECTS.md`，脚本会先展示该文件，默认沿用它，只询问本机路径；需要重写关联说明时输入 `n`，再进入完整询问流程。

完整询问流程会要求输入其他项目路径，并逐个询问：

- 项目 id：稳定短名，例如 `web`、`api`、`admin`。
- 角色/类型：例如 `frontend`、`backend`、`framework`、`middleware`。
- 项目说明：可空，用于说明职责边界。

只拆除当前项目的关联关系：

```bash
./agent-pack unrelate
```

把本机仍可访问的所有关联项目都拆回单项目：

```bash
./agent-pack unrelate --all
```

拆除只删除关联说明和本机路径表，不删除 Agent Pack 安装内容，也不删除项目代码。

### 项目内进化

1. 用户纠正或指出问题。
2. hook 写入 `.agents/evolution/signals.jsonl`。
3. session 启动时主 Agent 派 `evolution-runner` 消化信号。
4. 主 Agent 当场逐条问用户。
5. 用户同意后先改项目本地文件。
6. 如确实跨项目通用，再走升格流程。

## Skills 使用时机

### product-spec-builder

用户表达产品想法、要做一个工具、增加功能、修改需求、调整 UI 时使用。

输出或更新 `Product-Spec.md` 和变更记录。没有需求文档时走 0-1 访谈；已有需求文档时走迭代模式。

### design-brief-builder

用户需要确定视觉方向、设计风格，或说“高级感”“简洁”“现代”等模糊描述时使用。

前置是 `Product-Spec.md`。输出 `Design-Brief.md`，供设计工具和开发实现参照。

### design-maker

`Product-Spec.md` 和 `Design-Brief.md` 已完成，用户要生成设计稿时使用。

需要设计工具 MCP。负责生成页面、状态变体、组件规范和设计变量。

### dev-planner

`Product-Spec.md` 已完成，需要把需求拆成开发 Phase 时使用。

输出或更新 `DEV-PLAN.md`。Spec 变更后也用它分析影响范围，调整开发计划。

### dev-builder

`DEV-PLAN.md` 就绪，用户要开始写代码、继续下一个 Phase、搭建新项目骨架或实现功能时使用。

它按 Phase 开发，改前评估影响，改后跑验证，并进入 review/fix 循环。项目存在 OpenSpec change、`CONTEXT.md` 或 ADR 时会作为可选输入读取；新增测试先确认公开 seam，不测私有实现。

### bug-fixer

用户报告 bug、报错、功能异常、编译错误、运行时异常，或 code-review 报出缺陷时使用。

它先建立能抓住用户原始症状的 red-capable 反馈回路，再复现、最小化、定位根因、一次修一个问题，并做回归验证。

### code-review

用户要求审查代码，或功能开发完成需要验证实现质量时使用。

它保留 Stage 1 / Stage 2 门禁，同时按 Spec 轴和 Standards 轴分别汇报：前者对照 `Product-Spec.md`、`DEV-PLAN.md`、设计稿和 OpenSpec change，后者对照项目规范、测试、安全和代码质量。修复由主 Agent 再派 `dev-builder` 或 `bug-fixer` 执行。

### release-builder

用户要打包、部署、发布、上线，或项目准备交付时使用。

它负责构建、发布前检查、隐私审计、安装后或部署后冒烟测试。

### goal-creator

用户想把一个完整目标交给 `/goal` 自驱执行时使用。

它只生成 `/goal` 指令，由用户自己发送，不替用户触发 slash command。

### skill-builder

用户要创建新 Skill，或 `EVOLUTION.md` 提议创建新 Skill 且用户确认时使用。

它按现有 Skill 风格创建 `SKILL.md`，要求关键步骤有 completion criterion，长参考做 progressive disclosure，去掉重复和 no-op，必要时创建 templates，并维护 Claude skills symlink。

### evolution-engine

session 启动时发现 `.agents/evolution/signals.jsonl` 有信号，或用户手动要求重新消化进化建议时使用。

它只消化信号并生成建议，不替用户决定，不自行落地。升格能力包时，它必须写明跨项目通用性和最小 patch 方案。

## 目录维护原则

- 项目代码结构由项目技术栈决定，Agent Pack 不规定。
- `.agents/skills/` 是 Skill 正文唯一维护位置。
- `.agents/cli/` 是 Agent Pack CLI 业务逻辑唯一维护位置，根目录各 shell 入口只转发。
- `.agents/hooks/` 是 hook 业务逻辑唯一维护位置，Python runner 为主，shell 包装只转发 hook name 和 agent 来源。
- `.claude/skills/` 只是 symlink 暴露层。
- `.agents/evolution/` 是自进化队列源目录。
- `.codex/evolution` 和 `.claude/evolution` 只做入口 symlink。
- `.agents/RELATED-PROJECTS.md` 是可提交的多项目关系说明。
- `.agents/related-projects.local.json` 只记录本机路径，不提交。
- `.agents/evolution/signals.jsonl`、`.agents/evolution/proposals.md` 和 `.agents/.needs-review` 是本地运行状态，不提交。
- `AGENTS.md` 是共享主控规则；Claude Code 通过 `.claude/CLAUDE.md` 的 `@../AGENTS.md` 读取。
- 平台专属配置只放对应平台目录：Codex 放 `.codex/`，Claude Code 放 `.claude/`。

## 故障处理

### 缺少 lock

报错：

```text
缺少 .agents/agent-pack.lock.json
```

说明项目还没有安装，先运行：

```bash
/path/to/agent-pack install
```

### update 后仍有 local-modified

这是正常保护。说明项目本地文件被改过，脚本不会覆盖。用：

```bash
./agent-pack diff <path>
```

审阅后决定保留、手动合并或升格。

### promote --patch 被拒绝

常见原因：

- patch 路径是绝对路径。
- patch 路径来自临时目录。
- patch 包含 `..`。
- patch 改了能力包允许范围外的文件。

重新生成相对能力包根目录的最小 patch。

### Claude 看不到 Skill

检查：

```bash
ls -la .claude/skills
```

每个 Skill 应该是指向 `../../.agents/skills/<skill-name>` 的 symlink。缺失时运行：

```bash
./agent-pack update
```

### evolution 队列不一致

检查：

```bash
ls -la .agents/evolution .codex/evolution .claude/evolution
```

`.codex/evolution` 和 `.claude/evolution` 应该都指向 `../.agents/evolution`。不一致时运行：

```bash
./agent-pack update
```
