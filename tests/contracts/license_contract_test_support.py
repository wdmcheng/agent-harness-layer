"""许可证门禁合同测试的隔离仓库夹具。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "license_check.py"
POSTGRES_IMAGE = (
    "postgres:18.4@sha256:3a82e1f56c8f0f5616a11103ac3d47e632c3938698946a7ad26da0df1334744a"
)
REDIS_IMAGE = "redis:7.2.14@sha256:f0707c78ea880b293ccdeb410c9c0a8ccae93fe7128799b751333a698b0a39a7"
REDIS_LICENSE_BASIS = "https://raw.githubusercontent.com/redis/redis/7.2.14/COPYING"
REDIS_SECURITY_BASIS = "https://github.com/redis/redis/releases/tag/7.2.14"
NOTICE_RUNTIME_BOUNDARY = f"""
Service runtime evidence and license boundary:
- PostgreSQL actual server version: 18.4
- PostgreSQL security basis: https://www.postgresql.org/support/security/
- Redis actual server version: 7.2.14
- Redis security advisory basis: {REDIS_SECURITY_BASIS}
- Redis server license boundary: BSD-3-Clause
- redis-py client license boundary: MIT
"""


def sha256_text(value: str) -> str:
    """返回 fixture 字段使用的稳定 SHA-256。"""

    return hashlib.sha256(value.encode()).hexdigest()


def write_minimal_repository(root: Path) -> None:
    """创建不依赖当前 checkout 状态的最小合规仓库。"""

    (root / "compliance").mkdir(parents=True)
    (root / "templates/service-app").mkdir(parents=True)
    (root / "docs/adr").mkdir(parents=True)
    (root / "LICENSE").write_text(
        "Apache License\nVersion 2.0, January 2004\nhttp://www.apache.org/licenses/\n",
        encoding="utf-8",
    )
    (root / "NOTICE").write_text(
        "Fixture notices. Current repository has no vendored source.\n" + NOTICE_RUNTIME_BOUNDARY,
        encoding="utf-8",
    )
    (root / "templates/service-app/docker-compose.yml").write_text(
        "services:\n"
        "  postgres:\n"
        f"    image: ${{SERVICE_APP_POSTGRES_IMAGE:-{POSTGRES_IMAGE}}}\n"
        "  redis:\n"
        f"    image: ${{SERVICE_APP_REDIS_IMAGE:-{REDIS_IMAGE}}}\n",
        encoding="utf-8",
    )
    evidence = root / ".artifacts/license/smoke-service.log"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        json.dumps(
            {
                "schema_version": "service-smoke-evidence/v1",
                "status": "pass",
                "images": {
                    "postgres": {"reference": POSTGRES_IMAGE, "server_version": "18.4"},
                    "redis": {"reference": REDIS_IMAGE, "server_version": "7.2.14"},
                },
                "checks": {"streams": True, "xautoclaim": True, "recovery": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def write_lock(root: Path, *, version: str = "1.0.0") -> None:
    """写入包含一项核心运行时依赖的 uv lock fixture。"""

    (root / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.12"\n\n'
        "[[package]]\n"
        'name = "agent-harness"\n'
        'version = "0.1.0"\n'
        'source = { editable = "packages/agent-harness" }\n'
        'dependencies = [{ name = "fixture-package" }]\n\n'
        "[[package]]\n"
        'name = "fixture-package"\n'
        f'version = "{version}"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )


def write_observation(
    root: Path,
    *,
    version: str = "1.0.0",
    license_name: str = "MIT",
) -> Path:
    """写入 licensecheck JSON，避免 fixture 合同依赖实时网络。"""

    path = root / "licensecheck-observation.json"
    path.write_text(
        json.dumps(
            {
                "info": {"program": "licensecheck", "version": "2026.0.8"},
                "packages": [
                    {
                        "name": "fixture-package",
                        "version": version,
                        "license": license_name,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def write_policy(
    root: Path,
    *,
    package_version: str = "1.0.0",
    metadata_license: str = "MIT",
    license_expression: str = "MIT",
    decision: str = "allow",
    vendored: str = "",
) -> None:
    """写入覆盖 dependency、image 与可选 vendoring 的最小策略。"""

    policy = f'''schema_version = "third-party/v1"

[project]
license_expression = "Apache-2.0"
allowed_expressions = ["MIT", "Apache-2.0", "BSD-3-Clause", "Zlib"]
denied_expressions = ["GPL-3.0-only", "AGPL-3.0-only", "SSPL-1.0"]

[tool.licensecheck]
version = "2026.0.8"

[[packages]]
name = "fixture-package"
version = "{package_version}"
source = "registry:https://pypi.org/simple"
metadata_license = "{metadata_license}"
license_expression = "{license_expression}"
decision = "{decision}"
basis = "https://pypi.org/project/fixture-package/{package_version}/"

[service_images.postgres]
reference = "{POSTGRES_IMAGE}"
server_version = "18.4"
license_expression = "PostgreSQL"
basis = "https://www.postgresql.org/support/security/"
smoke_evidence = ".artifacts/license/smoke-service.log"

[service_images.redis]
reference = "{REDIS_IMAGE}"
server_version = "7.2.14"
license_expression = "BSD-3-Clause"
basis = "{REDIS_LICENSE_BASIS}"
security_basis = "{REDIS_SECURITY_BASIS}"
smoke_evidence = ".artifacts/license/smoke-service.log"
client_package = "redis"
client_license_expression = "MIT"
{vendored}
'''
    (root / "compliance/third-party.toml").write_text(policy, encoding="utf-8")


def run_check(root: Path, observation: Path) -> subprocess.CompletedProcess[str]:
    """通过公开 CLI 执行门禁，并固定 report 输出位置。"""

    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(root),
            "--metadata-observation",
            str(observation),
            "--report",
            ".artifacts/license/license-report.json",
        ],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )


def read_report(root: Path) -> dict[str, object]:
    """读取被测 CLI 原子写入的 JSON report。"""

    return json.loads((root / ".artifacts/license/license-report.json").read_text(encoding="utf-8"))


def prepared_repository(tmp_path: Path) -> tuple[Path, Path]:
    """返回可直接通过 dependency 与 image 基线的 fixture。"""

    write_minimal_repository(tmp_path)
    write_lock(tmp_path)
    write_policy(tmp_path)
    return tmp_path, write_observation(tmp_path)
