"""Import boundary declarations shared by code and quality gates."""

from __future__ import annotations

from pathlib import Path

BANNED_VENDOR_IMPORTS = {
    "dbos",
    "langfuse",
    "logfire",
    "phoenix",
    "pydantic_ai",
}

APPROVED_VENDOR_IMPORT_PARTS = {
    "adapters",
    "integrations",
}


def is_vendor_import_allowed(path: Path) -> bool:
    """Return whether a file path is allowed to import vendor SDKs."""

    return bool(APPROVED_VENDOR_IMPORT_PARTS & set(path.parts))
