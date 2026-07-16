"""Typed config loader 的公开契约测试。

这些用例故意穿过 `load_settings` seam，而不是测试私有 helper：调用方只关心
profile/agent/env 合并后的 typed settings，以及错误是否能变成可操作诊断。
"""

from __future__ import annotations

import os
import traceback
from pathlib import Path

import pytest

from agent_harness.config import SettingsLoadError, load_settings
from agent_harness.config import secret_files as secret_files_module

ROOT = Path(__file__).resolve().parents[2]
SERVICE_APP = ROOT / "templates" / "service-app"
PROFILES = SERVICE_APP / "configs" / "profiles"


__all__ = [
    "PROFILES",
    "Path",
    "ROOT",
    "SERVICE_APP",
    "SettingsLoadError",
    "load_settings",
    "os",
    "pytest",
    "secret_files_module",
    "traceback",
]
