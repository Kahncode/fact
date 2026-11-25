"""The stage manifest: the graph a workflow expects to run, declared with `@stages([...])` for the UI to draw."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from fact.core.enums import StepKind


@dataclass(frozen=True)
class Stage:
    """One node of a workflow's expected graph; `loops_to` is the stage a failure here loops back to."""

    id: str
    label: str
    kind: StepKind
    loops_to: str | None = None


def stages[T: type](manifest: Sequence[Stage]) -> Callable[[T], T]:
    """Class decorator: store the workflow's stage manifest on the class, for the UI to draw."""
    items = tuple(manifest)
    ids = [s.id for s in items]
    if len(set(ids)) != len(ids):
        msg = f"duplicate stage ids in {ids}"
        raise ValueError(msg)
    for stage in items:
        if stage.loops_to is not None and stage.loops_to not in ids:
            msg = f"stage {stage.id!r} loops to unknown stage {stage.loops_to!r}"
            raise ValueError(msg)

    def decorate(cls: T) -> T:
        cls.stage_manifest = items  # type: ignore[attr-defined]
        return cls

    return decorate
