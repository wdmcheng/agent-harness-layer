"""Workspace 文件边界和 `.agentignore` 规则。"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path


class WorkspaceAccessError(PermissionError):
    """路径越过 workspace 或命中 ignore 规则。"""


class WorkspacePolicy:
    """把所有文件工具路径限制在单个 workspace root 内。"""

    def __init__(self, *, root: Path, ignore_file: str = ".agentignore") -> None:
        """解析 workspace 根目录并加载简单 ignore 规则，后续路径检查均基于真实路径。"""

        self.root = root.resolve()
        self.ignore_file = ignore_file
        self._patterns = self._load_patterns()

    def resolve(self, path: str) -> Path:
        """返回 root 内部真实路径；越界或 ignored 时抛出 WorkspaceAccessError。"""

        candidate = (self.root / path).resolve()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceAccessError(f"path escapes workspace: {path}") from exc
        if self.is_ignored(relative.as_posix()):
            raise WorkspaceAccessError(f"path is denied by {self.ignore_file}: {path}")
        return candidate

    def is_ignored(self, relative_path: str) -> bool:
        """用轻量 glob 规则处理 `.agentignore`；该边界不引入 gitignore 解析器。"""

        normalized = relative_path.strip("/")
        for pattern in self._patterns:
            if fnmatch(normalized, pattern) or fnmatch(Path(normalized).name, pattern):
                return True
        return False

    def _load_patterns(self) -> list[str]:
        """读取忽略文件中的非空非注释 glob；故意不实现 gitignore 的否定和目录语义。"""

        ignore_path = self.root / self.ignore_file
        if not ignore_path.exists():
            return []
        patterns: list[str] = []
        for raw_line in ignore_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
        return patterns
