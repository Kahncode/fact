import sys
from pathlib import Path

# The scripts are standalone (run as `python scripts/task_loop.py`), so they import each other as top-level modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
