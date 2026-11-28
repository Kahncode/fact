"""
fact as a GitHub App: App JWTs, per-repository installation tokens, and the bot identity used for commits.

Token values are never logged and never appear in `repr()`.
"""

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
from fact.core.github.client import GitHubError, github_client
from fact.core.logging import get_logger
from fact.core.settings import Settings, config_file, get_settings

JWT_BACKDATE = timedelta(seconds=60)  # tolerate clock drift with GitHub
JWT_LIFETIME = timedelta(minutes=9)  # GitHub caps App JWTs at 10 minutes
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)

_FULL_NAME = re.compile(r"^[A-Za-z0-9-]+/(?!\.\.?$)[A-Za-z0-9._-]+$")

log = get_logger(__name__)


class GitHubAppConfigError(GitHubError):
    """The App ID or private key is missing or unreadable."""


@dataclass(frozen=True)
class Installation:
    id: int
    account: str


@dataclass(frozen=True)
class InstallationToken:
    token: str = field(repr=False)
    expires_at: datetime
    installation: Installation


@dataclass(frozen=True)
class AppInfo:
    id: int
    slug: str
    name: str


@dataclass(frozen=True)
class BotIdentity:
    """Commit author for fact[bot]: `name` is `<slug>[bot]`, `email` its GitHub noreply address."""

    slug: str
    name: str
    email: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def split_full_name(full_name: str) -> tuple[str, str]:
    """Validate `owner/repo` and split it; `ValueError` if it is not one."""
    if not _FULL_NAME.match(full_name):
        msg = f"repository must be 'owner/repo', got {full_name!r}"
        raise ValueError(msg)
    owner, _, repo = full_name.partition("/")
    return owner, repo


class GitHubApp:
    """
    Authenticates as the fact GitHub App.

    `transport` and `clock` exist for tests. Installation tokens are scoped to one repository and cached per
    repository until `TOKEN_REFRESH_MARGIN` before they expire. Caches live on the instance: keep one `GitHubApp`
    per process rather than calling `from_settings()` per use.
    """

    def __init__(
        self,
        app_id: int | str,
        private_key: str,
        *,
        api_url: str = "https://api.github.com",
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.app_id = str(app_id)
        self.api_url = api_url
        self._private_key = private_key
        self._transport = transport
        self._clock = clock
        self._tokens: dict[str, InstallationToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._app_info: AppInfo | None = None
        self._bot: BotIdentity | None = None

    def __repr__(self) -> str:
        """Show the App ID and API URL, never the key."""
        return f"GitHubApp(app_id={self.app_id!r}, api_url={self.api_url!r})"

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> GitHubApp:
        """
        Build from `github_app_id` and `github_app_private_key_path`; raises `GitHubAppConfigError` if unset.

        A relative key path is resolved from the folder of `config_file()` (the default fact.yaml), even when
        `settings` was loaded from another file.
        """
        s = settings or get_settings()
        if s.github_app_id is None or s.github_app_private_key_path is None:
            msg = "GitHub App not configured: set FACT_GITHUB_APP_ID and FACT_GITHUB_APP_PRIVATE_KEY_PATH"
            raise GitHubAppConfigError(msg)
        key_path = Path(s.github_app_private_key_path).expanduser()
        if not key_path.is_absolute():
            key_path = config_file().parent / key_path  # relative to fact.yaml, like .env
        try:
            private_key = key_path.read_text(encoding="utf-8")
        except OSError as e:
            msg = f"cannot read the GitHub App private key at {key_path}: {e.strerror}"
            raise GitHubAppConfigError(msg) from e
        return cls(s.github_app_id, private_key, api_url=s.github_api_url, transport=transport)

    def app_jwt(self) -> str:
        """Sign a fresh RS256 App JWT: issued 60 s in the past, valid 9 minutes."""
        now = self._clock()
        payload = {
            "iat": int((now - JWT_BACKDATE).timestamp()),
            "exp": int((now + JWT_LIFETIME).timestamp()),
            "iss": self.app_id,
        }
        try:
            return jwt.encode(payload, self._private_key, algorithm="RS256")
        except (ValueError, TypeError, jwt.PyJWTError) as e:
            msg = "the GitHub App private key is not a valid RSA PEM key"
            raise GitHubAppConfigError(msg) from e

    def api_client(self, token: str | None) -> httpx.AsyncClient:
        """Return a `github_client` on this App's API URL (and test transport), authenticated with `token`."""
        return github_client(token, api_url=self.api_url, transport=self._transport)

    async def app_info(self) -> AppInfo:
        if self._app_info is None:
            async with self.api_client(self.app_jwt()) as client:
                body = (await client.get("/app")).json()
            self._app_info = AppInfo(id=body["id"], slug=body["slug"], name=body["name"])
        return self._app_info

    async def installation(self, full_name: str) -> Installation:
        """Find the App's installation covering `owner/repo`; `GitHubNotFoundError` if the App is not installed there."""
        owner, repo = split_full_name(full_name)
        async with self.api_client(self.app_jwt()) as client:
            body = (await client.get(f"/repos/{owner}/{repo}/installation")).json()
        return Installation(id=body["id"], account=body["account"]["login"])

    def _cached_token(self, key: str) -> InstallationToken | None:
        cached = self._tokens.get(key)
        if cached and cached.expires_at - TOKEN_REFRESH_MARGIN > self._clock():
            return cached
        return None

    async def installation_token(self, full_name: str) -> InstallationToken:
        """
        Mint an installation access token for `owner/repo`, reused until 5 minutes before it expires.

        The token can reach only that repository. Concurrent calls for one repository mint once.
        """
        return await self.repositories_token([full_name])

    async def repositories_token(
        self, full_names: Sequence[str], permissions: Mapping[str, str] | None = None
    ) -> InstallationToken:
        """
        Mint one token reaching only `full_names` (one installation's), with only `permissions` when given.

        Without `permissions` it has every permission of the installation. Reused until 5 minutes before it expires;
        concurrent calls for the same repositories and permissions mint once. `GitHubError` when the repositories
        belong to different installations.
        """
        names = sorted({name.lower(): name for name in full_names}.values(), key=str.lower)
        if not names:
            msg = "a token needs at least one repository"
            raise ValueError(msg)
        perms = dict(sorted((permissions or {}).items()))
        key = ",".join(names).lower() + ("|" + ",".join(f"{k}:{v}" for k, v in perms.items()) if perms else "")
        if cached := self._cached_token(key):
            return cached
        async with self._locks.setdefault(key, asyncio.Lock()):
            if cached := self._cached_token(key):
                return cached
            installation = await self._shared_installation(names)
            body: dict[str, object] = {"repositories": [split_full_name(name)[1] for name in names]}
            if perms:
                body["permissions"] = perms
            async with self.api_client(self.app_jwt()) as client:
                answer = (await client.post(f"/app/installations/{installation.id}/access_tokens", json=body)).json()
            token = InstallationToken(answer["token"], _parse_time(answer["expires_at"]), installation)
            self._tokens[key] = token
        log.info(
            "github installation token minted",
            repos=names,
            permissions=perms or None,
            installation_id=installation.id,
            expires_at=token.expires_at.isoformat(),
        )
        return token

    async def _shared_installation(self, full_names: Sequence[str]) -> Installation:
        installations = [await self.installation(name) for name in full_names]
        if any(i.id != installations[0].id for i in installations):
            msg = f"{', '.join(full_names)} are not all in one installation of the App; one token cannot reach them"
            raise GitHubError(msg)
        return installations[0]

    async def bot_identity(self) -> BotIdentity:
        """
        Look up the App's bot user, for commit authorship. The user lookup is public, so it runs unauthenticated.

        The noreply domain is github.com's; GitHub Enterprise Server is not supported yet.
        """
        if self._bot is None:
            info = await self.app_info()
            name = f"{info.slug}[bot]"
            async with self.api_client(None) as client:
                user = (await client.get(f"/users/{name}")).json()
            self._bot = BotIdentity(slug=info.slug, name=name, email=f"{user['id']}+{name}@users.noreply.github.com")
        return self._bot


_SHARED: dict[tuple[str | None, str | None, str], GitHubApp] = {}


def shared_app(settings: Settings | None = None) -> GitHubApp:
    """
    Return the process's `GitHubApp` for these settings, built on first use so every caller shares its token caches.

    Raises `GitHubAppConfigError` while the App is not configured (and tries again on the next call).
    """
    s = settings or get_settings()
    key = (
        None if s.github_app_id is None else str(s.github_app_id),
        None if s.github_app_private_key_path is None else str(s.github_app_private_key_path),
        s.github_api_url,
    )
    if key not in _SHARED:
        _SHARED[key] = GitHubApp.from_settings(s)
    return _SHARED[key]
