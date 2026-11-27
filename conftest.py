"""Root test setup: shared by every package's tests."""

import os

# Tests read this checkout's fact.yaml, never one a developer's shell points `FACT_CONFIG` at: an outside file
# (the main checkout seen from a worktree, say) can carry keys this branch has removed or renamed.
os.environ.pop("FACT_CONFIG", None)
