"""运行期 hook：进化信号、自动 push、端口清理和提交前检查。"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

CORRECTION_PHRASES = (
    "不是这样",
    "不是这个意思",
    "不应该",
    "搞错",
    "你错",
    "又错",
    "理解错",
    "弄错",
    "不合理",
    "不通用",
    "不对劲",
    "这不对",
    "完全不对",
    "去掉",
    "删掉",
    "删除",
    "改成",
    "换成",
    "改为",
    "不需要",
    "没必要",
    "多余",
    "你漏",
    "漏掉",
    "漏了",
    "你忘",
    "忘了",
    "没提到",
    "没有提到",
    "你没提",
    "少了",
    "每次都",
    "怎么又",
    "怎么还",
    "我说过",
    "说过了",
    "提醒过",
    "强调过",
    "不是让你",
    "没复用",
    "你没按",
    "没生效",
    "没有生效",
    "没执行",
    "不喜欢",
    "不太喜欢",
    "我的意思是",
    "我是说",
    "其实应该",
    "应该是",
    "应该写",
)


def run(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """执行外部命令并把 OS 层失败收敛成 CompletedProcess。"""

    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def project_root() -> Path:
    """优先使用 agent 注入的项目目录，兜底读取 git 根目录。"""

    for name in (
        "AGENT_PACK_PROJECT_DIR",
        "CLAUDE_PROJECT_DIR",
        "CODEX_PROJECT_DIR",
        "PROJECT_DIR",
    ):
        value = os.environ.get(name)
        if value:
            return Path(value).resolve()
    result = run(["git", "rev-parse", "--show-toplevel"])
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return Path.cwd().resolve()


def read_payload() -> dict:
    """读取 hook stdin JSON；无法解析时返回空 payload。"""

    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def command_from(data: dict, agent: str = "") -> str:
    """从 Codex/Claude 不同 payload 形状中提取 shell command。"""

    tool_input = data.get("tool_input")
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        return tool_input["command"]
    if agent == "claude":
        return ""
    command = data.get("command")
    return command if isinstance(command, str) else ""


def prompt_from(data: dict) -> str:
    """读取用户 prompt，用于轻量进化信号识别。"""

    prompt = data.get("prompt")
    return prompt if isinstance(prompt, str) else ""


def npx_command() -> str:
    """返回当前平台可用的 npx 命令名。"""

    candidates = ["npx.cmd", "npx"] if os.name == "nt" else ["npx"]
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    return "npx"


def should_skip_feedback_prompt(prompt: str) -> bool:
    """跳过主 Agent 派给固定子 Agent 的审查提示。"""

    is_agent_prompt = re.search(r"你是[^\n]*(code-reviewer|evolution-runner)", prompt) is not None
    has_review_words = any(
        word in prompt
        for word in (
            "必须先阅读",
            "重点审查",
            "输出要求",
            "不要修改",
            "不做任何文件编辑",
        )
    )
    return is_agent_prompt and has_review_words


def evolution_dir(root: Path, agent: str) -> Path:
    """解析当前 agent 应使用的共享进化目录。"""

    if agent in {"claude", "codex"}:
        platform_dir = root / f".{agent}" / "evolution"
        if platform_dir.exists() or (root / f".{agent}").exists():
            return platform_dir
    return root / ".agents" / "evolution"


def detect_feedback_signal(root: Path, data: dict, agent: str = "") -> int:
    """把明显的用户纠正追加到共享进化信号队列。"""

    prompt = prompt_from(data)
    if not prompt or should_skip_feedback_prompt(prompt):
        return 0
    if any(phrase in prompt for phrase in CORRECTION_PHRASES):
        queue = evolution_dir(root, agent) / "signals.jsonl"
        queue.parent.mkdir(parents=True, exist_ok=True)
        with queue.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"type": "correction", "prompt": prompt},
                    ensure_ascii=False,
                )
                + "\n"
            )
    return 0


def check_evolution(root: Path, agent: str = "") -> int:
    """提示主 Agent 先消化待处理进化信号或建议。"""

    evo = evolution_dir(root, agent)
    proposals = evo / "proposals.md"
    signals = evo / "signals.jsonl"
    msg = ""
    if proposals.exists():
        pending = False
        count = 0
        for line in proposals.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("## 待审阅"):
                pending = True
                continue
            if line.startswith("## "):
                pending = False
            if pending and line.startswith("- "):
                count += 1
        if count > 0:
            msg = f"📋 有 {count} 条进化建议待确认，session 启动我会逐条摆给你问同不同意。"
    if signals.exists() and signals.stat().st_size > 0:
        msg = f"{msg} 🔄 有新进化信号，session 启动我会扫一遍、消化成建议并逐条问你。"
    if msg:
        print(msg)
    return 0


def auto_push(root: Path, data: dict, agent: str = "") -> int:
    """commit 后尝试 push 非保护分支。"""

    command = command_from(data, agent)
    if agent == "codex" and "git commit" not in command:
        return 0
    if agent != "codex" and command and "git commit" not in command:
        return 0
    branch = run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"])
    name = branch.stdout.strip() if branch.returncode == 0 else ""
    if name in ("main", "master"):
        print(
            f"⚠️ 当前在 {name} 分支，已跳过自动 push。保护分支需手动 push 或走 PR。",
            file=sys.stderr,
        )
        return 0
    if not name:
        return 0
    pushed = run(["git", "-C", str(root), "push"])
    if pushed.returncode != 0:
        print("❌ 自动 push 失败，请手动检查：", file=sys.stderr)
        print((pushed.stdout + pushed.stderr).strip(), file=sys.stderr)
    return 0


def kill_ports(ports: tuple[int, ...]) -> None:
    """启动前清理常见前端开发端口。"""

    if os.name == "nt":
        if not shutil.which("powershell"):
            return
        for port in ports:
            ps = (
                f"$ids=(Get-NetTCPConnection -LocalPort {port} "
                "-ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty OwningProcess -Unique); "
                "foreach($id in $ids){ "
                "Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return
    if not shutil.which("lsof"):
        return
    for port in ports:
        result = run(["lsof", f"-ti:{port}"])
        for pid in result.stdout.split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (OSError, ValueError):
                pass


def find_tsconfig(root: Path) -> Path | None:
    """向下查找最近的 TypeScript 配置，限制深度避免扫完整仓库。"""

    skip = {"node_modules", ".next", ".git"}
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        try:
            depth = len(current.relative_to(root).parts)
        except ValueError:
            continue
        dirnames[:] = [name for name in dirnames if name not in skip]
        if depth >= 3:
            dirnames[:] = []
        if "tsconfig.json" in filenames:
            return current / "tsconfig.json"
    return None


def pre_tool_shell(root: Path, data: dict, agent: str = "") -> int:
    """shell 执行前做确定性环境清理和提交前 TypeScript 检查。"""

    command = command_from(data, agent)
    if any(token in command for token in ("pnpm dev", "npm run dev", "yarn dev")):
        kill_ports((3000, 3001, 4173, 5173, 8080))
    if "git commit" not in command:
        return 0
    tsconfig = find_tsconfig(root)
    if not tsconfig:
        return 0
    checked = run([npx_command(), "tsc", "--noEmit"], cwd=tsconfig.parent)
    if checked.returncode != 0:
        print(
            "编译检查未通过，commit 被阻止。请修复以下错误：",
            file=sys.stderr,
        )
        print((checked.stdout + checked.stderr).strip(), file=sys.stderr)
        return 2
    return 0
