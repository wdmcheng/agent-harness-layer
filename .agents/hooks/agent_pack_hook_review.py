from __future__ import annotations

import json
from pathlib import Path

CONFIG_NAMES = {
    "Dockerfile",
    "Makefile",
    "makefile",
    "GNUmakefile",
    "Procfile",
    "Brewfile",
    "Gemfile",
    "Rakefile",
    "Jenkinsfile",
    "Justfile",
    "Taskfile.yml",
    "Taskfile.yaml",
    "package.json",
    "tsconfig.json",
    "jsconfig.json",
    "turbo.json",
    "pnpm-workspace.yaml",
    "pnpm-workspace.yml",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "go.sum",
    "pom.xml",
}

CONFIG_PATTERNS = (
    "Dockerfile.",
    "tsconfig.",
    "jsconfig.",
    "docker-compose.",
    "compose.",
    "build.gradle",
    "settings.gradle",
    "next.config.",
    "nuxt.config.",
    "vite.config.",
    "vitest.config.",
    "jest.config.",
    "playwright.config.",
    "webpack.config.",
    "rollup.config.",
    "tailwind.config.",
    "postcss.config.",
    "eslint.config.",
    "prettier.config.",
)

NON_CODE_SUFFIXES = (
    ".md",
    ".txt",
    ".lock",
    ".log",
    ".gitignore",
    ".prettierrc",
    ".eslintrc",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
)


def clean_candidate_path(raw: str) -> str | None:
    path = raw.split("\t", 1)[0].strip().strip('"')
    if path in ("", "/dev/null"):
        return None
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return path


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_config_file(name: str) -> bool:
    return name in CONFIG_NAMES or any(name.startswith(pattern) for pattern in CONFIG_PATTERNS)


def is_code_file(path: Path, root: Path) -> bool:
    try:
        absolute = (root / path).resolve() if not path.is_absolute() else path.resolve()
    except OSError:
        return False
    if not is_relative_to(absolute, root):
        return False
    if any(part in {".claude", ".codex", ".agents"} for part in absolute.parts):
        return False
    name = absolute.name
    if is_config_file(name):
        return True
    if "." not in name:
        return False
    if name == ".env" or name.startswith(".env."):
        return False
    return not name.endswith(NON_CODE_SUFFIXES)


def iter_patch_paths(text: str):
    for line in text.splitlines():
        for prefix in ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: "):
            if line.startswith(prefix):
                yield line[len(prefix) :]
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                yield parts[2]
                yield parts[3]
        elif line.startswith("--- ") or line.startswith("+++ "):
            yield line[4:]


def iter_payload_paths(data: dict):
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    for key in ("file_path", "path", "filename"):
        value = tool_input.get(key)
        if isinstance(value, str):
            yield value
    files = tool_input.get("files")
    if isinstance(files, list):
        for item in files:
            if isinstance(item, str):
                yield item
            elif isinstance(item, dict):
                for key in ("file_path", "path"):
                    value = item.get(key)
                    if isinstance(value, str):
                        yield value
    for key in ("patch", "input", "command"):
        value = tool_input.get(key)
        if isinstance(value, str):
            yield from iter_patch_paths(value)
    for key in ("patch", "input"):
        value = data.get(key)
        if isinstance(value, str):
            yield from iter_patch_paths(value)


def mark_review_needed(root: Path, data: dict, agent: str = "") -> int:
    state_file = root / ".agents" / ".needs-review"
    if agent == "claude":
        tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        file_path = tool_input.get("file_path")
        if isinstance(file_path, str) and is_claude_code_file(file_path, root):
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text("needs_review\n", encoding="utf-8")
        return 0
    for raw in iter_payload_paths(data):
        path = clean_candidate_path(raw)
        if path and is_code_file(Path(path), root):
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text("needs_review\n", encoding="utf-8")
            break
    return 0


def is_claude_code_file(file_path: str, root: Path) -> bool:
    path = Path(file_path)
    if not path.is_absolute():
        return False
    return is_code_file(path, root)


def stop_gate(root: Path) -> int:
    state_file = root / ".agents" / ".needs-review"
    if not state_file.exists():
        return 0
    state = state_file.read_text(encoding="utf-8", errors="ignore").strip()
    if state in ("", "clean"):
        state_file.unlink(missing_ok=True)
        return 0
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    "代码已修改但未通过 code review。请派发 code-reviewer 两阶段审查，"
                    "通过后写入 clean。用 /goal 自驱时，把 code-reviewer 通过写进 /goal 完成条件。"
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0
