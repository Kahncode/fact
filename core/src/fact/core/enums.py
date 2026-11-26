"""Enums stored as plain strings in the DB (see `docs/spec/05-domain-model.md`, `03-temporal.md`, `10-observability.md`)."""

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    FINISHED = "finished"


class RunOutcome(StrEnum):
    PR_OPENED = "pr_opened"  # the run ended with its change set open for review (M1, before the merge stage)
    NEEDS_HUMAN = "needs_human"  # the run gave up and needs a person, such as an exhausted checks loop
    MERGED = "merged"
    CLOSED = "closed"
    ABANDONED = "abandoned"
    FAILED = "failed"
    DROPPED = "dropped"  # a person dropped the run (`fact drop`), or rejected its plan at the `approve_plan` gate


class RunTrigger(StrEnum):
    MANUAL = "manual"  # `fact run` or `POST /runs/start` from a person: resets the scheduler's failure count
    SCHEDULER = "scheduler"  # started by the scheduler's tick


class StepKind(StrEnum):
    AGENT = "agent"
    SHELL = "shell"
    GITHUB = "github"
    GATE = "gate"
    NOTIFY = "notify"


class StepStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    FINISHED = "finished"


class StepOutcome(StrEnum):
    PASSED = "passed"
    FAILED_CHECKS = "failed_checks"
    FAILED_ERROR = "failed_error"
    NEEDS_HUMAN = "needs_human"
    OVER_BUDGET = "over_budget"
    STOPPED_BY_MONITOR = "stopped_by_monitor"
    DROPPED = "dropped"
    RATE_LIMITED = "rate_limited"  # the agent hit the subscription usage limit; the workflow waits and resumes it


class NotificationKind(StrEnum):
    GATE_WAITING = "gate_waiting"  # a run waits at a gate for a person's answer
    PRS_READY = "prs_ready"  # a run's PRs are open and green, ready for review
    RUN_FAILED = "run_failed"  # a run ended badly: failed, or gave up and needs a person
    RUN_FINISHED = "run_finished"  # a run ended any other way worth telling a person about
