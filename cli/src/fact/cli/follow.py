"""
`fact dev --follow <ref>`: add a worker generation whenever `<ref>` or a project's home branch moves.

Every `CHECK_INTERVAL_S` the follower fetches `origin`, resolves `<ref>` and each registered project's home default
branch (`git ls-remote`), and compares them with the newest generation (`fact.cli.generations`):

- `<ref>` moved: it pins it (`fact use`); the API restarts from the new pin and the new generation's worker starts;
- only a project's home moved: a new generation of the same pin starts, the API untouched.

Either way the old generations keep serving their runs: the scheduler is never turned off and no run is waited for.
While the newest generation's worker is not through its start, nothing is added; if it fails to start, `fact dev`
deletes it (`Generations.exited`), the previous one stays current, and the next check tries again. A project the
newest generation does not hold (it failed to load) counts as moved once its home moves after the follower's last
check. A failed check (git, `uv sync`) is printed and retried at the next one.
"""

import time
from collections.abc import Callable
from pathlib import Path

from fact.cli.generations import Generations
from fact.cli.supervisor import Changes
from fact.cli.use import PinError, current_pin, github_url, latest, pin, remote_head
from fact.core import generations
from fact.core.generations import Generation
from fact.core.settings import REPO_ROOT, Settings

CHECK_INTERVAL_S = 60.0
# what a check can raise: git or uv failing (PinError), a file lock while pinning
CHECK_ERRORS = (PinError, OSError)

HomeResolver = Callable[[str], str]  # a project's home `owner/repo` to its default branch's SHA now


def short(sha: str) -> str:
    return sha[:12]


class Follower:
    """Called by the supervisor on every poll; returns the new generation to start once `ref` or a home moved."""

    def __init__(  # noqa: PLR0913
        self,
        settings: Settings,
        ref: str,
        *,
        uv: str,
        workers: Generations,
        log: Callable[[str], None],
        repo: Path = REPO_ROOT,
        clock: Callable[[], float] = time.monotonic,
        resolve_home: HomeResolver | None = None,
    ) -> None:
        self._settings, self._ref, self._uv, self._repo = settings, ref, uv, repo
        self._workers, self._log, self._clock = workers, log, clock
        self._resolve_home = resolve_home or (lambda home: remote_head(github_url(home), repo))
        self._seen: dict[str, str] = {}  # each project's home SHA at the last check
        self._waiting: str | None = None  # the generation whose start a check waits for, reported once
        self._next = clock() + CHECK_INTERVAL_S

    def __call__(self) -> Changes | None:
        now = self._clock()
        if now < self._next:
            return None
        self._next = now + CHECK_INTERVAL_S
        try:
            return self._check()
        except CHECK_ERRORS as exc:
            self._log(f"upgrade check failed: {exc}; retrying in {CHECK_INTERVAL_S:g} s")
            return None

    def _check(self) -> Changes | None:
        sha = latest(self._ref, self._repo)
        homes = {p.name: self._resolve_home(p.home) for p in self._settings.projects}
        seen, self._seen = self._seen, homes
        newest = generations.newest(self._workers.directory)
        if newest is None:
            return None  # `fact dev` records one at start
        if not generations.started(self._workers.directory, newest.id):
            if self._waiting != newest.id:
                self._log(f"generation {newest.id} is starting: upgrading once it is up")
                self._waiting = newest.id
            return None
        if sha != newest.factory:
            self._log(f"{self._ref} moved {short(newest.factory)} -> {short(sha)}: pinning it")
            checkout = pin(self._settings, self._ref, self._uv, self._repo)
            return Changes(self._workers.add(checkout))
        if moved := _moved(newest, homes, seen):
            self._log(f"home of {', '.join(moved)} moved: starting a new generation of {short(sha)}")
            return Changes(self._workers.add(current_pin(self._settings)))
        return None


def _moved(newest: Generation, homes: dict[str, str], seen: dict[str, str]) -> list[str]:
    """Return the projects whose home is not at the newest generation's SHA (or, when it has none, at the last seen)."""
    moved = []
    for project, sha in homes.items():
        recorded = newest.projects.get(project)
        if (recorded is not None and recorded != sha) or (recorded is None and seen.get(project, sha) != sha):
            moved.append(project)
    return moved
