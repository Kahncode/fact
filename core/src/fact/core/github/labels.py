"""
The labels fact uses on a task repository, and a sync that creates or updates them. It never deletes a label.

GitHub label names are case-insensitive; a label that differs only in case is renamed to fact's spelling.
"""

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

import httpx
from fact.core.github.app import split_full_name


@dataclass(frozen=True)
class Label:
    name: str
    color: str  # six hex digits, no '#'
    description: str


AUTO_MERGE_LABEL = "fact:auto-merge"  # on a PR classify routed `auto_merge`
NEEDS_REVIEW_LABEL = "fact:needs-review"  # on a PR classify routed `human_review`

FACT_LABELS: tuple[Label, ...] = (
    Label("fact:ready", "0E8A16", "The factory may pick this issue up"),
    Label("fact:planned", "C5DEF5", "Planned; becomes fact:ready when the parent plan is approved"),
    Label("fact:running", "1D76DB", "A fact run is in progress"),
    Label("fact:needs-human", "D93F0B", "A fact run is waiting on you"),
    Label(AUTO_MERGE_LABEL, "0E8A16", "fact classified this PR safe to merge without a review"),
    Label(NEEDS_REVIEW_LABEL, "D93F0B", "fact classified this PR as needing a person's review"),
    Label("type:incident", "B60205", "Incident: something is broken in production"),
    Label("type:bug", "D73A4A", "Something is not working"),
    Label("type:feature", "A2EEEF", "New behaviour"),
    Label("type:chore", "FEF2C0", "Maintenance, tooling or docs"),
    Label("complexity:trivial", "C2E0C6", "A small, mechanical change"),
    Label("complexity:standard", "FBCA04", "A typical change"),
    Label("complexity:complex", "5319E7", "A large or risky change"),
    Label("priority:urgent", "B60205", "Starts before every other ready task of its project"),
    Label("priority:high", "D93F0B", "Starts before medium and low tasks of its project"),
    Label("priority:medium", "FBCA04", "The default priority"),
    Label("priority:low", "C2E0C6", "Starts after the other ready tasks of its project"),
)


class LabelAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class LabelChange:
    name: str
    action: LabelAction


async def _existing_labels(client: httpx.AsyncClient, owner: str, repo: str) -> dict[str, dict]:
    """Every label on the repository, keyed by lower-cased name, following pagination."""
    labels: dict[str, dict] = {}
    url: str | None = f"/repos/{owner}/{repo}/labels?per_page=100"
    while url:
        response = await client.get(url)
        labels.update({item["name"].lower(): item for item in response.json()})
        url = response.links.get("next", {}).get("url")
    return labels


def _differs(label: Label, existing: dict) -> bool:
    return (
        existing["name"] != label.name
        or existing["color"].upper() != label.color.upper()
        or (existing.get("description") or "") != label.description
    )


async def sync_labels(
    client: httpx.AsyncClient, full_name: str, labels: tuple[Label, ...] = FACT_LABELS
) -> list[LabelChange]:
    """Create the missing `labels` on `owner/repo` and fix colour, description and case of existing ones."""
    owner, repo = split_full_name(full_name)
    existing = await _existing_labels(client, owner, repo)
    changes: list[LabelChange] = []
    for label in labels:
        current = existing.get(label.name.lower())
        body = {"color": label.color, "description": label.description}
        if current is None:
            await client.post(f"/repos/{owner}/{repo}/labels", json={"name": label.name, **body})
            changes.append(LabelChange(label.name, LabelAction.CREATED))
        elif _differs(label, current):
            path = f"/repos/{owner}/{repo}/labels/{quote(current['name'], safe='')}"
            await client.patch(path, json={"new_name": label.name, **body})
            changes.append(LabelChange(label.name, LabelAction.UPDATED))
        else:
            changes.append(LabelChange(label.name, LabelAction.UNCHANGED))
    return changes
