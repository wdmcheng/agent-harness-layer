"""Typed config 竞态防护与安全诊断合同测试。"""

from __future__ import annotations

from tests.contracts.test_typed_config_contracts import (
    PROFILES as PROFILES,
)
from tests.contracts.test_typed_config_contracts import (
    Path as Path,
)
from tests.contracts.test_typed_config_contracts import (
    SettingsLoadError as SettingsLoadError,
)
from tests.contracts.test_typed_config_contracts import (
    load_settings as load_settings,
)
from tests.contracts.test_typed_config_contracts import (
    os as os,
)
from tests.contracts.test_typed_config_contracts import (
    pytest as pytest,
)
from tests.contracts.test_typed_config_contracts import (
    secret_files_module as secret_files_module,
)
from tests.contracts.test_typed_config_contracts import (
    traceback as traceback,
)


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


def test_secret_file_conflict_scrubs_traceback_frame_locals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """direct/file 冲突的异常链与 frame locals 都不得保留 direct secret。"""

    trusted_root = tmp_path / "secrets"
    trusted_root.mkdir()
    candidate = trusted_root / "fingerprint-key"
    candidate.write_text("unused-file-secret", encoding="utf-8")
    secret_fixture = "unique-direct-conflict-secret-fixture"
    monkeypatch.setenv("AGENT_HARNESS_BUDGET__FINGERPRINT_KEY", secret_fixture)
    monkeypatch.setenv("AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE", str(candidate))

    with pytest.raises(SettingsLoadError) as exc_info:
        load_settings(
            profile="service",
            profiles_dir=PROFILES,
            secret_root=trusted_root,
        )

    assert exc_info.value.errors[0].code == "config.secret_file_conflict"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert secret_fixture not in str(exc_info.value)
    assert secret_fixture not in "".join(traceback.format_exception(exc_info.value))
    current = exc_info.value.__traceback__
    settings_frames = 0
    while current is not None:
        module_name = current.tb_frame.f_globals.get("__name__")
        if module_name in {
            "agent_harness.config.settings",
            "agent_harness.config.secret_files",
        }:
            settings_frames += 1
            assert secret_fixture not in repr(current.tb_frame.f_locals)
        current = current.tb_next
    assert settings_frames > 0


def test_later_secret_file_failure_scrubs_earlier_value_from_traceback(
    tmp_path: Path,
) -> None:
    """后续 `_FILE` 失败时，已读取的前序 secret 不得留在 helper frame。"""

    trusted_root = tmp_path / "secrets"
    trusted_root.mkdir()
    fingerprint_path = trusted_root / "fingerprint-key"
    secret_fixture = "unique-earlier-file-secret-fixture"
    fingerprint_path.write_text(secret_fixture, encoding="utf-8")
    missing_path = trusted_root / "missing-storage-dsn"
    process_env = {
        "AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE": str(fingerprint_path),
        "AGENT_HARNESS_STORAGE__DSN_FILE": str(missing_path),
    }

    with pytest.raises(SettingsLoadError) as exc_info:
        secret_files_module.load_secret_file_env(
            process_env,
            secret_root=trusted_root,
        )

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    current = exc_info.value.__traceback__
    helper_frames = 0
    while current is not None:
        if current.tb_frame.f_globals.get("__name__") == "agent_harness.config.secret_files":
            helper_frames += 1
            assert secret_fixture not in repr(current.tb_frame.f_locals)
        current = current.tb_next
    assert helper_frames > 0


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
