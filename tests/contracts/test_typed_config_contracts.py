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


def test_local_and_service_profiles_load_typed_settings() -> None:
    # service profile 当前只校验部署边界形状，不启动 PostgreSQL、Redis 或 provider。
    local = load_settings(profile="local", profiles_dir=PROFILES)
    service = load_settings(profile="service", profiles_dir=PROFILES)

    assert local.profile == "local"
    assert local.storage.kind == "sqlite"
    assert local.queue.kind == "in-memory"
    assert local.observability.kind == "local-jsonl"
    assert local.model.requires_api_key is False
    assert local.identity.default.tenant_id == "default"

    assert service.profile == "service"
    assert service.service.api_process.enabled is True
    assert service.service.worker_process.enabled is True
    assert service.storage.kind == "postgresql"
    assert service.queue.kind == "redis"


def test_agent_yaml_and_env_file_override_profile_values(tmp_path: Path) -> None:
    profile_path = tmp_path / "local.yaml"
    profile_path.write_text(
        """
profile: local
storage:
  kind: filesystem
  root: .agent-harness/local
queue:
  kind: in-memory
observability:
  kind: local-jsonl
  path: .agent-harness/traces.jsonl
policy:
  provider: yaml
model:
  provider: fake
  requires_api_key: false
identity:
  default:
    tenant_id: default
    user_id: local-user
    session_id: local-session
    roles: [admin]
    permissions: ["*"]
    auth_method: local
""",
        encoding="utf-8",
    )
    agent_path = tmp_path / "agent.yaml"
    agent_path.write_text(
        """
name: research-agent
budget:
  max_tokens_per_run: 4096
tool_allowlist:
  - search
eval_dataset: eval-cases/drafts/research.yaml
delegation_edges:
  - summarizer
""",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_HARNESS_STORAGE__ROOT=.agent-harness/env\n", encoding="utf-8")

    # env file 应覆盖 profile 默认值；agent YAML 只进入 agent 子配置，不污染 profile。
    settings = load_settings(
        profile_path=profile_path,
        agent_config_path=agent_path,
        env_file=env_path,
    )

    assert settings.storage.root == ".agent-harness/env"
    assert settings.agent.name == "research-agent"
    assert settings.agent.budget.max_tokens_per_run == 4096
    assert settings.agent.tool_allowlist == ["search"]
    assert settings.agent.delegation_edges == ["summarizer"]


def test_empty_env_value_keeps_optional_setting_unconfigured(tmp_path: Path) -> None:
    profile_path = tmp_path / "local.yaml"
    profile_path.write_text(
        """
profile: local
storage:
  kind: sqlite
  dsn: "sqlite+aiosqlite:///:memory:"
queue:
  kind: in-memory
observability:
  kind: local-jsonl
  path: .agent-harness/traces.jsonl
auth:
  provider: local
  required: false
  dev_bearer_token: null
policy:
  provider: yaml
model:
  provider: fake
  requires_api_key: false
identity:
  default:
    tenant_id: default
    user_id: local-user
    session_id: local-session
    roles: [admin]
    permissions: ["*"]
    auth_method: local
""",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text("AGENT_HARNESS_AUTH__DEV_BEARER_TOKEN=\n", encoding="utf-8")

    settings = load_settings(profile_path=profile_path, env_file=env_path)

    assert settings.auth.dev_bearer_token is None


def test_test_only_dsn_env_does_not_enter_product_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 service 合同的 DSN 注入不能污染所有 AGENT_HARNESS 配置调用方。"""

    monkeypatch.setenv(
        "AGENT_HARNESS_TEST_POSTGRES_DSN",
        "postgresql+asyncpg://test:test@127.0.0.1:55433/test",
    )

    settings = load_settings(profile="local", profiles_dir=PROFILES)

    assert settings.profile == "local"
    assert settings.storage.kind == "sqlite"


def test_secret_file_maps_to_existing_typed_field_and_strips_one_line_ending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_FILE` 只提供 env 值，字段路径与 schema 仍由 typed settings 决定。"""

    trusted_root = tmp_path / "secrets"
    trusted_root.mkdir()
    secret_path = trusted_root / "storage-dsn"
    secret_path.write_text("postgresql+asyncpg://service:token@db/app \r\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_HARNESS_STORAGE__DSN_FILE", str(secret_path))
    monkeypatch.delenv("AGENT_HARNESS_STORAGE__DSN", raising=False)

    settings = load_settings(
        profile="service",
        profiles_dir=PROFILES,
        secret_root=trusted_root,
    )

    assert settings.storage.dsn == "postgresql+asyncpg://service:token@db/app "


def test_direct_and_secret_file_conflict_fails_before_reading_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部署冲突不能被 override 掩盖，也不能把任一候选值写进诊断。"""

    trusted_root = tmp_path / "secrets"
    trusted_root.mkdir()
    secret_path = trusted_root / "storage-dsn"
    secret_fixture = "file-secret-fixture"
    direct_fixture = "direct-secret-fixture"
    secret_path.write_text(secret_fixture, encoding="utf-8")
    monkeypatch.setenv("AGENT_HARNESS_STORAGE__DSN_FILE", str(secret_path))
    monkeypatch.setenv("AGENT_HARNESS_STORAGE__DSN", direct_fixture)

    def fail_if_opened(_path: object, _flags: int) -> int:
        raise AssertionError("direct/file 冲突必须在读取 secret 前失败")

    monkeypatch.setattr(secret_files_module.os, "open", fail_if_opened)

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(
            profile="service",
            profiles_dir=PROFILES,
            secret_root=trusted_root,
            overrides={"storage": {"dsn": "override-secret-fixture"}},
        )

    error = exc_info.value.errors[0]
    serialized = str(exc_info.value)
    assert error.code == "config.secret_file_conflict"
    assert error.field_path == "storage.dsn"
    assert error.hint == "只设置 direct env 或对应的 _FILE，移除另一个输入"
    for forbidden in (secret_fixture, direct_fixture, "override-secret-fixture", str(secret_path)):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "case",
    [
        "relative",
        "directory",
        "symlink",
        "outside",
        "unreadable",
        "empty",
        "non_utf8",
        "oversized",
    ],
)
def test_secret_file_rejects_untrusted_path_type_or_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """所有拒绝路径共享稳定、脱敏且可操作的字段级诊断。"""

    trusted_root = tmp_path / "secrets"
    trusted_root.mkdir()
    candidate = trusted_root / case
    outside = tmp_path / "outside-secret"
    outside.write_text("outside-secret-fixture", encoding="utf-8")
    if case == "relative":
        raw_path = "relative-secret"
    elif case == "directory":
        candidate.mkdir()
        raw_path = str(candidate)
    elif case == "symlink":
        candidate.symlink_to(outside)
        raw_path = str(candidate)
    elif case == "outside":
        raw_path = str(outside)
    elif case == "unreadable":
        candidate.write_text("unreadable-secret-fixture", encoding="utf-8")
        candidate.chmod(0)
        raw_path = str(candidate)
    elif case == "empty":
        candidate.write_bytes(b"")
        raw_path = str(candidate)
    elif case == "non_utf8":
        candidate.write_bytes(b"\xff\xfe")
        raw_path = str(candidate)
    else:
        candidate.write_bytes(b"x" * (64 * 1024 + 1))
        raw_path = str(candidate)
    monkeypatch.setenv("AGENT_HARNESS_STORAGE__DSN_FILE", raw_path)
    monkeypatch.delenv("AGENT_HARNESS_STORAGE__DSN", raising=False)

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(
            profile="service",
            profiles_dir=PROFILES,
            secret_root=trusted_root,
        )

    error = exc_info.value.errors[0]
    serialized = str(exc_info.value)
    assert error.code == "config.secret_file_invalid"
    assert error.field_path == "storage.dsn"
    assert error.hint == ("使用受信 root 内绝对、可读、非空且不超过 64 KiB 的普通 UTF-8 文件")
    for forbidden in ("outside-secret-fixture", str(outside), raw_path):
        assert forbidden not in serialized


def test_dotenv_file_reference_is_ignored_without_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_FILE` 不属于 dotenv 语法，只有进程环境能请求受控文件读取。"""

    secret_path = tmp_path / "dotenv-secret"
    secret_path.write_text("dotenv-secret-fixture", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"AGENT_HARNESS_STORAGE__DSN_FILE={secret_path}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("AGENT_HARNESS_STORAGE__DSN_FILE", raising=False)

    settings = load_settings(
        profile="service",
        profiles_dir=PROFILES,
        env_file=env_path,
        secret_root=tmp_path,
    )

    assert settings.storage.dsn is None


def test_secret_file_replacement_between_check_and_open_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """路径检查后的 inode 替换不能把攻击者内容送进 typed settings。"""

    trusted_root = tmp_path / "secrets"
    trusted_root.mkdir()
    candidate = trusted_root / "storage-dsn"
    candidate.write_text("original-secret-fixture", encoding="utf-8")
    replacement = trusted_root / "replacement"
    replacement_fixture = "replacement-secret-fixture"
    replacement.write_text(replacement_fixture, encoding="utf-8")
    monkeypatch.setenv("AGENT_HARNESS_STORAGE__DSN_FILE", str(candidate))
    monkeypatch.delenv("AGENT_HARNESS_STORAGE__DSN", raising=False)
    real_open = os.open

    def replace_then_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int
    ) -> int:
        replacement.replace(candidate)
        return real_open(path, flags)

    monkeypatch.setattr(secret_files_module.os, "open", replace_then_open)

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(
            profile="service",
            profiles_dir=PROFILES,
            secret_root=trusted_root,
        )

    assert exc_info.value.errors[0].code == "config.secret_file_invalid"
    assert replacement_fixture not in str(exc_info.value)


def test_secret_file_value_uses_existing_pydantic_field_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文件内容的 schema 错误继续使用既有 typed field path，且不回显原值。"""

    trusted_root = tmp_path / "secrets"
    trusted_root.mkdir()
    candidate = trusted_root / "api-port"
    secret_fixture = "not-a-port-secret-fixture"
    candidate.write_text(secret_fixture, encoding="utf-8")
    monkeypatch.setenv("AGENT_HARNESS_SERVICE__API_PROCESS__PORT_FILE", str(candidate))

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(
            profile="service",
            profiles_dir=PROFILES,
            secret_root=trusted_root,
        )

    error = exc_info.value.errors[0]
    assert error.code == "config.invalid"
    assert error.field_path == "service.api_process.port"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert secret_fixture not in str(exc_info.value)
    assert secret_fixture not in "".join(traceback.format_exception(exc_info.value))
    current = exc_info.value.__traceback__
    settings_frames = 0
    while current is not None:
        if current.tb_frame.f_globals.get("__name__") == "agent_harness.config.settings":
            settings_frames += 1
            assert secret_fixture not in repr(current.tb_frame.f_locals)
        current = current.tb_next
    assert settings_frames > 0


def test_config_errors_include_field_path_and_hint(tmp_path: Path) -> None:
    # 错误路径测试锁 operator-facing diagnostics，避免泄漏原始 Pydantic/YAML trace。
    profile_path = tmp_path / "broken.yaml"
    profile_path.write_text(
        """
profile: local
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

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile_path=profile_path)

    assert exc_info.value.errors[0].field_path == "storage"
    hint = exc_info.value.errors[0].hint
    assert hint is not None
    assert "profile YAML" in hint
    assert exc_info.value.to_envelope().error.code == "config.invalid"


def test_unsafe_yaml_tags_are_reported_without_construction(tmp_path: Path) -> None:
    profile_path = tmp_path / "unsafe.yaml"
    profile_path.write_text(
        '!!python/object/apply:os.system ["echo unsafe"]',
        encoding="utf-8",
    )

    # `safe_load` 必须把 Python object tags 当成配置错误，而不是构造对象。
    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile_path=profile_path)

    assert exc_info.value.errors[0].code == "config.invalid_yaml"
    assert exc_info.value.errors[0].field_path == "profile"
    assert str(profile_path) not in str(exc_info.value)


@pytest.mark.parametrize(
    "failure",
    [UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"), OSError("private path")],
)
def test_invalid_env_file_uses_safe_structured_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    env_file = tmp_path / "private" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("placeholder", encoding="utf-8")

    original_read_text = Path.read_text

    def fail_read_text(path: Path, *, encoding: str) -> str:
        assert encoding == "utf-8"
        if path == env_file:
            raise failure
        return original_read_text(path, encoding=encoding)

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(
            profile="service",
            profiles_dir=PROFILES,
            env_file=env_file,
        )

    detail = exc_info.value.errors[0]
    assert detail.code == "config.invalid_env"
    assert detail.field_path == ".env"
    assert detail.hint == "检查 .env 的读取权限和 UTF-8 编码"
    assert str(tmp_path) not in str(exc_info.value)
    assert "private path" not in str(exc_info.value)


def test_missing_profile_error_uses_safe_stable_diagnostic(tmp_path: Path) -> None:
    missing = tmp_path / "customer" / "private" / "missing.yaml"

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(profile_path=missing)

    error = exc_info.value.errors[0]
    assert error.code == "config.missing"
    assert error.field_path == "profile"
    assert error.hint == "创建或检查 profile YAML"
    assert str(tmp_path) not in str(exc_info.value)
