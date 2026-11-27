"""
The cost comment on a run's PR: time, turns, peak context, tokens and cost per step and per harness and model.

`render_cost_comment` turns a run as the runs API returns it (`GET /runs/{id}`, with its steps) into markdown;
`upsert_comment` creates fact's comment on the PR, or updates the one carrying `COST_MARKER`. Only reported usage is
shown: a cost the harness did not report reads `unknown` and is left out of the totals, which say how many are missing.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

COST_MARKER = "<!-- fact:cost -->"
COMMENTS_PAGE = 100
NONE = "—"
UNKNOWN = "unknown"
TOTAL = "**Total**"
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600


@dataclass(frozen=True)
class StepCost:
    stage: str
    attempt: int
    route: str  # `harness / model`, or `—` for steps without an agent
    duration_s: float | None  # wall time; None while the step has not ended
    wait_s: float
    tokens_in: int  # every prompt token; the cache counts are part of it
    tokens_out: int
    cache_read: int
    cache_write: int
    cost: Decimal | None  # None: the harness reported no cost
    partial: bool = False  # the session was cut short: tokens of the messages received
    turns: int | None = None  # agent steps: model calls over the whole session
    peak_context: int | None = None  # agent steps: the input of the session's largest turn
    unknown_costs: int = 0  # for totals: agent steps whose cost is None
    name: str | None = None  # the step's name within its stage, such as `lint`; None for totals

    @property
    def label(self) -> str:
        return f"{self.stage}:{self.name}" if self.name else self.stage

    @property
    def fresh_in(self) -> int:
        return max(self.tokens_in - self.cache_read - self.cache_write, 0)


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return max((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 0.0)


def _route(step: dict[str, Any]) -> str:
    harness, model = step.get("harness"), step.get("model")
    if not harness and not model:
        return NONE
    return " / ".join(part for part in (harness, model) if part)


def _step_cost(s: dict[str, Any]) -> StepCost:
    cost = s.get("cost_usd")
    return StepCost(
        stage=s["stage_id"],
        attempt=s["attempt"],
        route=_route(s),
        duration_s=_seconds(s.get("started_at"), s.get("ended_at")),
        wait_s=float(s.get("wait_seconds") or 0),
        tokens_in=int(s.get("tokens_in") or 0),
        tokens_out=int(s.get("tokens_out") or 0),
        cache_read=int(s.get("cache_read_tokens") or 0),
        cache_write=int(s.get("cache_write_tokens") or 0),
        cost=None if cost is None else Decimal(str(cost)),
        partial=bool(s.get("usage_partial")),
        turns=s.get("turns"),
        peak_context=s.get("peak_context_tokens"),
        name=s.get("name"),
    )


def step_costs(run: dict[str, Any]) -> list[StepCost]:
    return [_step_cost(s) for s in run.get("steps", [])]


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "running"
    total = round(seconds)
    if total < SECONDS_PER_MINUTE:
        return f"{total}s"
    if total < SECONDS_PER_HOUR:
        return f"{total // SECONDS_PER_MINUTE}m {total % SECONDS_PER_MINUTE:02d}s"
    minutes = (total % SECONDS_PER_HOUR) // SECONDS_PER_MINUTE
    return f"{total // SECONDS_PER_HOUR}h {minutes:02d}m"


def _count(n: int | None) -> str:
    return f"{n:,}" if n else NONE


def _usage_cells(s: StepCost) -> list[str]:
    tokens = [_count(s.fresh_in), _count(s.cache_read), _count(s.cache_write), _count(s.tokens_out)]
    return [_count(s.turns), _count(s.peak_context), *tokens]


def _cost(s: StepCost) -> str:
    if s.stage == TOTAL:
        known = f"${s.cost or 0:,.2f}"
        return f"{known} + {s.unknown_costs} {UNKNOWN}" if s.unknown_costs else known
    if s.cost is None:
        return UNKNOWN if s.route != NONE else NONE
    return f"${s.cost:,.2f}"


def _total(steps: list[StepCost], stage: str = TOTAL) -> StepCost:
    durations = [s.duration_s for s in steps if s.duration_s is not None]
    return StepCost(
        stage=stage,
        attempt=len(steps),
        route="",
        duration_s=sum(durations, 0.0),
        wait_s=sum(s.wait_s for s in steps),
        tokens_in=sum(s.tokens_in for s in steps),
        tokens_out=sum(s.tokens_out for s in steps),
        cache_read=sum(s.cache_read for s in steps),
        cache_write=sum(s.cache_write for s in steps),
        cost=sum((s.cost for s in steps if s.cost is not None), Decimal(0)),
        partial=any(s.partial for s in steps),
        turns=sum(s.turns or 0 for s in steps),
        peak_context=max((s.peak_context or 0 for s in steps), default=0),  # the largest step's, never a sum
        unknown_costs=sum(1 for s in steps if s.cost is None and s.route != NONE),
    )


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


USAGE_HEADERS = ["Turns", "Peak context", "Uncached input", "Cache read", "Cache write", "Output"]
_RIGHT = "---:"


def _steps_table(steps: list[StepCost]) -> list[str]:
    lines = [
        _row(["Stage", "Attempt", "Harness / model", "Duration", "Wait", *USAGE_HEADERS, "Cost"]),
        _row(["---", _RIGHT, "---", _RIGHT, _RIGHT, *[_RIGHT] * len(USAGE_HEADERS), _RIGHT]),
    ]
    for s in [*steps, _total(steps)]:
        attempt = f"{s.attempt} steps" if s.stage == TOTAL else str(s.attempt)
        stage = f"{s.label} (cut short)" if s.partial and s.stage != TOTAL else s.label
        timing = [format_duration(s.duration_s), format_duration(s.wait_s) if s.wait_s else NONE]
        lines.append(_row([stage, attempt, s.route, *timing, *_usage_cells(s), _cost(s)]))
    return lines


def _routes_table(steps: list[StepCost]) -> list[str]:
    by_route: dict[str, list[StepCost]] = defaultdict(list)
    for s in steps:
        if s.route != NONE:
            by_route[s.route].append(s)
    if not by_route:
        return []
    lines = [
        _row(["Harness / model", "Steps", "Duration", *USAGE_HEADERS, "Cost"]),
        _row(["---", _RIGHT, _RIGHT, *[_RIGHT] * len(USAGE_HEADERS), _RIGHT]),
    ]
    for route, group in by_route.items():
        t = _total(group)
        lines.append(_row([route, str(len(group)), format_duration(t.duration_s), *_usage_cells(t), _cost(t)]))
    return ["", "**Per harness and model**", "", *lines]


def _headline(total: StepCost, steps: int) -> str:
    text = f"**fact run cost:** ${total.cost or 0:,.2f} over {format_duration(total.duration_s)} ({steps} steps)"
    if total.unknown_costs:
        text += f"; cost not reported for {total.unknown_costs} agent steps"
    return text


FOOTNOTE = (
    "Durations are wall time; wait is time spent on a person. Turns are the model calls of the whole session, "
    "including the parts Claude Code woke again. Tokens are summed over every turn of the session, and the four parts "
    "are billed at different rates: uncached input is only a few tokens per turn, because Claude Code caches each "
    "prompt up to its end, so new input is billed as cache write; cache read counts the cached prompt again on every "
    "turn; output is the tokens the model wrote. Peak context is the largest single turn's input (uncached input, "
    "cache read and cache write); the totals show the largest step's, not a sum. Cost is what Claude Code reported "
    "(`total_cost_usd`, at API prices even on a subscription login). A session cut short reports the tokens of the "
    "messages received and no cost."
)


def render_cost_comment(run: dict[str, Any]) -> str:
    """Markdown for the PR comment, starting with `COST_MARKER` so fact finds and updates it later."""
    steps = step_costs(run)
    total = _total(steps)
    body = [COST_MARKER, "### Cost", "", _headline(total, len(steps)), "", *_steps_table(steps), *_routes_table(steps)]
    body += ["", f"<sub>Run `{run.get('id', '?')}`. {FOOTNOTE}</sub>"]
    return "\n".join(body) + "\n"


async def find_cost_comment(client: httpx.AsyncClient, full_name: str, number: int) -> int | None:
    """Return the id of the PR's comment that carries `COST_MARKER`, or None."""
    page = 1
    while True:
        params = {"per_page": str(COMMENTS_PAGE), "page": str(page)}
        comments = (await client.get(f"/repos/{full_name}/issues/{number}/comments", params=params)).json()
        for comment in comments:
            if COST_MARKER in (comment.get("body") or ""):
                return comment["id"]
        if len(comments) < COMMENTS_PAGE:
            return None
        page += 1


async def upsert_comment(client: httpx.AsyncClient, full_name: str, number: int, body: str) -> bool:
    """Update fact's cost comment on the PR, or create it; True when it was created."""
    existing = await find_cost_comment(client, full_name, number)
    if existing is not None:
        await client.patch(f"/repos/{full_name}/issues/comments/{existing}", json={"body": body})
        return False
    await client.post(f"/repos/{full_name}/issues/{number}/comments", json={"body": body})
    return True
