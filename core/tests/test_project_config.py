from pathlib import Path

import pytest
from fact.core.project_config import ConfigError, load_project_config, load_repo_config
from fact.core.settings import REPO_ROOT


def _write(root: Path, name: str, text: str) -> Path:
    (root / ".fact").mkdir(exist_ok=True)
    (root / ".fact" / name).write_text(text, encoding="utf-8")
    return root


def test_loads_real_project_file() -> None:
    cfg = load_project_config(REPO_ROOT)
    assert cfg.name == "fact"
    assert [(r.full_name, r.role, r.default_branch) for r in cfg.repositories] == [("Kahncode/fact", "factory", "main")]
    assert cfg.task_source.kind == "github"
    assert cfg.task_source.repo == "Kahncode/fact"
    assert cfg.workflows.default == "standard"
    assert cfg.runner_pool == "local"
    assert cfg.require_plan_approval is False  # issues are approved by the `fact:ready` label
    assert cfg.classify.schema_paths == ["core/migrations/**"]
    assert cfg.classify.api_paths == ["api/openapi.json"]
    assert cfg.classify.always_review_paths == [".fact/**", ".github/**"]
    assert cfg.auto_merge is True
    assert cfg.merge_method == "squash"


def test_loads_real_repo_file() -> None:
    cfg = load_repo_config(REPO_ROOT)
    assert cfg.setup == [["uv", "sync"], ["uv", "run", "pre-commit", "install", "--install-hooks"]]
    assert cfg.lint[1] == ["uv", "run", "black", "--check", "."]
    assert cfg.test == [["uv", "run", "pytest"]]
    assert cfg.build == []


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


def test_classify_and_merge_defaults(tmp_path: Path) -> None:
    _write(tmp_path, "project.yaml", BASE)
    cfg = load_project_config(tmp_path)
    assert cfg.classify.schema_paths == cfg.classify.api_paths == cfg.classify.always_review_paths == []
    assert cfg.auto_merge is True
    assert cfg.merge_method == "squash"


def test_merge_method_is_checked(tmp_path: Path) -> None:
    _write(tmp_path, "project.yaml", BASE + "merge_method: fast-forward\n")
    with pytest.raises(ConfigError, match="merge_method"):
        load_project_config(tmp_path)
