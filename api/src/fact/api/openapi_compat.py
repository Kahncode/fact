"""
The OpenAPI snapshot (`api/openapi.json`) and the compatibility check against it.

A published route or field never changes: `compare` lists every difference between the snapshot and the
live schema, split into breaking ones (something removed or changed) and additions (regenerate the snapshot).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fact.api.app import create_app
from fact.core.settings import Settings

SNAPSHOT_PATH = Path(__file__).resolve().parents[3] / "openapi.json"
REGENERATE_COMMAND = "uv run python scripts/openapi_snapshot.py"

type Json = Any


@dataclass
class Differences:
    breaking: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        """Return whether anything differs."""
        return bool(self.breaking or self.added)

    def report(self) -> str:
        lines: list[str] = []
        if self.breaking:
            lines.append("Breaking API changes (a published route or field never changes):")
            lines += [f"  - {item}" for item in self.breaking]
        if self.added:
            lines.append(f"API additions not in the snapshot; run `{REGENERATE_COMMAND}`:")
            lines += [f"  - {item}" for item in self.added]
        return "\n".join(lines)


def live_schema(settings: Settings | None = None) -> dict[str, Json]:
    return create_app(settings).openapi()


def dump(schema: dict[str, Json]) -> str:
    """Render the schema with sorted keys and LF endings so diffs stay small and output is stable."""
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_snapshot(schema: dict[str, Json], path: Path = SNAPSHOT_PATH) -> None:
    path.write_bytes(dump(schema).encode("utf-8"))


def read_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, Json]:
    return json.loads(path.read_text(encoding="utf-8"))


def compare(published: Json, live: Json) -> Differences:
    """List what `live` removed or changed from `published` (breaking) and what it added."""
    differences = Differences()
    _walk(published, live, "", differences)
    return differences


def _walk(old: Json, new: Json, path: str, out: Differences) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        _walk_dict(old, new, path, out)
    elif isinstance(old, list) and isinstance(new, list):
        _walk_list(old, new, path, out)
    elif old != new:
        out.breaking.append(f"changed {path or '/'}: {_short(old)} -> {_short(new)}")


def _walk_dict(old: dict[str, Json], new: dict[str, Json], path: str, out: Differences) -> None:
    for key, value in old.items():
        child = f"{path}/{_escape(key)}"
        if key not in new:
            out.breaking.append(f"removed {child}")
        else:
            _walk(value, new[key], child, out)
    out.added += [f"added {path}/{_escape(key)}" for key in new if key not in old]


def _walk_list(old: list[Json], new: list[Json], path: str, out: Differences) -> None:
    if _is_parameter_list(old + new):
        _walk_dict(_parameters_by_key(old), _parameters_by_key(new), path, out)
        return
    old_items = {_canonical(item): item for item in old}
    new_items = {_canonical(item): item for item in new}
    out.breaking += [f"removed {path}: {_short(item)}" for key, item in old_items.items() if key not in new_items]
    # A newly required field breaks every client that does not send it, so it is not a mere addition.
    target = out.breaking if path.endswith("/required") else out.added
    target += [f"added {path}: {_short(item)}" for key, item in new_items.items() if key not in old_items]


def _is_parameter_list(items: list[Json]) -> bool:
    return bool(items) and all(isinstance(item, dict) and "name" in item and "in" in item for item in items)


def _parameters_by_key(items: list[dict[str, Json]]) -> dict[str, Json]:
    return {f"{item['in']}:{item['name']}": item for item in items}


def _canonical(value: Json) -> str:
    return json.dumps(value, sort_keys=True)


def _short(value: Json, limit: int = 80) -> str:
    text = _canonical(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _escape(key: str) -> str:
    """JSON Pointer escaping, so `/api/v1/runs` stays one path segment."""
    return key.replace("~", "~0").replace("/", "~1")
