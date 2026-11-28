import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import pytest
import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fact.core.github import (
    BotIdentity,
    GitHubApp,
    GitHubAppConfigError,
    GitHubNotFoundError,
)
from fact.core.logging import configure_logging
from fact.core.settings import Settings

APP_ID = 4242
INSTALLATION_ID = 777
BOT_USER_ID = 9001
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def make_rsa_key() -> tuple[str, rsa.RSAPublicKey]:
    """Throwaway RSA key: the private PEM and its public key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return pem, key.public_key()


@dataclass
class FakeGitHub:
    """Answers the endpoints `GitHubApp` uses. Each minted token is distinct: `ghs_secret_<n>`."""

    token_ttl: timedelta = timedelta(hours=1)
    installed: set[str] = field(default_factory=lambda: {"kahncode/fact"})
    requests: list[httpx.Request] = field(default_factory=list)
    minted: list[str] = field(default_factory=list)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        await asyncio.sleep(0)  # yield like real I/O, so concurrent callers interleave
        path = request.url.path
        if path == "/app":
            return httpx.Response(200, json={"id": APP_ID, "slug": "fact-factory", "name": "fact factory"})
        if path.startswith("/repos/") and path.endswith("/installation"):
            repo = path.removeprefix("/repos/").removesuffix("/installation").lower()
            if repo not in self.installed:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"id": INSTALLATION_ID, "account": {"login": "Kahncode"}})
        if path == f"/app/installations/{INSTALLATION_ID}/access_tokens" and request.method == "POST":
            token = f"ghs_secret_{len(self.minted)}"
            self.minted.append(token)
            expires = (NOW + self.token_ttl).isoformat().replace("+00:00", "Z")
            return httpx.Response(201, json={"token": token, "expires_at": expires})
        if path == "/users/fact-factory[bot]":
            return httpx.Response(200, json={"id": BOT_USER_ID, "login": "fact-factory[bot]"})
        return httpx.Response(404, json={"message": "Not Found"})

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(scope="module")
def rsa_key() -> tuple[str, rsa.RSAPublicKey]:
    return make_rsa_key()


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def gh(rsa_key: tuple[str, rsa.RSAPublicKey], fake: FakeGitHub, clock: Clock) -> GitHubApp:
    return GitHubApp(APP_ID, rsa_key[0], api_url="https://gh.test", transport=fake.transport, clock=clock)


def test_app_jwt_claims(gh: GitHubApp, rsa_key: tuple[str, rsa.RSAPublicKey]) -> None:
    token = gh.app_jwt()

    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(token, rsa_key[1], algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == str(APP_ID)
    assert claims["iat"] == int(NOW.timestamp()) - 60
    assert claims["exp"] == int(NOW.timestamp()) + 9 * 60


async def test_installation_token_uses_jwt_and_github_headers(
    gh: GitHubApp, fake: FakeGitHub, rsa_key: tuple[str, rsa.RSAPublicKey]
) -> None:
    token = await gh.installation_token("Kahncode/fact")

    assert token.token == "ghs_secret_0"  # noqa: S105 fake
    assert token.expires_at == NOW + timedelta(hours=1)
    assert token.installation.id == INSTALLATION_ID
    assert token.installation.account == "Kahncode"
    assert json.loads(fake.requests[-1].content) == {"repositories": ["fact"]}
    for request in fake.requests:
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        scheme, _, bearer = request.headers["Authorization"].partition(" ")
        assert scheme == "Bearer"
        assert jwt.decode(bearer, rsa_key[1], algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})


async def test_installation_token_cached_until_refresh_margin(gh: GitHubApp, fake: FakeGitHub, clock: Clock) -> None:
    first = await gh.installation_token("Kahncode/fact")
    clock.now = NOW + timedelta(minutes=54)  # 6 minutes left: still cached
    assert await gh.installation_token("kahncode/FACT") is first
    clock.now = NOW + timedelta(minutes=55, seconds=1)  # under 5 minutes left: refreshed
    second = await gh.installation_token("Kahncode/fact")

    assert second.token == "ghs_secret_1"  # noqa: S105 fake
    assert fake.minted == ["ghs_secret_0", "ghs_secret_1"]


async def test_concurrent_calls_mint_once(gh: GitHubApp, fake: FakeGitHub) -> None:
    tokens = await asyncio.gather(*(gh.installation_token("Kahncode/fact") for _ in range(5)))

    assert fake.minted == ["ghs_secret_0"]
    assert {t.token for t in tokens} == {"ghs_secret_0"}


async def test_repositories_token_is_scoped_and_cached_apart(gh: GitHubApp, fake: FakeGitHub) -> None:
    fake.installed.add("kahncode/site")
    full = await gh.installation_token("Kahncode/fact")
    read = await gh.repositories_token(["Kahncode/site", "Kahncode/fact"], {"contents": "read"})

    assert read.token != full.token
    assert json.loads(fake.requests[-1].content) == {
        "repositories": ["fact", "site"],
        "permissions": {"contents": "read"},
    }
    assert await gh.repositories_token(["kahncode/fact", "Kahncode/site"], {"contents": "read"}) is read


async def test_installation_token_not_installed(gh: GitHubApp) -> None:
    with pytest.raises(GitHubNotFoundError, match="installed") as exc:
        await gh.installation_token("someone/else")
    assert exc.value.status_code == httpx.codes.NOT_FOUND


@pytest.mark.parametrize("name", ["fact", "a/b/c", "../x", "owner/", "owner/..", "owner/."])
async def test_installation_token_rejects_bad_names(gh: GitHubApp, fake: FakeGitHub, name: str) -> None:
    with pytest.raises(ValueError, match="owner/repo"):
        await gh.installation_token(name)
    assert fake.requests == []


async def test_bot_identity(gh: GitHubApp, fake: FakeGitHub) -> None:
    bot = await gh.bot_identity()

    assert bot == BotIdentity(
        slug="fact-factory",
        name="fact-factory[bot]",
        email=f"{BOT_USER_ID}+fact-factory[bot]@users.noreply.github.com",
    )
    user_request = fake.requests[-1]
    assert "Authorization" not in user_request.headers
    assert await gh.bot_identity() is bot
    assert len(fake.requests) == len(set(fake.paths()))


async def test_token_never_logged_or_in_repr(
    gh: GitHubApp, fake: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    root, httpx_log = logging.getLogger(), logging.getLogger("httpx")
    handlers, level, httpx_level = root.handlers[:], root.level, httpx_log.level
    try:
        configure_logging("prod", "DEBUG")
        httpx_log.setLevel(logging.DEBUG)
        token = await gh.installation_token("Kahncode/fact")
        await gh.bot_identity()
        out = capsys.readouterr().out
    finally:
        root.handlers[:], root.level = handlers, level
        httpx_log.setLevel(httpx_level)
        structlog.reset_defaults()

    records = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert any(r["event"] == "github installation token minted" for r in records)
    assert fake.minted
    for secret in fake.minted:
        assert secret not in out
    assert "ghs_secret" not in repr(token)
    assert "PRIVATE" not in repr(gh)


def test_from_settings(tmp_path: Path, rsa_key: tuple[str, rsa.RSAPublicKey]) -> None:
    key_file = tmp_path / "app.pem"
    key_file.write_text(rsa_key[0], encoding="utf-8")
    settings = Settings(
        github_app_id=str(APP_ID), github_app_private_key_path=key_file, github_api_url="https://ghe.test"
    )

    gh = GitHubApp.from_settings(settings)

    assert gh.app_id == str(APP_ID)
    assert gh.api_url == "https://ghe.test"
    assert jwt.decode(gh.app_jwt(), rsa_key[1], algorithms=["RS256"])["iss"] == str(APP_ID)


def test_from_settings_missing(tmp_path: Path) -> None:
    with pytest.raises(GitHubAppConfigError, match="FACT_GITHUB_APP_ID"):
        GitHubApp.from_settings(Settings(github_app_id=None))
    with pytest.raises(GitHubAppConfigError, match="cannot read"):
        GitHubApp.from_settings(Settings(github_app_id="1", github_app_private_key_path=tmp_path / "missing.pem"))


def test_bad_private_key() -> None:
    with pytest.raises(GitHubAppConfigError, match="RSA PEM"):
        GitHubApp(APP_ID, "not a key").app_jwt()
