import json
from urllib.parse import unquote

import httpx
import pytest
from fact.core.github import FACT_LABELS, Label, LabelAction, github_client, sync_labels

REPO = "/repos/acme/app/labels"


class FakeLabels:
    """In-memory label store behind the GitHub labels API, paginated `page_size` per page."""

    def __init__(self, labels: list[dict], page_size: int = 100) -> None:
        self.labels = labels
        self.page_size = page_size
        self.writes: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        if request.method == "GET" and path == REPO:
            return self._page(int(request.url.params.get("page", "1")))
        body = json.loads(request.content)
        self.writes.append((request.method, path, body))
        if request.method == "POST" and path == REPO:
            self.labels.append({"name": body["name"], "color": body["color"], "description": body["description"]})
            return httpx.Response(201, json=self.labels[-1])
        if request.method == "PATCH" and path.startswith(REPO + "/"):
            current = next(lb for lb in self.labels if lb["name"] == path.removeprefix(REPO + "/"))
            current.update(name=body["new_name"], color=body["color"], description=body["description"])
            return httpx.Response(200, json=current)
        return httpx.Response(404, json={"message": "Not Found"})

    def _page(self, page: int) -> httpx.Response:
        start = (page - 1) * self.page_size
        headers = {}
        if start + self.page_size < len(self.labels):
            headers["Link"] = f'<https://gh.test{REPO}?per_page=100&page={page + 1}>; rel="next"'
        return httpx.Response(200, json=self.labels[start : start + self.page_size], headers=headers)


async def _sync(fake: FakeLabels, labels: tuple[Label, ...] = FACT_LABELS) -> dict[str, LabelAction]:
    async with github_client("tok", api_url="https://gh.test", transport=httpx.MockTransport(fake)) as client:
        changes = await sync_labels(client, "acme/app", labels)
    return {c.name: c.action for c in changes}


async def test_creates_every_label_on_an_empty_repo() -> None:
    fake = FakeLabels([])

    actions = await _sync(fake)

    assert set(actions.values()) == {LabelAction.CREATED}
    assert len(actions) == len(FACT_LABELS)
    assert {f"priority:{p}" for p in ("urgent", "high", "medium", "low")} <= actions.keys()
    assert {(lb["name"], lb["color"], lb["description"]) for lb in fake.labels} == {
        (lb.name, lb.color, lb.description) for lb in FACT_LABELS
    }


async def test_updates_colour_description_and_case_and_keeps_other_labels() -> None:
    labels = (Label("fact:ready", "0E8A16", "Go"), Label("type:bug", "D73A4A", "Broken"))
    fake = FakeLabels(
        [
            {"name": "fact:ready", "color": "ffffff", "description": "old"},
            {"name": "Type:Bug", "color": "d73a4a", "description": "Broken"},
            {"name": "wontfix", "color": "000000", "description": None},
        ]
    )

    actions = await _sync(fake, labels)

    assert actions == {"fact:ready": LabelAction.UPDATED, "type:bug": LabelAction.UPDATED}
    assert fake.writes == [
        ("PATCH", f"{REPO}/fact:ready", {"new_name": "fact:ready", "color": "0E8A16", "description": "Go"}),
        ("PATCH", f"{REPO}/Type:Bug", {"new_name": "type:bug", "color": "D73A4A", "description": "Broken"}),
    ]
    assert {lb["name"] for lb in fake.labels} == {"fact:ready", "type:bug", "wontfix"}


async def test_second_run_changes_nothing() -> None:
    fake = FakeLabels([{"name": "bug", "color": "d73a4a", "description": None}])
    await _sync(fake)
    fake.writes.clear()

    actions = await _sync(fake)

    assert set(actions.values()) == {LabelAction.UNCHANGED}
    assert fake.writes == []


async def test_matches_colours_case_insensitively_and_missing_description() -> None:
    fake = FakeLabels([{"name": "x:y", "color": "abcdef", "description": None}])

    actions = await _sync(fake, (Label("x:y", "ABCDEF", ""),))

    assert actions == {"x:y": LabelAction.UNCHANGED}
    assert fake.writes == []


async def test_follows_pagination() -> None:
    existing = [{"name": f"other-{i}", "color": "000000", "description": ""} for i in range(5)]
    existing.append({"name": "fact:ready", "color": "0E8A16", "description": "Go"})
    fake = FakeLabels(existing, page_size=2)

    actions = await _sync(fake, (Label("fact:ready", "0E8A16", "Go"),))

    assert actions == {"fact:ready": LabelAction.UNCHANGED}
    assert fake.writes == []


async def test_rejects_a_bad_repository_name() -> None:
    async with github_client("tok", api_url="https://gh.test", transport=httpx.MockTransport(FakeLabels([]))) as c:
        with pytest.raises(ValueError, match="owner/repo"):
            await sync_labels(c, "not-a-repo")
