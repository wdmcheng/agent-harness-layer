from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from agent_harness.contracts.boundaries import (
    BANNED_VENDOR_IMPORTS,
    is_vendor_import_allowed,
)

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


def test_boundary_contract_lists_banned_vendors_and_adapter_allowlist() -> None:
    assert {"pydantic_ai", "dbos", "logfire", "phoenix", "langfuse"} <= BANNED_VENDOR_IMPORTS
    assert is_vendor_import_allowed(
        ROOT / "packages" / "agent-harness" / "src" / "agent_harness" / "adapters" / "model.py"
    )
    assert not is_vendor_import_allowed(
        ROOT / "templates" / "service-app" / "app" / "api" / "run.py"
    )


def test_doctor_cli_reports_local_profile_without_provider_keys() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "doctor",
            "--profile",
            "local",
            "--profiles-dir",
            str(PROFILES),
        ],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "profile: local" in result.stdout
    assert "storage: filesystem" in result.stdout
    assert "queue: in-memory" in result.stdout
    assert "model: fake (api key not required)" in result.stdout


def test_doctor_cli_reports_profile_errors_with_nonzero_exit(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text(
        """
profile: broken
queue:
  kind: in-memory
observability:
  kind: local-jsonl
policy:
  provider: yaml
model:
  provider: fake
  requires_api_key: false
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "doctor",
            "--profile",
            "broken",
            "--profiles-dir",
            str(tmp_path),
        ],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "config.invalid" in result.stderr
    assert "field=storage" in result.stderr
