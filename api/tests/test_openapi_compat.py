"""The live OpenAPI schema matches the committed `api/openapi.json` snapshot."""

import copy
import json
from pathlib import Path

import pytest
from fact.api.openapi_compat import (
    REGENERATE_COMMAND,
    SNAPSHOT_PATH,
    compare,
    dump,
    live_schema,
    read_snapshot,
    write_snapshot,
)
from fact.core.settings import Settings

PROJECTS = "/api/v1/projects"
PROJECT_OUT = "/components/schemas/ProjectOut"


@pytest.fixture
def published() -> dict:
    return read_snapshot()


def test_live_schema_matches_snapshot(settings: Settings) -> None:
    differences = compare(read_snapshot(), live_schema(settings))
    assert not differences, differences.report()


def test_snapshot_file_is_exactly_what_the_script_writes(settings: Settings) -> None:
    assert SNAPSHOT_PATH.read_bytes() == dump(live_schema(settings)).encode("utf-8")


def test_write_snapshot_is_sorted_and_stable(tmp_path: Path, published: dict) -> None:
    path = tmp_path / "openapi.json"
    write_snapshot(published, path)
    first = path.read_bytes()
    write_snapshot(json.loads(first), path)
    assert path.read_bytes() == first
    assert b"\r\n" not in first
    assert list(json.loads(first)["paths"]) == sorted(published["paths"])


def test_removed_route_is_breaking(published: dict) -> None:
    live = copy.deepcopy(published)
    del live["paths"][PROJECTS]
    differences = compare(published, live)
    assert "removed /paths/~1api~1v1~1projects" in differences.breaking
    assert "removed /paths/~1api~1v1~1projects" in differences.report()


def test_removed_method_is_breaking(published: dict) -> None:
    live = copy.deepcopy(published)
    del live["paths"][PROJECTS]["get"]
    assert compare(published, live).breaking == ["removed /paths/~1api~1v1~1projects/get"]


def test_removed_response_field_is_breaking(published: dict) -> None:
    live = copy.deepcopy(published)
    schema = live["components"]["schemas"]["ProjectOut"]
    del schema["properties"]["home_repo"]
    schema["required"].remove("home_repo")
    breaking = compare(published, live).breaking
    assert f"removed {PROJECT_OUT}/properties/home_repo" in breaking
    assert f'removed {PROJECT_OUT}/required: "home_repo"' in breaking


def test_changed_field_type_is_breaking(published: dict) -> None:
    live = copy.deepcopy(published)
    live["components"]["schemas"]["ProjectOut"]["properties"]["id"]["type"] = "string"
    differences = compare(published, live)
    assert differences.breaking == [f'changed {PROJECT_OUT}/properties/id/type: "integer" -> "string"']
    assert not differences.added


def test_removed_parameter_is_breaking(published: dict) -> None:
    operation = next(
        (path, method, op)
        for path, methods in published["paths"].items()
        for method, op in methods.items()
        if op.get("parameters")
    )
    path, method, op = operation
    parameter = op["parameters"][0]
    live = copy.deepcopy(published)
    live["paths"][path][method]["parameters"].pop(0)
    breaking = compare(published, live).breaking
    assert any(f"{parameter['in']}:{parameter['name']}" in item for item in breaking), breaking


def test_newly_required_field_is_breaking(published: dict) -> None:
    live = copy.deepcopy(published)
    schema = live["components"]["schemas"]["ProjectOut"]
    schema["properties"]["owner"] = {"type": "string"}
    schema["required"].append("owner")
    differences = compare(published, live)
    assert f'added {PROJECT_OUT}/required: "owner"' in differences.breaking
    assert f"added {PROJECT_OUT}/properties/owner" in differences.added


def test_added_route_fails_until_snapshot_is_regenerated(published: dict, tmp_path: Path) -> None:
    live = copy.deepcopy(published)
    live["paths"]["/api/v1/widgets"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    differences = compare(published, live)
    assert differences
    assert not differences.breaking
    assert differences.added == ["added /paths/~1api~1v1~1widgets"]
    assert REGENERATE_COMMAND in differences.report()

    regenerated = tmp_path / "openapi.json"
    write_snapshot(live, regenerated)
    assert not compare(read_snapshot(regenerated), live)
