import json
import logging
from collections.abc import Iterator

import pytest
import structlog
from fact.core.logging import bind_run, configure_logging, get_logger
from fact.core.settings import AppEnv, Settings


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:], root.level = handlers, level
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_run_id_bound_only_inside_bind_run(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("prod")
    log = get_logger("t")

    log.info("before")
    with bind_run("run-1", step_id="step-a"):
        log.info("inside")
    log.info("after")

    before, inside, after = _lines(capsys)
    assert "run_id" not in before
    assert inside["run_id"] == "run-1"
    assert inside["step_id"] == "step-a"
    assert inside["event"] == "inside"
    assert inside["level"] == "info"
    assert "timestamp" in inside
    assert "run_id" not in after
    assert "step_id" not in after


def test_stdlib_records_go_through_structlog(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("prod")

    with bind_run("run-2"):
        logging.getLogger("uvicorn.error").warning("from %s", "stdlib")

    (record,) = _lines(capsys)
    assert record["event"] == "from stdlib"
    assert record["level"] == "warning"
    assert record["logger"] == "uvicorn.error"
    assert record["run_id"] == "run-2"


def test_bind_run_restores_outer_binding(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("prod")
    log = get_logger("t")

    with bind_run("outer"):
        with bind_run("inner", step_id="s"):
            log.info("nested")
        log.info("restored")

    nested, restored = _lines(capsys)
    assert nested["run_id"] == "inner"
    assert nested["step_id"] == "s"
    assert restored["run_id"] == "outer"
    assert "step_id" not in restored


@pytest.mark.parametrize("env", ["local", "prod"])
def test_exception_traceback_rendered(env: str, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(env)
    try:
        raise ValueError("boom-structlog")  # noqa: EM101, TRY301
    except ValueError:
        get_logger("t").exception("failed")
    try:
        raise ValueError("boom-stdlib")  # noqa: EM101, TRY301
    except ValueError:
        logging.getLogger("lib").exception("failed")

    out = capsys.readouterr().out
    assert "boom-structlog" in out
    assert "boom-stdlib" in out
    assert "Traceback" in out


def test_console_renderer_for_local_and_test(capsys: pytest.CaptureFixture[str]) -> None:
    for env in ("local", "test"):
        configure_logging(env)
        with bind_run("run-3"):
            get_logger("t").info("hello")
        out = capsys.readouterr().out
        assert "hello" in out
        assert "run_id" in out
        assert not out.lstrip().startswith("{")


def test_level_filters(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("prod", level="warning")
    get_logger("t").info("quiet")
    get_logger("t").warning("loud")

    (record,) = _lines(capsys)
    assert record["event"] == "loud"


def test_json_has_callsite_and_gcp_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("prod", gcp=True)
    get_logger("t").info(
        "done",
        http_request={"method": "GET", "url": "/x", "headers": {"user-agent": "ua"}},
        http_response={"status_code": 200, "processing_time": 1500},
    )
    (record,) = _lines(capsys)
    assert record["message"] == "done"
    assert record["severity"] == "INFO"
    assert record["httpRequest"] == {
        "requestMethod": "GET",
        "requestUrl": "/x",
        "status": 200,
        "latency": "1.5s",
        "userAgent": "ua",
    }
    assert record["func_name"] == "test_json_has_callsite_and_gcp_fields"
    assert "filename" in record
    assert "lineno" in record


def test_effective_log_level_per_env() -> None:
    assert Settings(env=AppEnv.LOCAL).effective_log_level == "DEBUG"
    assert Settings(env=AppEnv.PROD).effective_log_level == "INFO"
    assert Settings(env=AppEnv.PROD, log_level="warning").effective_log_level == "WARNING"


def test_gcp_keeps_bodies_and_invalid_level_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("prod", gcp=True)
    get_logger("t").info(
        "done",
        http_request={"method": "POST", "url": "/x", "query": "a=1", "headers": {}, "body": {"k": 1}},
        http_response={"status_code": 200, "processing_time": 1, "body": {"ok": True}},
    )
    (record,) = _lines(capsys)
    assert record["request_body"] == {"k": 1}
    assert record["response_body"] == {"ok": True}
    assert record["request_query"] == "a=1"
    with pytest.raises(ValueError, match="log_level"):
        Settings(log_level="bogus")
    assert Settings(log_level="warning").effective_log_level == "WARNING"
