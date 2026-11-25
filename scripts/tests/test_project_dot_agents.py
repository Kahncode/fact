"""Tests for the SessionStart hook that projects .agents/{skills,agents} into .claude/."""

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "project_dot_agents.py"


def load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("project_dot_agents", HOOK)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load_hook()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".agents" / "skills" / "demo").mkdir(parents=True)
    (tmp_path / ".agents" / "skills" / "demo" / "SKILL.md").write_text("skill", encoding="utf-8")
    (tmp_path / ".agents" / "agents" / "core").mkdir(parents=True)
    (tmp_path / ".agents" / "agents" / "core" / "reviewer.md").write_text("agent", encoding="utf-8")
    return tmp_path


def test_copy_mode_projects_skills_and_agents(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert hook.project(root, "copy", force=False) == 0

    assert (root / ".claude" / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "skill"
    assert (root / ".claude" / "agents" / "core" / "reviewer.md").read_text(encoding="utf-8") == "agent"
    out = capsys.readouterr().out
    assert "PROJECTION_SKILLS=copy" in out
    assert "PROJECTION_AGENTS=copy" in out


def test_copy_mode_is_idempotent(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hook.project(root, "copy", force=False)
    capsys.readouterr()

    assert hook.project(root, "copy", force=False) == 0
    assert "agents copy already up to date" in capsys.readouterr().out


def test_auto_mode_exposes_agents(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert hook.project(root, "auto", force=False) == 0

    dst = root / ".claude" / "agents"
    assert (dst / "core" / "reviewer.md").read_text(encoding="utf-8") == "agent"
    kind = hook.link_kind(dst)
    if kind is not None:
        assert os.path.realpath(dst) == os.path.realpath(root / ".agents" / "agents")
    out = capsys.readouterr().out
    assert f"PROJECTION_AGENTS={kind or 'copy'}" in out
    assert "PROJECTION_SKILLS=" in out


def test_unmanaged_agents_dir_is_refused_without_force(root: Path) -> None:
    unmanaged = root / ".claude" / "agents"
    unmanaged.mkdir(parents=True)
    (unmanaged / "local.md").write_text("mine", encoding="utf-8")

    assert hook.project(root, "copy", force=False) == 1
    assert (unmanaged / "local.md").read_text(encoding="utf-8") == "mine"
    assert (root / ".claude" / "skills" / "demo" / "SKILL.md").exists()


def test_unmanaged_agents_dir_is_backed_up_with_force(root: Path) -> None:
    unmanaged = root / ".claude" / "agents"
    unmanaged.mkdir(parents=True)
    (unmanaged / "local.md").write_text("mine", encoding="utf-8")

    assert hook.project(root, "copy", force=True) == 0

    backups = list((root / ".claude").glob("agents.bak-*"))
    assert len(backups) == 1
    assert (backups[0] / "local.md").read_text(encoding="utf-8") == "mine"
    assert (root / ".claude" / "agents" / "core" / "reviewer.md").exists()


def test_missing_source_fails_but_projects_the_rest(root: Path) -> None:
    (root / ".agents" / "agents" / "core" / "reviewer.md").unlink()
    (root / ".agents" / "agents" / "core").rmdir()
    (root / ".agents" / "agents").rmdir()

    assert hook.project(root, "copy", force=False) == 1
    assert (root / ".claude" / "skills" / "demo" / "SKILL.md").exists()
    assert not (root / ".claude" / "agents").exists()


def test_error_in_one_directory_does_not_block_the_other(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = hook.project_one

    def flaky(root: Path, name: str, mode: str, force: bool) -> int:  # noqa: FBT001
        if name == "skills":
            raise PermissionError(13, "locked")
        return real(root, name, mode, force)

    monkeypatch.setattr(hook, "project_one", flaky)

    assert hook.project(root, "copy", force=False) == 1
    assert (root / ".claude" / "agents" / "core" / "reviewer.md").exists()


def test_locally_edited_copy_is_backed_up_then_resynced(root: Path) -> None:
    hook.project(root, "copy", force=False)
    edited = root / ".claude" / "agents" / "core" / "reviewer.md"
    edited.write_text("local edit", encoding="utf-8")

    assert hook.project(root, "copy", force=False) == 0

    backups = list((root / ".claude").glob("agents.bak-*"))
    assert len(backups) == 1
    assert (backups[0] / "core" / "reviewer.md").read_text(encoding="utf-8") == "local edit"
    assert edited.read_text(encoding="utf-8") == "agent"


def test_link_mode_failure_keeps_the_managed_copy(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook.project(root, "copy", force=False)

    def no_links(_src: Path, _dst: Path) -> str:
        raise OSError(1, "no permission")

    monkeypatch.setattr(hook, "make_link", no_links)

    assert hook.project(root, "link", force=False) == 1
    assert (root / ".claude" / "agents" / "core" / "reviewer.md").read_text(encoding="utf-8") == "agent"
    assert (root / ".claude" / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "skill"
