"""
Stands in for `claude` in tests: records argv and stdin, replays a stream-json fixture, then exits.

Driven by environment variables: FAKE_STREAM (fixture to replay), FAKE_RECORD (file for argv, prompt and env),
FAKE_EXIT (exit code), FAKE_STDERR (text for stderr), FAKE_HANG (sleep forever after the first line),
FAKE_SKIP_STDIN (exit at once without reading stdin), FAKE_LONG_LINE (print a line of that many bytes first).
"""

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    if os.environ.get("FAKE_SKIP_STDIN"):
        return 1
    prompt = sys.stdin.read()
    if record := os.environ.get("FAKE_RECORD"):
        auto_memory = os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY")
        recorded = {"argv": sys.argv[1:], "prompt": prompt, "disable_auto_memory": auto_memory}
        Path(record).write_text(json.dumps(recorded), encoding="utf-8")
    sys.stderr.write(os.environ.get("FAKE_STDERR", ""))
    sys.stderr.flush()
    lines = (
        Path(os.environ["FAKE_STREAM"]).read_text(encoding="utf-8").splitlines() if "FAKE_STREAM" in os.environ else []
    )
    if long := int(os.environ.get("FAKE_LONG_LINE", "0")):
        sys.stdout.write("x" * long + "\n")
    for i, line in enumerate(lines):
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        if i == 0 and os.environ.get("FAKE_HANG"):
            time.sleep(3600)
    return int(os.environ.get("FAKE_EXIT", "0"))


if __name__ == "__main__":
    raise SystemExit(main())
