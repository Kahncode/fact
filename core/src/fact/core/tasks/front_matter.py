"""The YAML front matter of a task file, read from its text alone (a file task's file, wherever it was read)."""

import codecs
from typing import Any

import yaml

BOM = codecs.BOM_UTF8.decode()  # a file read as plain UTF-8 (`git show`) keeps it


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Split a task file into its YAML front matter and its body; `ValueError` when it has none."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        msg = "missing YAML front matter"
        raise ValueError(msg)
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            meta = yaml.safe_load("".join(lines[1:i]))
            if not isinstance(meta, dict):
                msg = "front matter is not a mapping"
                raise ValueError(msg)
            return meta, "".join(lines[i + 1 :]).strip("\r\n")
    msg = "unterminated YAML front matter"
    raise ValueError(msg)


def task_file_id(text: str) -> str | None:
    """Return the `id` in a task file's front matter; None when the text is not a task file."""
    try:
        meta, _ = split_front_matter(text.removeprefix(BOM))
    except ValueError, yaml.YAMLError:
        return None
    return str(meta["id"]) if "id" in meta else None
