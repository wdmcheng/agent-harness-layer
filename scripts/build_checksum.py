"""为当前 `dist/` 中的 wheel/sdist 生成稳定 SHA-256 清单。"""

from __future__ import annotations

import hashlib
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    """只写入构建目录，避免把工作树历史或发布远端当作副作用目标。"""

    dist = Path("dist")
    artifacts = sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")])
    if not artifacts:
        raise SystemExit("build-checksum: dist has no wheel or sdist")
    (dist / "SHA256SUMS").write_text(
        "".join(f"{_sha256(path)}  {path.name}\n" for path in artifacts),
        encoding="utf-8",
    )
    print(f"build-checksum: wrote {dist / 'SHA256SUMS'} ({len(artifacts)} artifacts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
