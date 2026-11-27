"""Every API route must be listed here with the tests that cover it, so a new route cannot ship untested."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

from fact.api.app import create_app
from fact.core.settings import Settings

# (path, method) -> tests as "module.test_name" inside api/tests; None marks a route deliberately left untested.
TESTED_ROUTES: dict[tuple[str, str], list[str | None]] = {
    ("/health", "GET"): ["test_app.test_health_is_up_and_not_logged"],
    ("/api/v1/projects", "GET"): ["test_app.test_projects_lists_fact_with_repository"],
    ("/api/v1/runs", "POST"): ["test_runs.test_create_and_get_run", "test_runs.test_create_run_errors"],
    ("/api/v1/runs", "GET"): ["test_runs.test_list_runs_filters"],
    ("/api/v1/runs/start", "POST"): [
        "test_run_start.test_refuses_unfinished_dependencies",
        "test_run_start.test_unknown_task_and_project",
        "test_run_start.test_temporal_down_is_503",
        "test_run_start.test_start_runs_the_project_workflow",
        "test_run_start.test_start_refused_by_temporal_keeps_the_number",
        "test_run_start.test_id_taken_in_temporal_moves_to_the_next",
        "test_run_start_scheduled.test_scheduler_start_refused_once_the_task_has_a_run",
        "test_run_start_scheduled.test_scheduled_start_refused_only_when_its_pool_is_full",
    ],
    ("/api/v1/scheduler/tick", "POST"): [
        "test_scheduler_tick.test_tick_starts_in_plan_order_within_the_caps",
        "test_scheduler_switch.test_prod_has_no_loop_and_no_switch",
    ],
    ("/api/v1/scheduler/preview", "GET"): [
        "test_scheduler.test_preview_order_reasons_and_usage",
        "test_scheduler.test_failure_cap_and_its_reset_by_a_manual_run",
        "test_scheduler_tick.test_full_window_starts_nothing",
    ],
    ("/api/v1/scheduler/active", "GET"): ["test_scheduler.test_active_runs"],
    ("/api/v1/scheduler", "GET"): [
        "test_scheduler_switch.test_dev_loop_is_on_at_start_and_follows_the_switch",
        "test_scheduler_switch.test_prod_has_no_loop_and_no_switch",
    ],
    ("/api/v1/scheduler/on", "POST"): ["test_scheduler_switch.test_dev_loop_is_on_at_start_and_follows_the_switch"],
    ("/api/v1/scheduler/off", "POST"): ["test_scheduler_switch.test_dev_loop_is_on_at_start_and_follows_the_switch"],
    ("/api/v1/runs/{run_id}", "GET"): ["test_runs.test_create_and_get_run"],
    ("/api/v1/runs/{run_id}", "PATCH"): ["test_runs.test_patch_run"],
    ("/api/v1/runs/{run_id}/steps", "POST"): ["test_runs.test_steps_lifecycle_and_order"],
    ("/api/v1/steps/{step_id}", "PATCH"): ["test_runs.test_steps_lifecycle_and_order"],
    ("/api/v1/runs/{run_id}/events", "POST"): ["test_runs.test_events_batch"],
    ("/api/v1/runs/{run_id}/gates/{gate_id}", "POST"): [
        "test_human.test_gate_steer_and_status",
        "test_human.test_finished_workflow_is_a_conflict",
        "test_human.test_unknown_run_and_bad_bodies",
    ],
    ("/api/v1/runs/{run_id}/instructions", "POST"): [
        "test_human.test_gate_steer_and_status",
        "test_human.test_finished_workflow_is_a_conflict",
        "test_human.test_unknown_run_and_bad_bodies",
    ],
    ("/api/v1/runs/{run_id}/status", "GET"): [
        "test_human.test_gate_steer_and_status",
        "test_human.test_unknown_run_and_bad_bodies",
    ],
    ("/api/v1/runs/{run_id}/stop", "POST"): [
        "test_stop.test_stop_cancels_the_workflow_with_the_reason",
        "test_stop.test_stop_refuses_a_finished_or_unknown_run",
    ],
    ("/api/v1/artifacts/{key:path}", "PUT"): ["test_artifacts.test_upload_download"],
    ("/api/v1/artifacts/{key:path}", "GET"): ["test_artifacts.test_upload_download"],
    ("/api/v1/notifications", "POST"): [
        "test_notifications.test_same_dedupe_key_is_one_notification",
        "test_notifications.test_notification_errors",
    ],
    ("/api/v1/notifications", "GET"): [
        "test_notifications.test_same_dedupe_key_is_one_notification",
        "test_notifications.test_unread_filter_newest_first_and_mark_read",
    ],
    ("/api/v1/notifications/{notification_id}/read", "POST"): [
        "test_notifications.test_unread_filter_newest_first_and_mark_read",
        "test_notifications.test_notification_errors",
    ],
    ("/api/v1/usage/{account}", "PUT"): [
        "test_usage.test_report_then_list",
        "test_usage.test_newer_report_wins_and_unmentioned_windows_stay",
    ],
    ("/api/v1/usage", "GET"): ["test_usage.test_report_then_list"],
}


def iter_route_keys() -> Iterator[tuple[str, str]]:
    """Yield `(path, method)` for every API route. HEAD and OPTIONS are implicit and skipped."""
    for route in create_app(Settings()).routes:
        # FastAPI wraps included routers lazily, so `app.routes` has no flat APIRoute objects.
        # Only routers we include count: the built-in /docs and /openapi.json routes are skipped.
        contexts = route.effective_route_contexts() if hasattr(route, "effective_route_contexts") else []
        for ctx in contexts:
            for method in sorted(ctx.methods or ()):
                if method not in {"HEAD", "OPTIONS"}:
                    yield ctx.path, method


def _load_test_module(name: str) -> object:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / f"{name}.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_routes_have_tests() -> None:
    app_keys = set(iter_route_keys())
    assert app_keys, "no routes found: FastAPI may have changed how included routers are exposed"
    listed = set(TESTED_ROUTES)
    assert app_keys - listed == set(), f"Routes missing from TESTED_ROUTES: {sorted(app_keys - listed)}"
    assert listed - app_keys == set(), f"TESTED_ROUTES lists routes not in the app: {sorted(listed - app_keys)}"


def test_listed_tests_exist() -> None:
    for (path, method), tests in TESTED_ROUTES.items():
        assert tests, f"{method} {path} needs at least one test, or [None] to mark it untested"
        for name in filter(None, tests):
            module_name, func_name = name.rsplit(".", 1)
            func = getattr(_load_test_module(module_name), func_name, None)
            assert callable(func), f"{name} (for {method} {path}) is not a callable"
            assert func_name.startswith("test_"), f"{name} (for {method} {path}) is not named test_*"
