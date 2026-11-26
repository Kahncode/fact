"""What a `Standard` test run did, read from the fake activities' calls."""

from typing import Protocol

from fact.core.activity_contract import AGENT_STEP, RECORD_RUN_FINISHED, SHELL_STEP, ShellStepInput
from fact.core.enums import RunOutcome


class Calls(Protocol):
    def of(self, name: str) -> list: ...


def agent_sessions(fakes: Calls) -> list[tuple[str, int, str]]:
    return [(s.stage_id, s.attempt, s.prompt_name) for s in fakes.of(AGENT_STEP)]


def finished(fakes: Calls) -> RunOutcome:
    (finished,) = fakes.of(RECORD_RUN_FINISHED)
    return finished.outcome


def checks(fakes: Calls) -> list[ShellStepInput]:
    return [s for s in fakes.of(SHELL_STEP) if s.stage_id == "checks"]
