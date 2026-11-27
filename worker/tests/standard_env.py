"""Where the `Standard` workflow tests find the workflow, and the test worker's options."""

import importlib
import sys
from datetime import timedelta
from types import ModuleType
from typing import Any

from fact.core.settings import REPO_ROOT

WORKFLOWS_DIR = REPO_ROOT / ".fact" / "workflows"
WORKFLOW_QUEUE = "wf-test"
RUNNER_QUEUE = "runner-test"  # the queue the fake workspace handle names, not the default `sandbox`
IDENTITY = "test-worker"  # a fixed worker identity instead of the host name
# Heartbeats reach the server at once, so an activity sees its cancellation without the 60 s default throttle
OPTIONS: dict[str, Any] = {"identity": IDENTITY, "max_heartbeat_throttle_interval": timedelta(milliseconds=50)}


def load_standard() -> ModuleType:
    """Import `.fact/workflows/standard.py` by name, so the workflow sandbox can import it again."""
    if str(WORKFLOWS_DIR) not in sys.path:
        sys.path.insert(0, str(WORKFLOWS_DIR))
    return importlib.import_module("standard")
