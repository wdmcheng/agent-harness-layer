"""代码和质量门禁共享的 import 边界声明。"""

from __future__ import annotations

from pathlib import Path

BANNED_VENDOR_IMPORTS = {
    "dbos",
    "langfuse",
    "logfire",
    "mcp",
    "openai",
    "phoenix",
    "pydantic_ai",
}

# 当前唯一批准根是核心包 adapter。不能用任意同名路径片段放行模板或业务代码。
APPROVED_VENDOR_IMPORT_PREFIXES = {
    ("packages", "agent-harness", "src", "agent_harness", "adapters"),
}


def is_vendor_import_allowed(path: Path) -> bool:
    """判断文件路径是否处在批准的 vendor 边界后面。

    allowlist 使用仓库相对完整前缀。未来 integration root 必须单独审查并加入，
    不能让模板或业务 Agent 创建名为 adapters/integrations 的目录绕过边界。
    """

    return any(path.parts[: len(prefix)] == prefix for prefix in APPROVED_VENDOR_IMPORT_PREFIXES)
