from pathlib import Path
from typing import Annotated

from fact.api.deps import get_artifacts
from fact.core.artifacts import InvalidArtifactKeyError, LocalArtifactStore
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, Response

router = APIRouter(prefix="/api/v1")

Store = Annotated[LocalArtifactStore, Depends(get_artifacts)]
TEXT_SUFFIXES = (".log", ".jsonl")


def _path(store: LocalArtifactStore, key: str) -> Path:
    try:
        return store.path_for(key)
    except InvalidArtifactKeyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.put("/artifacts/{key:path}", status_code=status.HTTP_204_NO_CONTENT)
async def put_artifact(key: str, request: Request, store: Store) -> Response:
    _path(store, key)  # 400 on a bad key before reading the body
    await store.put_stream(key, request.stream())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/artifacts/{key:path}")
async def get_artifact(key: str, store: Store) -> FileResponse:
    path = _path(store, key)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")
    # Artifact content is untrusted agent output: only logs are shown as text, everything else is a download.
    text = key.endswith(TEXT_SUFFIXES)
    return FileResponse(
        path,
        filename=path.name,
        media_type="text/plain; charset=utf-8" if text else "application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
        content_disposition_type="inline" if text else "attachment",
    )
