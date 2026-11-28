"""`fact use`: a version pinned from a local repository with a bare `origin`."""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from fact.cli import use
from fact.cli.main import app
from fact.core.settings import Settings
from typer.testing import CliRunner


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return result.stdout.strip()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """Make a factory source tree whose `origin` (a bare repo) is one commit ahead of its local `main`."""
    bare = tmp_path / "origin.git"
    repo = tmp_path / "source"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _git("clone", str(bare), str(repo), cwd=tmp_path)
    _git("config", "user.name", "t", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "one", cwd=repo)
    _git("push", "origin", "HEAD:main", cwd=repo)
    (repo / "README.md").write_text("two\n", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "two", cwd=repo)
    _git("push", "origin", "HEAD:main", cwd=repo)
    _git("reset", "--hard", "HEAD~1", cwd=repo)
    _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)  # as if "two" was pushed from elsewhere
    return repo


@pytest.fixture
def synced(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], Path]]:
    """Record `uv sync` instead of running it; git commands still run."""
    calls: list[tuple[list[str], Path]] = []
    real_run = use._run  # noqa: SLF001

    def fake_run(argv: list[str], cwd: Path, timeout: float | None = None) -> str:
        if argv[0] == "uv":
            calls.append((argv, cwd))
            return ""
        return real_run(argv, cwd, timeout)

    monkeypatch.setattr(use, "_run", fake_run)
    return calls


def test_pin_uses_fetched_origin(settings: Settings, source: Path, synced: list[tuple[list[str], Path]]) -> None:
    origin_sha = _git("rev-parse", "main", cwd=source.parent / "origin.git")
    assert _git("rev-parse", "origin/main", cwd=source) != origin_sha  # not fetched yet

    checkout = use.pin(settings, "main", "uv", repo=source)

    assert checkout == settings.pinned_dir / "factory" / origin_sha
    assert _git("rev-parse", "HEAD", cwd=checkout) == origin_sha
    assert (checkout / "README.md").is_file()  # the fetched commit, not the stale local main
    assert synced == [(["uv", "sync", "--locked"], checkout)]
    assert use.current_file(settings).read_text(encoding="utf-8") == f"{origin_sha}\n"
    assert use.current_pin(settings) == checkout


@pytest.mark.usefixtures("synced")
def test_pin_copies_dotenv_each_time(settings: Settings, source: Path) -> None:
    (source / ".env").write_text("FACT_GITHUB_APP_ID=1", encoding="utf-8")
    checkout = use.pin(settings, "main", "uv", repo=source)
    assert (checkout / ".env").read_text(encoding="utf-8") == "FACT_GITHUB_APP_ID=1"

    (source / ".env").write_text("FACT_GITHUB_APP_ID=2", encoding="utf-8")
    use.pin(settings, "main", "uv", repo=source)  # same SHA: the checkout is kept, the secret still refreshed
    assert (checkout / ".env").read_text(encoding="utf-8") == "FACT_GITHUB_APP_ID=2"


def test_pin_is_idempotent_and_accepts_a_sha(
    settings: Settings, source: Path, synced: list[tuple[list[str], Path]]
) -> None:
    local_sha = _git("rev-parse", "HEAD", cwd=source)
    first = use.pin(settings, local_sha, "uv", repo=source)
    second = use.pin(settings, local_sha, "uv", repo=source)

    assert first == second == settings.pinned_dir / "factory" / local_sha
    assert [cwd for _, cwd in synced] == [first, second]
    assert use.current_pin(settings) == first


@pytest.mark.parametrize("ref", ["nope", "--upload-pack=x", ""])
def test_pin_rejects_bad_ref(settings: Settings, source: Path, ref: str) -> None:
    with pytest.raises(use.PinError):
        use.pin(settings, ref, "uv", repo=source)
    assert not use.current_file(settings).exists()


def test_current_pin_without_pin(settings: Settings) -> None:
    with pytest.raises(use.PinError, match="fact use"):
        use.current_pin(settings)


def test_current_pin_missing_checkout(settings: Settings) -> None:
    settings.pinned_dir.mkdir(parents=True)
    use.current_file(settings).write_text("b" * 40, encoding="utf-8")
    with pytest.raises(use.PinError, match="missing"):
        use.current_pin(settings)


def test_use_command_reports_errors(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    monkeypatch.setattr(use.shutil, "which", lambda _: "uv")

    def fail(*_args: object, **_kwargs: object) -> Path:
        msg = "cannot resolve 'x'"
        raise use.PinError(msg)

    monkeypatch.setattr(use, "pin", fail)
    result = CliRunner().invoke(app, ["use", "x"])

    assert result.exit_code == 1


def test_pin_head_is_local(settings: Settings, source: Path, synced: list[tuple[list[str], Path]]) -> None:
    local_sha = _git("rev-parse", "HEAD", cwd=source)
    assert use.pin(settings, "HEAD", "uv", repo=source).name == local_sha
    assert len(synced) == 1


@pytest.mark.parametrize("damage", ["delete", "corrupt"])
def test_pin_recreates_broken_checkout(
    settings: Settings, source: Path, synced: list[tuple[list[str], Path]], damage: str
) -> None:
    sha = _git("rev-parse", "HEAD", cwd=source)
    checkout = use.pin(settings, sha, "uv", repo=source)
    if damage == "delete":
        shutil.rmtree(checkout)
    else:
        with (checkout / ".git").open("r+", encoding="utf-8") as f:  # "w" fails: git hides it on Windows
            f.truncate()
            f.write("gitdir: nowhere")
        (checkout / ".git").chmod(stat.S_IREAD)  # the stale checkout must still be removable

    assert use.pin(settings, sha, "uv", repo=source) == checkout
    assert _git("rev-parse", "HEAD", cwd=checkout) == sha
    assert [cwd for _, cwd in synced] == [checkout, checkout]


def test_remote_head_and_changed_paths(source: Path, tmp_path: Path) -> None:
    head = use.remote_head(str(tmp_path / "origin.git"), source)  # the project-home check, against a local remote
    use.latest("main", source)
    old = _git("rev-parse", "HEAD", cwd=source)

    assert head == use.resolve_ref(source, "main")
    assert use.changed(source, old, head, ["README.md"])
    assert not use.changed(source, old, head, ["cli/"])
