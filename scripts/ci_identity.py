"""计算 CI evidence 与验收消费者共享的 Git 输入身份。"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


class InputIdentityError(RuntimeError):
    """表示 Git 输入身份无法完整、确定地计算。"""


def _git_text(repo: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise InputIdentityError(f"git {' '.join(args)} failed: {exc}") from exc
    if completed.returncode != 0:
        raise InputIdentityError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def _git_bytes(repo: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    except OSError as exc:
        raise InputIdentityError(f"git {' '.join(args)} failed: {exc}") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode(errors="replace")
        raise InputIdentityError(f"git {' '.join(args)} failed: {stderr.strip()}")
    return completed.stdout


def dirty_diff_bytes(repo: Path) -> bytes:
    """合并 tracked patch 与未忽略的新文件，排除生成的 evidence。"""

    payload = bytearray(_git_bytes(repo, "diff", "--binary", "HEAD"))
    untracked = _git_bytes(repo, "ls-files", "--others", "--exclude-standard", "-z")
    for raw_path in sorted(item for item in untracked.split(b"\0") if item):
        relative = Path(os.fsdecode(raw_path))
        candidate = repo / relative
        mode = candidate.lstat().st_mode & 0o7777
        if candidate.is_symlink():
            kind = b"symlink"
            content = os.fsencode(os.readlink(candidate))
        elif candidate.is_file():
            kind = b"file"
            content = candidate.read_bytes()
        else:
            raise InputIdentityError(
                f"untracked input is not a regular file or symlink: {relative}"
            )
        payload.extend(b"\0UNTRACKED\0")
        payload.extend(len(raw_path).to_bytes(8, "big"))
        payload.extend(raw_path)
        payload.extend(mode.to_bytes(4, "big"))
        payload.extend(len(kind).to_bytes(1, "big"))
        payload.extend(kind)
        payload.extend(len(content).to_bytes(8, "big"))
        payload.extend(content)
    return bytes(payload)


def input_identity(repo: Path) -> dict[str, str]:
    """绑定 commit、tracked diff 与所有未忽略的新文件。"""

    commit = _git_text(repo, "rev-parse", "HEAD").strip()
    diff = dirty_diff_bytes(repo)
    return {
        "commit_sha": commit,
        "dirty_diff_sha256": hashlib.sha256(diff).hexdigest(),
    }
