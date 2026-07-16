"""Agent scaffold 校验、路径边界与原子发布合同测试。"""

from __future__ import annotations

from tests.contracts.test_agent_scaffold_cli_contracts import (
    ROOT as ROOT,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    AgentRegistry as AgentRegistry,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    Path as Path,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    ScaffoldError as ScaffoldError,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    _agents_root as _agents_root,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    _sqlite_dsn as _sqlite_dsn,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    app as app,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    importlib as importlib,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    pytest as pytest,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    runner as runner,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    scaffold_agent_package as scaffold_agent_package,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    scaffold_module as scaffold_module,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    sys as sys,
)


def test_scaffold_cli_help_success_structure_and_existing_target(tmp_path: Path) -> None:
    help_result = runner.invoke(app, ["scaffold", "agent", "--help"])
    assert help_result.exit_code == 0
    assert "AGENT_ID" in help_result.stdout
    assert "--agents-dir" in help_result.stdout

    agents_dir = _agents_root(tmp_path)
    result = runner.invoke(
        app,
        ["scaffold", "agent", "support.triage", "--agents-dir", str(agents_dir)],
    )
    assert result.exit_code == 0, result.output
    assert "created: support/triage" in result.stdout
    target = agents_dir / "support" / "triage"
    expected_files = {
        "__init__.py",
        "agent.py",
        "tools.py",
        "schemas.py",
        "config.yaml",
        "evals/drafts/example.yaml",
    }
    assert expected_files <= {
        path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()
    }
    approved_dir = target / "evals" / "approved"
    assert approved_dir.is_dir()
    assert list(approved_dir.iterdir()) == []
    assert (agents_dir / "__init__.py").is_file()
    assert (agents_dir / "support" / "__init__.py").is_file()
    assert not list(target.rglob("__pycache__"))

    registry = AgentRegistry.load_from_directory(agents_dir)
    descriptor = registry.get("support.triage")
    public_payload = descriptor.to_payload()
    assert descriptor.model_policy.provider == "fake"
    assert descriptor.tool_policy.allowed_tools == []
    assert descriptor.delegation_targets == []
    assert descriptor.config_ref == "support/triage/config.yaml"
    assert not {"executor", "callable", "module", "path"} & public_payload.keys()

    list_dsn = _sqlite_dsn(tmp_path / "cli-list.db")
    run_migrations(list_dsn)
    list_result = runner.invoke(
        app,
        [
            "agents",
            "list",
            "--agents-dir",
            str(agents_dir),
            "--profiles-dir",
            str(ROOT / "templates" / "service-app" / "configs" / "profiles"),
            "--storage-dsn",
            list_dsn,
        ],
    )
    assert list_result.exit_code == 0, list_result.output
    assert "support.triage" in list_result.stdout

    source_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in target.rglob("*")
        if path.suffix in {".py", ".yaml"}
    ).lower()
    for forbidden in (
        "pydantic_ai",
        "dbos",
        "logfire",
        "phoenix",
        "langfuse",
        "sqlalchemy",
        "sessionmaker",
        "api_key",
        "secret=",
    ):
        assert forbidden not in source_text

    existing = runner.invoke(
        app,
        ["scaffold", "agent", "support.triage", "--agents-dir", str(agents_dir)],
    )
    assert existing.exit_code == 1
    assert "scaffold.target_exists" in existing.output
    assert AgentRegistry.load_from_directory(agents_dir).get("support.triage") == descriptor


@pytest.mark.parametrize(
    "agent_id",
    ["", "/tmp/agent", "../escape", "support..triage", "Support", "bad-name", "a/b", ".a"],
)
def test_scaffold_invalid_ids_leave_filesystem_unchanged(tmp_path: Path, agent_id: str) -> None:
    agents_dir = _agents_root(tmp_path)
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    result = runner.invoke(
        app,
        ["scaffold", "agent", agent_id, "--agents-dir", str(agents_dir)],
    )
    assert result.exit_code != 0
    assert "scaffold.invalid_agent_id" in result.output
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert after == before


def test_scaffold_rejects_parent_symlink_escape(tmp_path: Path) -> None:
    agents_dir = _agents_root(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (agents_dir / "support").symlink_to(external, target_is_directory=True)

    with pytest.raises(ScaffoldError, match="must not be a symlink") as caught:
        scaffold_agent_package("support.triage", agents_dir=agents_dir)

    assert caught.value.code == "scaffold.symlink_escape"
    assert list(external.iterdir()) == []


def test_staging_is_invisible_and_validation_failure_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_dir = _agents_root(tmp_path)
    observed_staging = False

    def observe_then_validate(root: Path, target: Path, agent_id: str) -> None:
        nonlocal observed_staging
        if root != agents_dir:
            observed_staging = True
            assert AgentRegistry.load_from_directory(agents_dir).list_agents() == []
            staging_namespace = tmp_path / ".agent-harness-scaffold-staging"
            staging_configs = list(staging_namespace.rglob("config.yaml"))
            assert len(staging_configs) == 1
            assert not (agents_dir / "support" / "triage").exists()
        registry = AgentRegistry.load_from_directory(root)
        registry.get(agent_id)
        registry.resolve_executor(agent_id)

    monkeypatch.setattr(scaffold_module, "_validate_generated_package", observe_then_validate)
    scaffold_agent_package("support.triage", agents_dir=agents_dir)
    assert observed_staging is True
    assert not (tmp_path / ".agent-harness-scaffold-staging").exists()

    failing_agents = tmp_path / "failing-agents"
    failing_agents.mkdir()

    def fail_validation(root: Path, target: Path, agent_id: str) -> None:
        del root, target, agent_id
        raise ValueError("injected schema failure")

    monkeypatch.setattr(scaffold_module, "_validate_generated_package", fail_validation)
    with pytest.raises(ScaffoldError, match="injected schema failure"):
        scaffold_agent_package("broken.agent", agents_dir=failing_agents)
    assert list(failing_agents.iterdir()) == []
    assert not (tmp_path / ".agent-harness-scaffold-staging").exists()
    assert not (tmp_path / ".agent-harness-scaffold.lock").exists()


def test_default_discovery_uses_copied_service_app_root_and_unknown_root_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied = tmp_path / "copied-service"
    agents_dir = copied / "agents"
    nested = copied / "docs" / "guide"
    agents_dir.mkdir(parents=True)
    nested.mkdir(parents=True)
    (copied / "pyproject.toml").write_text(
        '[project]\nname = "agent-harness-service-app"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(nested)
    result = runner.invoke(app, ["scaffold", "agent", "support.triage"])
    assert result.exit_code == 0, result.output
    assert (agents_dir / "support" / "triage" / "config.yaml").is_file()
    assert not (copied / "templates" / "service-app" / "agents").exists()

    unknown = tmp_path / "unknown"
    unknown.mkdir()
    monkeypatch.chdir(unknown)
    before = sorted(path.relative_to(unknown).as_posix() for path in unknown.rglob("*"))
    failed = runner.invoke(app, ["scaffold", "agent", "unknown.agent"])
    assert failed.exit_code == 1
    assert "scaffold.agents_dir_required" in failed.output
    assert "--agents-dir" in failed.output
    after = sorted(path.relative_to(unknown).as_posix() for path in unknown.rglob("*"))
    assert after == before


def test_explicit_custom_agents_dir_emits_importable_schema_refs(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "custom-root"
    agents_dir.mkdir()
    scaffold_agent_package("support.triage", agents_dir=agents_dir)
    descriptor = AgentRegistry.load_from_directory(agents_dir).get("support.triage")

    assert descriptor.input_schema_ref == "custom-root.support.triage.schemas.ScaffoldInput"
    assert descriptor.output_schema_ref == "custom-root.support.triage.schemas.ScaffoldOutput"
    assert descriptor.eval_dataset == "custom-root/support/triage/evals/approved"

    module_name = "custom-root.support.triage.schemas"
    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module(module_name)
        assert hasattr(module, "ScaffoldInput")
        assert hasattr(module, "ScaffoldOutput")
    finally:
        sys.path.remove(str(tmp_path))
        for loaded_name in tuple(sys.modules):
            if loaded_name == "custom-root" or loaded_name.startswith("custom-root."):
                sys.modules.pop(loaded_name, None)
