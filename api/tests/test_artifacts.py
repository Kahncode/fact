from collections.abc import AsyncIterator
from http import HTTPStatus

import pytest
from httpx import AsyncClient

CHUNKS = 5
CHUNK_SIZE = 100_000
KEY = "runs/r1/implement/1/output.log"


async def test_upload_download(client: AsyncClient) -> None:
    put = await client.put(f"/api/v1/artifacts/{KEY}", content=b"line1\nline2\n")
    assert put.status_code == HTTPStatus.NO_CONTENT
    got = await client.get(f"/api/v1/artifacts/{KEY}")
    assert got.status_code == HTTPStatus.OK
    assert got.content == b"line1\nline2\n"
    assert got.headers["content-type"].startswith("text/plain")


async def test_html_is_download_not_rendered(client: AsyncClient) -> None:
    await client.put("/api/v1/artifacts/runs/r/s/1/x.html", content=b"<script>1</script>")
    got = await client.get("/api/v1/artifacts/runs/r/s/1/x.html")
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["content-disposition"].startswith("attachment")
    assert got.headers["x-content-type-options"] == "nosniff"


async def test_multi_chunk_upload(client: AsyncClient) -> None:
    async def body() -> AsyncIterator[bytes]:
        for _ in range(CHUNKS):
            yield b"x" * CHUNK_SIZE

    await client.put("/api/v1/artifacts/runs/r/s/1/big.log", content=body())
    assert len((await client.get("/api/v1/artifacts/runs/r/s/1/big.log")).content) == CHUNKS * CHUNK_SIZE


async def test_jsonl_is_text_and_other_is_not(client: AsyncClient) -> None:
    await client.put("/api/v1/artifacts/runs/r/s/1/transcript.jsonl", content=b"{}\n")
    await client.put("/api/v1/artifacts/runs/r/s/1/diff.patch", content=b"x")
    assert (
        (await client.get("/api/v1/artifacts/runs/r/s/1/transcript.jsonl"))
        .headers["content-type"]
        .startswith("text/plain")
    )
    assert (
        not (await client.get("/api/v1/artifacts/runs/r/s/1/diff.patch"))
        .headers["content-type"]
        .startswith("text/plain")
    )


async def test_missing_is_404(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/artifacts/runs/nope/x.log")).status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize(
    "key", ["%2e%2e/x", "a/%2e%2e/%2e%2e/x", "..%2Fx", "C:%5Cx", "C:/x", "%2Fetc/passwd", "a%5C..%5Cx"]
)
async def test_escape_refused(client: AsyncClient, key: str) -> None:
    assert (await client.put(f"/api/v1/artifacts/{key}", content=b"x")).status_code == HTTPStatus.BAD_REQUEST
    assert (await client.get(f"/api/v1/artifacts/{key}")).status_code == HTTPStatus.BAD_REQUEST
