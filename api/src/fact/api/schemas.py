"""Response models of the API."""

from pydantic import BaseModel, ConfigDict


class RepositoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    full_name: str
    role: str | None
    default_branch: str
    merge_order: int


class ProjectOut(BaseModel):
    id: int
    name: str
    home_repo: str
    repositories: list[RepositoryOut]
