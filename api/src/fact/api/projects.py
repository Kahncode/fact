"""Sync the project registry from `fact.yaml` (and each project's `.fact/project.yaml`) into the DB."""

from fact.core.logging import get_logger
from fact.core.models import Project, Repository
from fact.core.project_config import RepositoryConfig, load_project_config
from fact.core.settings import ProjectRef, Settings, project_path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = get_logger(__name__)


def _repositories(ref: ProjectRef) -> list[RepositoryConfig]:
    path = project_path(ref)
    return load_project_config(path).repositories if path else []


async def _upsert_project(session: AsyncSession, ref: ProjectRef) -> Project:
    project = await session.scalar(select(Project).where(Project.name == ref.name))
    if project is None:
        project = Project(name=ref.name, home_repo=ref.home)
        session.add(project)
        await session.flush()
    else:
        project.home_repo = ref.home
    return project


async def _upsert_repositories(session: AsyncSession, project: Project, repos: list[RepositoryConfig]) -> None:
    rows = await session.scalars(select(Repository).where(Repository.project_id == project.id))
    existing = {r.full_name: r for r in rows}
    for cfg in repos:
        row = existing.get(cfg.full_name)
        if row is None:
            row = Repository(project_id=project.id, full_name=cfg.full_name)
            session.add(row)
        row.role = cfg.role
        row.default_branch = cfg.default_branch
        row.merge_order = cfg.merge_order


async def sync_projects(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> None:
    """
    Upsert every registry project and its repositories. Idempotent; never deletes rows.

    Fails fast: a missing or invalid `.fact/project.yaml` raises `ConfigError` and the API does not start.
    """
    # Load configs first so an invalid file fails before anything is written.
    loaded = [(ref, _repositories(ref)) for ref in settings.projects]
    async with sessions() as session, session.begin():
        for ref, repos in loaded:
            project = await _upsert_project(session, ref)
            await _upsert_repositories(session, project, repos)
    log.info("projects_synced", count=len(loaded))
