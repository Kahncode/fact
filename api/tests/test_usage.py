"""Usage windows: a run's report upserts by account and window, and the list returns them."""

from http import HTTPStatus

from httpx import AsyncClient

FIVE = {"name": "five_hour", "utilization": 1.0, "resets_at": "2026-11-26T15:00:00+00:00", "rejected": True}
WEEK = {"name": "seven_day", "utilization": 0.4, "resets_at": "2026-11-30T00:00:00+00:00"}


def _view(rows: list[dict]) -> list[tuple]:
    return [(r["account"], r["window"], r["utilization"], r["resets_at"][:19]) for r in rows]


async def test_report_then_list(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/usage")).json() == []
    put = await client.put("/api/v1/usage/claude", json={"windows": [FIVE, WEEK]})
    assert put.status_code == HTTPStatus.OK, put.text
    expected = [
        ("claude", "five_hour", 1.0, "2026-11-26T15:00:00"),
        ("claude", "seven_day", 0.4, "2026-11-30T00:00:00"),
    ]
    assert _view(put.json()) == expected
    listed = (await client.get("/api/v1/usage")).json()
    assert _view(listed) == expected
    assert all(r["updated_at"].endswith(("+00:00", "Z")) for r in listed)


async def test_newer_report_wins_and_unmentioned_windows_stay(client: AsyncClient) -> None:
    await client.put("/api/v1/usage/claude", json={"windows": [FIVE, WEEK]})
    reset = {**FIVE, "utilization": 0.05, "resets_at": "2026-11-26T20:00:00Z"}
    await client.put("/api/v1/usage/claude", json={"windows": [reset]})
    await client.put("/api/v1/usage/other", json={"windows": [WEEK]})
    assert _view((await client.get("/api/v1/usage")).json()) == [
        ("claude", "five_hour", 0.05, "2026-11-26T20:00:00"),
        ("claude", "seven_day", 0.4, "2026-11-30T00:00:00"),
        ("other", "seven_day", 0.4, "2026-11-30T00:00:00"),
    ]


async def test_bad_reports_are_refused(client: AsyncClient) -> None:
    bad_account = await client.put("/api/v1/usage/Not%20OK", json={"windows": [WEEK]})
    assert bad_account.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    negative = await client.put("/api/v1/usage/claude", json={"windows": [{**WEEK, "utilization": -1}]})
    assert negative.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
