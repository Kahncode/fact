"""Shared setup for the core tests."""

import sys
from pathlib import Path

# Test modules import shared helpers (such as `fake_github_issues`) by name; `--import-mode=importlib` adds no path.
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))
