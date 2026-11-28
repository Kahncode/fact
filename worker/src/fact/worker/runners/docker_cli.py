"""
The `docker` CLI as the docker runner uses it: the run's container and volume, and commands inside it.

Values the container's commands get (a token among them) reach `docker exec` through the client's own environment, as
`--env NAME` without a value: never in argv, so never in a process listing, a log line or the container's config.

Every `docker exec` process is already the leader of its own process group. The command runs under a small `sh`
wrapper that writes its pid (so the group id) to a file in the container's `/tmp` and stays the group's leader. When
the command ends, a second `docker exec` kills that group, since killing the `docker` client stops nothing inside: on
timeout or cancel to stop the command, and after a normal exit too, so background children (`server &`) die with the
command as they do with the local runner.
"""

import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path, PurePosixPath

from fact.core.logging import get_logger
from fact.core.project_config import RunnerPoolConfig
from fact.worker.runners.base import ExecExit, ExecLine, ExecOutput, RunnerError
from fact.worker.runners.process import spawn, stream

log = get_logger(__name__)

DOCKER = "docker"  # the CLI, found on PATH
WORKSPACE = PurePosixPath("/workspace")
USER = "fact"
LABEL_RUN = "fact.run"  # the run id, on the run's container and volume
CLI_TIMEOUT = 300.0  # `docker run` may wait on a busy daemon
KILL_TIMEOUT = 30.0
# `$0` is the pid file, left for the kill to read and remove; the wrapper stays the group's leader so the group id is
# its pid
_WRAPPER = 'echo $$ > "$0"; "$@"'
_KILL_GROUP = '[ -s "$0" ] && kill -KILL -- "-$(cat "$0")" 2>/dev/null; rm -f "$0"; true'
# a stopped or cancelled command may not have written its pid yet: wait up to 5 s for it
_KILL = 'i=0; while [ ! -s "$0" ] && [ $i -lt 50 ]; do sleep 0.1; i=$((i+1)); done; ' + _KILL_GROUP
_HOST_CWD = Path(tempfile.gettempdir())  # the client touches no file; any existing folder will do


def container_name(run_id: str) -> str:
    return f"fact-run-{run_id}"


def volume_name(run_id: str) -> str:
    return f"fact-ws-{run_id}"


def run_args(run_id: str, image: str, pool: RunnerPoolConfig, sandbox_args: Sequence[str]) -> list[str]:
    """
    `docker run` arguments: fact's hardening, then the pool's `run_args`, then the workflow's, so the most specific wins.

    The only mount is the run's named volume: no bind mount, no Docker socket.
    """
    hardening = [
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", pool.memory,
        "--cpus", str(pool.cpus),
        "--pids-limit", str(pool.pids),
    ]  # fmt: skip
    own = [
        "--name", container_name(run_id),
        "--init",
        "--user", USER,
        "--label", f"{LABEL_RUN}={run_id}",
        "--mount", f"type=volume,source={volume_name(run_id)},target={WORKSPACE}",
        "--workdir", str(WORKSPACE),
    ]  # fmt: skip
    return ["run", "--detach", *hardening, *own, *pool.run_args, *sandbox_args, image, "sleep", "infinity"]


async def docker(args: Sequence[str], timeout: float = CLI_TIMEOUT) -> tuple[int, list[str]]:  # noqa: ASYNC109
    """Run `docker <args>`; return its exit code and merged output lines."""
    try:
        proc = await spawn([DOCKER, *args], _HOST_CWD, dict(os.environ))
    except FileNotFoundError as exc:
        msg = f"{DOCKER}: command not found; the docker runner pool needs the Docker CLI"
        raise RunnerError(msg) from exc
    lines: list[str] = []
    code = -1
    async for item in stream(proc, timeout):
        if isinstance(item, ExecLine):
            lines.append(item.text)
        elif isinstance(item, ExecExit):
            code = item.exit_code
    return code, lines


async def docker_ok(args: Sequence[str], timeout: float = CLI_TIMEOUT) -> list[str]:  # noqa: ASYNC109
    """Run `docker <args>`; `RunnerError` with its output unless it exits 0."""
    code, lines = await docker(args, timeout)
    if code != 0:
        log.warning("docker_failed", args=list(args[:2]), exit_code=code, output=lines[-20:])
        msg = f"docker {' '.join(args[:2])} failed ({code}): {' '.join(lines[-20:])}"
        raise RunnerError(msg)
    return lines


async def ensure_running(container: str) -> None:
    """Start the container when it is stopped (after a reboot or `docker stop`); `RunnerError` when it is gone."""
    lines = await docker_ok(["inspect", "--format", "{{.State.Running}}", container])
    if lines and lines[0].strip() == "true":
        return
    log.info("container_starting", container=container)
    await docker_ok(["start", container])


async def remove(run_id: str) -> None:
    """Remove the run's container and its volume; both may already be gone."""
    await docker_ok(["rm", "--force", "--volumes", container_name(run_id)])
    await docker_ok(["volume", "rm", "--force", volume_name(run_id)])


async def exec_stream(
    container: str,
    argv: Sequence[str],
    workdir: PurePosixPath,
    env: Mapping[str, str],
    timeout: float | None,  # noqa: ASYNC109 - enforced by `stream`
) -> AsyncIterator[ExecOutput]:
    """Run `argv` in the running container; when it ends (exit, timeout or cancel) kill its process group inside."""
    pidfile = f"/tmp/fact-exec-{uuid.uuid4().hex}.pid"  # noqa: S108 - the container's own /tmp
    names = [flag for name in sorted(env) for flag in ("--env", name)]
    args = [DOCKER, "exec", "--workdir", str(workdir), *names, container, "sh", "-c", _WRAPPER, pidfile, *argv]
    proc = await spawn(args, _HOST_CWD, {**os.environ, **env})
    ended = False
    try:
        async for item in stream(proc, timeout):
            if isinstance(item, ExecExit):
                # after a normal exit the pid file is written (or never will be): no need to wait for it
                await _kill_group(container, pidfile, wait=item.timed_out)
                ended = True
            yield item
    finally:
        if not ended:  # cancelled, or the caller stopped reading
            await _kill_group(container, pidfile, wait=True)


async def _kill_group(container: str, pidfile: str, *, wait: bool) -> None:
    script = _KILL if wait else _KILL_GROUP
    code, lines = await docker(["exec", container, "sh", "-c", script, pidfile], KILL_TIMEOUT)
    if code != 0:
        log.warning("exec_kill_failed", container=container, exit_code=code, output=lines[-5:])
