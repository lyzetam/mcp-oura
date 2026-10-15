"""Signing in with OAuth: where the tokens live, and how they are renewed."""

import asyncio
import json
import stat
import time
from datetime import date

import httpx
import pytest

from fake_oura import CLIENT_ID, CLIENT_SECRET, TOKEN
from mcp_oura import auth
from mcp_oura.client import OuraAPIError, OuraClient

DAY = date(2026, 10, 12)


def saved(path, **overrides):
    creds = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "access_token": TOKEN,
        "refresh_token": "refresh-0",
        "expires_at": time.time() + 86400,
        **overrides,
    }
    auth.save_credentials(path, creds)
    return path


def test_credentials_are_readable_only_by_their_owner(tmp_path):
    path = saved(tmp_path / "mcp-oura" / "credentials.json")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text())["refresh_token"] == "refresh-0"


def test_the_default_location_follows_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.delenv("OURA_CREDENTIALS_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert auth.credentials_path() == tmp_path / "mcp-oura" / "credentials.json"
    monkeypatch.setenv("OURA_CREDENTIALS_FILE", str(tmp_path / "elsewhere.json"))
    assert auth.credentials_path() == tmp_path / "elsewhere.json"


def test_a_damaged_credentials_file_asks_to_sign_in_again(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text('{"access_token": "only-this"}')
    with pytest.raises(OuraAPIError, match="mcp-oura login"):
        auth.OAuthCredentials(path)


async def test_a_current_token_is_used_as_it_is(tmp_path, fake):
    creds = auth.OAuthCredentials(saved(tmp_path / "c.json"), transport=fake.transport())
    assert await creds.token() == TOKEN
    assert fake.token_requests == 0


async def test_a_token_about_to_expire_is_renewed_and_the_new_pair_saved(tmp_path, fake):
    fake.refresh_tokens.add("refresh-0")
    path = saved(tmp_path / "c.json", expires_at=time.time() + 60)
    creds = auth.OAuthCredentials(path, transport=fake.transport())

    assert await creds.token() == "access-1"
    on_disk = json.loads(path.read_text())
    assert on_disk["refresh_token"] == "refresh-1"
    assert on_disk["expires_at"] > time.time() + 80000
    assert on_disk["client_secret"] == CLIENT_SECRET
    assert "refresh-0" not in fake.refresh_tokens


async def test_a_rejected_token_is_renewed_once_and_the_call_retried(tmp_path, fake):
    fake.refresh_tokens.add("refresh-0")
    path = saved(tmp_path / "c.json", access_token="revoked")
    tokens = auth.OAuthCredentials(path, transport=fake.transport())
    client = OuraClient(tokens, timezone="UTC", transport=fake.transport(), retry_delay=0)

    assert await client.days("daily_sleep", DAY, DAY)
    assert fake.token_requests == 1
    await client.aclose()


async def test_a_token_rejected_even_after_renewal_asks_to_sign_in_again(tmp_path, fake):
    fake.refresh_tokens.add("refresh-0")
    path = saved(tmp_path / "c.json", access_token="revoked")
    tokens = auth.OAuthCredentials(path, transport=fake.transport())
    client = OuraClient(tokens, timezone="UTC", transport=fake.transport(), retry_delay=0)
    fake.app_revoked = True

    with pytest.raises(OuraAPIError, match="mcp-oura login"):
        await client.days("daily_sleep", DAY, DAY)
    await client.aclose()


async def test_two_servers_renewing_at_once_spend_the_refresh_token_once(tmp_path, fake):
    # Claude Code and Claude Desktop can each run a server on the same credentials file.
    fake.refresh_tokens.add("refresh-0")
    path = saved(tmp_path / "c.json", expires_at=0)
    first = auth.OAuthCredentials(path, transport=fake.slow_token_transport())
    second = auth.OAuthCredentials(path, transport=fake.slow_token_transport())

    assert await asyncio.gather(first.token(), second.token()) == ["access-1", "access-1"]
    assert fake.token_requests == 1


async def test_a_refused_renewal_asks_to_sign_in_again(tmp_path, fake):
    creds = auth.OAuthCredentials(saved(tmp_path / "c.json", expires_at=0), transport=fake.transport())
    with pytest.raises(OuraAPIError, match="mcp-oura login"):
        await creds.token()


async def test_many_calls_renewing_at_once_in_one_server_spend_the_refresh_token_once(tmp_path, fake):
    # Tools fetch several collections in parallel, so one server renews from many calls at once.
    fake.refresh_tokens.add("refresh-0")
    path = saved(tmp_path / "c.json", expires_at=0)
    creds = auth.OAuthCredentials(path, transport=fake.slow_token_transport())

    assert await asyncio.gather(*(creds.token() for _ in range(5))) == ["access-1"] * 5
    assert fake.token_requests == 1


async def test_a_call_cancelled_mid_renewal_does_not_lose_the_new_tokens(tmp_path, fake):
    # Once Oura has answered, the old refresh token is spent: the new pair must reach the file
    # even if the tool call that started the renewal is cancelled (Esc, a client timeout).
    fake.refresh_tokens.add("refresh-0")
    answered, release = asyncio.Event(), asyncio.Event()

    async def slow_reply(request):
        response = fake.handle(request)
        if request.url.path == "/oauth/token":
            answered.set()
            await release.wait()
        return response

    path = saved(tmp_path / "c.json", expires_at=0)
    creds = auth.OAuthCredentials(path, transport=httpx.MockTransport(slow_reply))
    call = asyncio.create_task(creds.token())
    async with asyncio.timeout(5):
        await answered.wait()
    call.cancel()
    release.set()

    assert await creds.token() == "access-1"
    assert json.loads(path.read_text())["refresh_token"] == "refresh-1"
    assert fake.token_requests == 1


async def test_a_renewal_reply_without_a_lifetime_is_still_saved(tmp_path, fake):
    fake.refresh_tokens.add("refresh-0")
    issue = fake.issue
    fake.issue = lambda: {k: v for k, v in issue().items() if k != "expires_in"}
    path = saved(tmp_path / "c.json", expires_at=0)

    assert await auth.OAuthCredentials(path, transport=fake.transport()).token() == "access-1"
    assert json.loads(path.read_text())["refresh_token"] == "refresh-1"


async def test_a_renewal_reply_without_an_access_token_is_a_readable_error(tmp_path, fake):
    fake.refresh_tokens.add("refresh-0")
    fake.issue = lambda: {"token_type": "bearer"}
    creds = auth.OAuthCredentials(saved(tmp_path / "c.json", expires_at=0), transport=fake.transport())
    with pytest.raises(OuraAPIError, match="no usable access token"):
        await creds.token()


def test_a_credentials_file_others_can_read_is_made_private(tmp_path):
    path = saved(tmp_path / "c.json")
    path.chmod(0o644)
    auth.OAuthCredentials(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_errors_name_the_credentials_file_without_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".config" / "mcp-oura" / "credentials.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    with pytest.raises(OuraAPIError) as error:
        auth.OAuthCredentials(path)
    assert "~/.config/mcp-oura/credentials.json" in str(error.value)
    assert str(tmp_path) not in str(error.value)


async def test_an_outage_during_early_renewal_keeps_the_still_valid_token(tmp_path, fake):
    fake.refresh_tokens.add("refresh-0")
    creds = auth.OAuthCredentials(
        saved(tmp_path / "c.json", expires_at=time.time() + 60), transport=fake.transport()
    )
    fake.fail_token_requests = [503]
    assert await creds.token() == TOKEN


async def test_an_outage_is_not_mistaken_for_a_lost_sign_in(tmp_path, fake):
    fake.refresh_tokens.add("refresh-0")
    creds = auth.OAuthCredentials(saved(tmp_path / "c.json", expires_at=0), transport=fake.transport())
    fake.fail_token_requests = [503]
    with pytest.raises(OuraAPIError, match="try again") as error:
        await creds.token()
    assert "mcp-oura login" not in str(error.value)


def test_an_unreadable_credentials_file_is_named_without_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".config" / "mcp-oura" / "credentials.json"
    path.mkdir(parents=True)  # a directory where the file should be
    with pytest.raises(OuraAPIError) as error:
        auth.OAuthCredentials(path)
    assert str(tmp_path) not in str(error.value)
