"""代码和质量门禁共享的 import 边界声明。"""

from __future__ import annotations

from pathlib import Path

BANNED_VENDOR_IMPORTS = {
    "dbos",
    "langfuse",
    "logfire",
    "phoenix",
    "pydantic_ai",
}

# 允许的不是具体文件，而是目录职责：provider 只能藏在 adapter/integration 后面。
APPROVED_VENDOR_IMPORT_PARTS = {
    "adapters",
    "integrations",
}


def is_vendor_import_allowed(path: Path) -> bool:
    """判断文件路径是否处在批准的 vendor 边界后面。

    allowlist 按路径片段表达职责：未来 adapters 和受控 integrations 可以
    import provider SDK；核心契约、模板入口、示例、脚本和测试应保持
    vendor-neutral。
    """

    return bool(APPROVED_VENDOR_IMPORT_PARTS & set(path.parts))
