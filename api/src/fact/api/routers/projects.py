from typing import Annotated

from fact.api.deps import get_session
from fact.api.schemas import ProjectOut, RepositoryOut
from fact.core.models import Project, Repository
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1")


@router.get("/projects")
async def list_projects(session: Annotated[AsyncSession, Depends(get_session)]) -> list[ProjectOut]:
    projects = (await session.scalars(select(Project).order_by(Project.name))).all()
    repos = (await session.scalars(select(Repository).order_by(Repository.merge_order, Repository.full_name))).all()
    return [
        ProjectOut(
            id=p.id,
            name=p.name,
            home_repo=p.home_repo,
            repositories=[RepositoryOut.model_validate(r) for r in repos if r.project_id == p.id],
        )
        for p in projects
    ]
