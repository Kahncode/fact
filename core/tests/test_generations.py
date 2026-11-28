"""Worker generation records: numbered in creation order, the newest last."""

from pathlib import Path

import pytest
from fact.core import generations
from fact.core.generations import Generation, GenerationError


def test_each_generation_is_newer_than_the_last_and_keeps_its_projects(tmp_path: Path) -> None:
    first = generations.create(tmp_path, "a" * 40)
    generations.write(tmp_path, Generation(first.id, first.factory, {"acme": "c" * 40}))
    for _ in range(9):
        generations.create(tmp_path, "b" * 40)
    (tmp_path / "notes.json").write_text("{}", encoding="utf-8")  # not a generation: skipped
    (tmp_path / "11-bbbbbbbbbbbb.json").write_text("{", encoding="utf-8")  # unreadable: skipped

    found = generations.all_generations(tmp_path)

    assert [g.number for g in found] == list(range(1, 11))  # numeric order: 10 is after 9
    assert found[0] == Generation("1-aaaaaaaaaaaa", "a" * 40, {"acme": "c" * 40})
    assert found[0].versions() == [("wf-acme", f"{'a' * 12}+{'c' * 40}")]
    assert generations.newest(tmp_path) == found[-1]


def test_an_id_that_is_not_a_generation_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GenerationError, match="invalid generation id"):
        generations.read(tmp_path, "../escape")
