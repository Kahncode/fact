"""
The cost comment on a run's PR: time, tokens and cost per step and per harness and model.

`render_cost_comment` turns a run as the runs API returns it (`GET /runs/{id}`, with its steps) into markdown;
`upsert_comment` creates fact's comment on the PR, or updates the one carrying `COST_MARKER`.
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
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600


@dataclass(frozen=True)
class StepCost:
    stage: str
    attempt: int
    route: str  # `harness / model`, or `—` for steps without an agent
    duration_s: float | None  # wall time; None while the step has not ended
    wait_s: float
    tokens_in: int
    tokens_out: int
    cost: Decimal


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return max((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 0.0)


def _route(step: dict[str, Any]) -> str:
    harness, model = step.get("harness"), step.get("model")
    if not harness and not model:
        return NONE
    return " / ".join(part for part in (harness, model) if part)


def step_costs(run: dict[str, Any]) -> list[StepCost]:
    return [
        StepCost(
            stage=s["stage_id"],
            attempt=s["attempt"],
            route=_route(s),
            duration_s=_seconds(s.get("started_at"), s.get("ended_at")),
            wait_s=float(s.get("wait_seconds") or 0),
            tokens_in=int(s.get("tokens_in") or 0),
            tokens_out=int(s.get("tokens_out") or 0),
            cost=Decimal(str(s.get("cost_usd") or 0)),
        )
        for s in run.get("steps", [])
    ]


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


def _tokens(tokens_in: int, tokens_out: int) -> str:
    return f"{tokens_in:,} in / {tokens_out:,} out" if tokens_in or tokens_out else NONE


def _cost(cost: Decimal) -> str:
    return f"${cost:,.2f}" if cost else NONE


def _total(steps: list[StepCost]) -> StepCost:
    durations = [s.duration_s for s in steps if s.duration_s is not None]
    return StepCost(
        stage="**Total**",
        attempt=len(steps),
        route="",
        duration_s=sum(durations, 0.0),
        wait_s=sum(s.wait_s for s in steps),
        tokens_in=sum(s.tokens_in for s in steps),
        tokens_out=sum(s.tokens_out for s in steps),
        cost=sum((s.cost for s in steps), Decimal(0)),
    )


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _steps_table(steps: list[StepCost]) -> list[str]:
    lines = [
        _row(["Stage", "Attempt", "Harness / model", "Duration", "Wait", "Tokens", "Cost"]),
        _row(["---", "---:", "---", "---:", "---:", "---:", "---:"]),
    ]
    for s in [*steps, _total(steps)]:
        attempt = str(s.attempt) if s.stage != "**Total**" else f"{s.attempt} steps"
        timing = [format_duration(s.duration_s), format_duration(s.wait_s) if s.wait_s else NONE]
        lines.append(_row([s.stage, attempt, s.route, *timing, _tokens(s.tokens_in, s.tokens_out), _cost(s.cost)]))
    return lines


def _routes_table(steps: list[StepCost]) -> list[str]:
    by_route: dict[str, list[StepCost]] = defaultdict(list)
    for s in steps:
        if s.route != NONE:
            by_route[s.route].append(s)
    if not by_route:
        return []
    lines = [
        _row(["Harness / model", "Steps", "Duration", "Tokens", "Cost"]),
        _row(["---", "---:", "---:", "---:", "---:"]),
    ]
    for route, group in by_route.items():
        t = _total(group)
        cells = [route, str(len(group)), format_duration(t.duration_s), _tokens(t.tokens_in, t.tokens_out)]
        lines.append(_row([*cells, _cost(t.cost)]))
    return ["", "**Per harness and model**", "", *lines]


def render_cost_comment(run: dict[str, Any]) -> str:
    """Markdown for the PR comment, starting with `COST_MARKER` so fact finds and updates it later."""
    steps = step_costs(run)
    total = _total(steps)
    headline = f"**fact run cost:** ${total.cost:,.2f} over {format_duration(total.duration_s)} ({len(steps)} steps)"
    body = [COST_MARKER, "### Cost", "", headline, "", *_steps_table(steps), *_routes_table(steps)]
    body += ["", f"<sub>Run `{run.get('id', '?')}`. Durations are wall time; wait is time spent on a person.</sub>"]
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
