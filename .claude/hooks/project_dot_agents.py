#!/usr/bin/env python3
"""
Project <root>/.agents/{skills,agents} into <root>/.claude/{skills,agents} for Claude Code.

.agents/ holds the canonical copies: skills (read directly by Codex, OpenCode
and pi) and subagents (our own convention, in Claude's format). Claude Code
only reads .claude/skills and .claude/agents, so this script exposes the same
directories there.

Modes:
  auto  (default) link if possible (symlink, or junction on Windows), else copy;
        a managed copy is upgraded to a link as soon as linking works
  link  link only; fail if linking is not possible
  copy  always copy (re-run to re-sync; unchanged content is left alone)

Safety: nothing is deleted before its replacement exists. An unmanaged
.claude/<name> (--force) and a managed copy that was edited locally are moved
to .claude/<name>.bak-<timestamp>, never deleted.

Idempotent. For each projected directory the output is a status line, then
`PROJECTION_<NAME>=<symlink|junction|copy>` (PROJECTION_SKILLS, PROJECTION_AGENTS).
Exit codes: 0 ok, 1 error. Stdlib only; Windows, macOS, Linux and WSL.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

COPY_MARKER = ".kahnclaude-copy"  # holds the content digest of the managed copy
PROJECTED = ("skills", "agents")  # .agents/<name> -> .claude/<name>
REPARSE_POINT = 0x400
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # NTFS junction


def link_kind(path: Path) -> str | None:
    """'symlink' or 'junction' if path is such a link (not followed), else None."""
    if path.is_symlink():
        return "symlink"
    if os.name == "nt":
        try:
            st = os.lstat(path)
        except OSError:
            return None
        is_reparse = getattr(st, "st_file_attributes", 0) & REPARSE_POINT
        if is_reparse and getattr(st, "st_reparse_tag", 0) == IO_REPARSE_TAG_MOUNT_POINT:
            return "junction"
    return None


def remove_link(path: Path, kind: str) -> None:
    """Remove a symlink or junction without touching its target."""
    if kind == "symlink":
        path.unlink()
    else:
        path.rmdir()


def make_link(src: Path, dst: Path) -> str:
    """Create dst -> src. Returns 'symlink' or 'junction'; raises OSError."""
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return "junction"
    try:
        dst.symlink_to(os.path.relpath(src, dst.parent), target_is_directory=True)
    except NotImplementedError as exc:
        raise OSError(str(exc)) from exc
    return "symlink"


def tree_digest(path: Path) -> str:
    """Stable digest of all file paths and contents under path (marker excluded)."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for name in sorted(filenames):
            if name == COPY_MARKER:
                continue
            full = Path(dirpath) / name
            h.update(full.relative_to(path).as_posix().encode())
            h.update(b"\0")
            h.update(full.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def read_marker(dst: Path) -> str | None:
    try:
        return (dst / COPY_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def backup_path(dst: Path) -> Path:
    return dst.with_name(f"{dst.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")


def replace_dir(new: Path, dst: Path) -> None:
    """Swap `new` into place at dst; the old dst is removed only after success."""
    old = dst.with_name(dst.name + ".old")
    if old.exists():
        shutil.rmtree(old)
    had_old = dst.exists()
    if had_old:
        dst.rename(old)
    try:
        new.rename(dst)
    except OSError:
        if had_old:
            old.rename(dst)
        raise
    if had_old:
        shutil.rmtree(old, ignore_errors=True)


def try_link(src: Path, dst: Path) -> str | None:
    """Link dst -> src, replacing a managed copy safely. None if linking failed."""
    aside = None
    if dst.exists():
        aside = dst.with_name(dst.name + ".old")
        if aside.exists():
            shutil.rmtree(aside)
        dst.rename(aside)
    try:
        kind = make_link(src, dst)
    except OSError:
        if aside is not None:
            aside.rename(dst)
        return None
    if aside is not None:
        shutil.rmtree(aside, ignore_errors=True)
    return kind


def sync_copy(src: Path, dst: Path, key: str) -> None:
    digest = tree_digest(src)
    if dst.is_dir() and read_marker(dst) == digest and tree_digest(dst) == digest:
        print(f"OK: {dst.name} copy already up to date")
        print(f"{key}=copy")
        return
    new = dst.with_name(dst.name + ".new")
    if new.exists():
        shutil.rmtree(new)
    shutil.copytree(src, new)
    (new / COPY_MARKER).write_text(digest + "\n", encoding="utf-8")
    replace_dir(new, dst)
    print(f"COPIED: {src} -> {dst}")
    print(f"{key}=copy")


def prepare_dst(src: Path, dst: Path, force: bool) -> bool:
    """Make way for a new projection at dst (a real directory, not a link). False on error."""
    if not dst.is_dir():
        if dst.exists():
            print(f"ERROR: {dst} exists and is not a directory", file=sys.stderr)
            return False
        return True
    marker = read_marker(dst)
    if marker is None:
        if not force:
            print(
                f"ERROR: {dst} contains unmanaged files. Move them into {src} "
                "or pass --force (they are moved to a .bak folder, not deleted).",
                file=sys.stderr,
            )
            return False
        bak = backup_path(dst)
        dst.rename(bak)
        print(f"BACKUP: unmanaged {dst} moved to {bak}")
    elif tree_digest(dst) != marker:
        bak = backup_path(dst)
        shutil.copytree(dst, bak)
        print(f"WARNING: {dst} was edited locally; copy saved to {bak}", file=sys.stderr)
    return True


def project_one(root: Path, name: str, mode: str, force: bool) -> int:
    """Project root/.agents/<name> into root/.claude/<name>. Returns an exit code."""
    src = root / ".agents" / name
    dst = root / ".claude" / name
    key = f"PROJECTION_{name.upper()}"
    if not src.is_dir():
        print(f"ERROR: {src} does not exist", file=sys.stderr)
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)

    kind = link_kind(dst)
    if kind:
        if mode != "copy" and os.path.realpath(dst) == os.path.realpath(src):
            print(f"OK: {name} already linked")
            print(f"{key}={kind}")
            return 0
        remove_link(dst, kind)  # stale link, or switching to copy mode
    elif not prepare_dst(src, dst, force):
        return 1

    if mode in ("auto", "link"):
        kind = try_link(src, dst)
        if kind:
            print(f"LINKED: {dst} -> {src}")
            print(f"{key}={kind}")
            return 0
        if mode == "link":
            print("ERROR: cannot link (no symlink/junction permission)", file=sys.stderr)
            return 1

    sync_copy(src, dst, key)
    return 0


def project(root: Path, mode: str, force: bool) -> int:
    """Project every directory in PROJECTED, each independently; 1 if any of them failed."""
    rc = 0
    for name in PROJECTED:
        try:
            rc = max(rc, project_one(root, name, mode, force))
        except (OSError, shutil.Error) as exc:
            print(f"ERROR: {name}: {exc}", file=sys.stderr)
            rc = 1
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("root", nargs="?", default=".", help="project root (default: .)")
    parser.add_argument("--mode", choices=["auto", "link", "copy"], default="auto")
    parser.add_argument("--force", action="store_true", help="move an unmanaged .claude/<name> aside and replace it")
    args = parser.parse_args()
    try:
        return project(Path(args.root).resolve(), args.mode, args.force)
    except (OSError, shutil.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
