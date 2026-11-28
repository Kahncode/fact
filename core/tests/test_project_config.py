from pathlib import Path

import pytest
from fact.core.project_config import ConfigError, load_project_config, load_repo_config
from fact.core.settings import REPO_ROOT


def _write(root: Path, name: str, text: str) -> Path:
    (root / ".fact").mkdir(exist_ok=True)
    (root / ".fact" / name).write_text(text, encoding="utf-8")
    return root


def test_committed_config_files_validate() -> None:
    assert load_project_config(REPO_ROOT).name
    load_repo_config(REPO_ROOT)


def test_project_missing_name(tmp_path: Path) -> None:
    _write(tmp_path, "project.yaml", "repositories: [{full_name: a/b, role: x}]\nworkflows: {default: s}\n")
    with pytest.raises(ConfigError) as exc:
        load_project_config(tmp_path)
    assert "project.yaml" in str(exc.value)
    assert "name" in str(exc.value)


def test_command_as_string_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "repo.yaml", "test:\n  - uv run pytest\n")
    with pytest.raises(ConfigError) as exc:
        load_repo_config(tmp_path)
    assert "repo.yaml" in str(exc.value)
    assert "test.0" in str(exc.value)


def test_github_task_source_needs_repo(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "project.yaml",
        "name: p\nrepositories: [{full_name: a/b, role: x}]\ntask_source: {kind: github}\nworkflows: {default: s}\n",
    )
    with pytest.raises(ConfigError, match="task_source"):
        load_project_config(tmp_path)


def test_workflow_type_map(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "project.yaml",
        "name: p\nrepositories: [{full_name: a/b, role: x}]\nworkflows: {default: s, bug: fixer}\n",
    )
    assert load_project_config(tmp_path).workflows.by_type == {"bug": "fixer"}


def test_missing_file_named(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"repo.yaml"):
        load_repo_config(tmp_path)


BASE = "name: p\nrepositories: [{full_name: a/b, role: x}]\nworkflows: {default: s}\n"


def test_merge_method_is_checked(tmp_path: Path) -> None:
    _write(tmp_path, "project.yaml", BASE + "merge_method: fast-forward\n")
    with pytest.raises(ConfigError, match="merge_method"):
        load_project_config(tmp_path)


def test_docker_pool_parses(tmp_path: Path) -> None:
    pool = "runner_pool: {kind: docker, memory: 4g, run_args: [--network, none], default_sandbox: python}\n"
    _write(tmp_path, "project.yaml", BASE + pool)
    loaded = load_project_config(tmp_path).runner_pool
    assert (loaded.kind, loaded.memory, loaded.cpus, loaded.pids) == ("docker", "4g", 4, 4096)
    assert loaded.run_args == ["--network", "none"]
    assert loaded.default_sandbox == "python"


@pytest.mark.parametrize("name", ["python-", "py.", "a..b", "-py", "Py"])
def test_a_sandbox_name_docker_refuses_in_a_tag_is_refused(tmp_path: Path, name: str) -> None:
    _write(tmp_path, "project.yaml", BASE + f"runner_pool: {{kind: docker, default_sandbox: '{name}'}}\n")
    with pytest.raises(ConfigError, match="default_sandbox"):
        load_project_config(tmp_path)


def test_docker_only_field_refused_on_local(tmp_path: Path) -> None:
    _write(tmp_path, "project.yaml", BASE + "runner_pool: {kind: local, memory: 4g}\n")
    with pytest.raises(ConfigError, match="memory: only for kind docker"):
        load_project_config(tmp_path)
