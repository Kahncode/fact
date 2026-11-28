"""
Structured logging for every fact process.

Console output locally and in tests, JSON elsewhere. stdlib `logging` records (uvicorn, temporalio, ...) go through
the same processors, so one `run_id` appears on every line emitted while a run is bound.
"""

import logging
import sys
from collections.abc import Generator, Sequence
from contextlib import contextmanager

import structlog
from fact.core.logging_gcp import format_gcloud_logs
from fact.core.settings import AppEnv
from structlog.contextvars import bound_contextvars
from structlog.typing import Processor

_CONSOLE_ENVS = (AppEnv.LOCAL, AppEnv.TEST)


_CALLSITE = structlog.processors.CallsiteParameterAdder(
    {
        structlog.processors.CallsiteParameter.FILENAME,
        structlog.processors.CallsiteParameter.FUNC_NAME,
        structlog.processors.CallsiteParameter.LINENO,
    }
)


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]


QUIET_LIBRARIES = ("httpx", "httpcore", "hpack", "aiosqlite")


def configure_logging(
    env: AppEnv | str,
    level: str = "INFO",
    *,
    gcp: bool = False,
    extra_processors: Sequence[Processor] = (),
) -> None:
    """
    Configure structlog and the stdlib root logger. Safe to call more than once: the last call wins.

    `level` applies to fact's own loggers; `QUIET_LIBRARIES` log only warnings and errors at any level.

    Console renderer (colored when stdout is a terminal) when `env` is `local` or `test`, JSON renderer otherwise.
    JSON lines also carry the call site (file, function, line). `gcp` renames fields for Google Cloud Logging
    (`message`, `severity`, `httpRequest`); `extra_processors` run just before the JSON renderer. Output goes to
    stdout, captured at call time: call this once at process start, after any stream redirection.
    """
    console = AppEnv(env) in _CONSOLE_ENVS
    shared = _shared_processors()
    if console:
        renderer: Processor = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    else:
        shared += [structlog.processors.format_exc_info, structlog.processors.UnicodeDecoder(), _CALLSITE]
        if gcp:
            shared.append(format_gcloud_logs)
        shared += extra_processors
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[structlog.stdlib.filter_by_level, *shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in QUIET_LIBRARIES:  # a line per request, connection step or query: only their warnings
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)


@contextmanager
def bind_run(run_id: str, step_id: str | None = None) -> Generator[None]:
    """Bind `run_id` (and `step_id` if given) to every log line in this context, then restore the previous values."""
    keys = {"run_id": run_id}
    if step_id is not None:
        keys["step_id"] = step_id
    with bound_contextvars(**keys):
        yield
