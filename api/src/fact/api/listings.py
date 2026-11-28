"""
The scheduler's kept listings: each project's ready tasks as its task source last listed them, in the API process.

A tick lists a project's task source again only once its listing is older than `concurrency.listing_max_age_seconds`,
or after one of its starts was refused (`drop`). An old listing is safe: every start reads its task again and refuses
one closed or taken since (`fact.api.routers.run_start`), and run history is read from the database at every tick.
Losing the listings on an API restart costs one more read.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fact.core.scheduler import ReadyTask


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Listing:
    tasks: list[ReadyTask]
    max_concurrent_runs: int  # the project's runner pool cap, read with the listing
    listed_at: datetime


@dataclass
class Listings:
    max_age: timedelta
    clock: Callable[[], datetime] = _now
    kept: dict[str, Listing] = field(default_factory=dict)

    def get(self, project: str) -> Listing | None:
        """Return the project's last listing, however old."""
        return self.kept.get(project)

    def fresh(self, project: str) -> Listing | None:
        """Return the project's last listing if it is not older than `max_age`."""
        listing = self.kept.get(project)
        return listing if listing and self.clock() - listing.listed_at <= self.max_age else None

    def keep(self, project: str, tasks: list[ReadyTask], max_concurrent_runs: int) -> Listing:
        listing = Listing(tasks, max_concurrent_runs, self.clock())
        self.kept[project] = listing
        return listing

    def drop(self, project: str) -> None:
        """Forget the project's listing (a start from it was refused): the next tick lists it again."""
        self.kept.pop(project, None)
