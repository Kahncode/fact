"""
Project workflows import only `fact.sdk` and `fact.core` from fact.

`.fact/workflows/` is loaded by path, not imported as a package, so import-linter (`[tool.importlinter]`) cannot see
it; this test is its contract for those files.
"""

import ast
from pathlib import Path

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".fact" / "workflows"
ALLOWED = ("fact.sdk", "fact.core")


def _fact_imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return [name for name in names if name == "fact" or name.startswith("fact.")]


def test_project_workflows_import_only_sdk_and_core() -> None:
    files = sorted(WORKFLOWS_DIR.glob("*.py"))
    assert files, f"no workflows under {WORKFLOWS_DIR}"
    bad = [
        f"{path.name}: {name}"
        for path in files
        for name in _fact_imports(path)
        if not any(name == allowed or name.startswith(f"{allowed}.") for allowed in ALLOWED)
    ]
    assert bad == []
