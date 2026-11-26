"""
Write the API's OpenAPI schema to `api/openapi.json`, sorted and stable so diffs stay small.

Run after any intended API change: `uv run python scripts/openapi_snapshot.py`.
"""

from fact.api.openapi_compat import SNAPSHOT_PATH, live_schema, write_snapshot


def main() -> None:
    write_snapshot(live_schema())
    print(f"Wrote {SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
