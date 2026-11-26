"""Notifications: one per `dedupe_key`, listed newest first, read once."""

from http import HTTPStatus
from typing import Any

from httpx import AsyncClient

RUN = {"workflow_id": "wf-1", "project": "fact", "task_key": "task:M2-03", "workflow_type": "standard"}


async def _run_id(client: AsyncClient) -> str:
    return (await client.post("/api/v1/runs", json=RUN)).json()["id"]


def _body(run_id: str | None, key: str, **extra: object) -> dict[str, Any]:
    return {"run_id": run_id, "kind": "gate_waiting", "title": f"waiting: {key}", "dedupe_key": key, **extra}


async def test_same_dedupe_key_is_one_notification(client: AsyncClient) -> None:
    run_id = await _run_id(client)
    first = await client.post("/api/v1/notifications", json=_body(run_id, f"{run_id}:gate:g-1", body="checks red"))
    assert first.status_code == HTTPStatus.CREATED, first.text
    again = await client.post("/api/v1/notifications", json=_body(run_id, f"{run_id}:gate:g-1", body="changed"))
    assert again.status_code == HTTPStatus.OK
    repeat = again.json()  # changes nothing
    assert (repeat["id"], repeat["body"]) == (first.json()["id"], "checks red")

    listed = (await client.get("/api/v1/notifications")).json()
    assert [(n["id"], n["body"], n["read_at"]) for n in listed] == [(first.json()["id"], "checks red", None)]


async def test_unread_filter_newest_first_and_mark_read(client: AsyncClient) -> None:
    run_id = await _run_id(client)
    ids = []
    for key in ("a", "b", "c"):
        response = await client.post("/api/v1/notifications", json=_body(run_id, key, link="http://ui/run"))
        ids.append(response.json()["id"])

    read = await client.post(f"/api/v1/notifications/{ids[1]}/read")
    assert read.status_code == HTTPStatus.OK
    read_at = read.json()["read_at"]
    assert read_at is not None
    again = (await client.post(f"/api/v1/notifications/{ids[1]}/read")).json()["read_at"]
    assert again.removesuffix("Z") == read_at.removesuffix("Z")  # kept; SQLite drops the time zone on reload

    unread = (await client.get("/api/v1/notifications", params={"unread": "true"})).json()
    assert [n["id"] for n in unread] == [ids[2], ids[0]]
    everything = (await client.get("/api/v1/notifications")).json()
    assert [n["id"] for n in everything] == [ids[2], ids[1], ids[0]]
    newest = (await client.get("/api/v1/notifications", params={"limit": 1})).json()
    assert [n["id"] for n in newest] == [ids[2]]


async def test_notification_errors(client: AsyncClient) -> None:
    assert (await client.post("/api/v1/notifications/999/read")).status_code == HTTPStatus.NOT_FOUND
    unknown_run = await client.post("/api/v1/notifications", json=_body("nope", "k"))
    assert unknown_run.status_code == HTTPStatus.NOT_FOUND
    bad_kind = await client.post("/api/v1/notifications", json=_body(None, "k", kind="party"))
    assert bad_kind.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    no_run = await client.post("/api/v1/notifications", json=_body(None, "planning:1"))
    assert no_run.status_code == HTTPStatus.CREATED  # a notification need not belong to a run
