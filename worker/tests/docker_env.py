"""Real-Docker tests skip when Docker (running Linux containers) is not there; CI on Linux always runs them."""

import shutil
import subprocess

import pytest


def _linux_docker() -> bool:
    if shutil.which("docker") is None:
        return False
    info = subprocess.run(
        ["docker", "info", "--format", "{{.OSType}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return info.returncode == 0 and info.stdout.strip() == "linux"


needs_docker = pytest.mark.skipif(not _linux_docker(), reason="needs Docker running Linux containers")
