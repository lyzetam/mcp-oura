"""`mcp-oura login` and `logout`, with a stand-in for the browser and the fake Oura API."""

import asyncio
import errno
import json
import os
import socket
import stat
import threading
import time
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from filelock import FileLock

from fake_oura import CLIENT_ID, CLIENT_SECRET
from mcp_oura import auth, login
from mcp_oura.cli import main

LOOPBACK = "http://localhost:0/callback"  # port 0: any free port, so tests never collide


def browser(fake, *, approve=True, forged=(), host=None):
    """Stands in for the user: reads the authorize URL, then lets Oura redirect back. `forged`
    redirects (another program's guesses) arrive first; `host` overrides the redirect's host."""
    seen = {}

    def open_url(url: str) -> bool:
        q = dict(parse_qsl(urlsplit(url).query))
        seen.update(q)
        fake.codes["code-1"] = q["code_challenge"]
        back = urlsplit(q["redirect_uri"])
        if host:
            back = back._replace(netloc=f"{host}:{back.port}")
        answer = {"code": "code-1"} if approve else {"error": "access_denied"}

        def redirect():
            for params in (*forged, {**answer, "state": q["state"]}):
                seen.setdefault("statuses", []).append(
                    httpx.get(back.geturl(), params=params, trust_env=False).status_code
                )

        threading.Thread(target=redirect).start()
        return True

    return open_url, seen


def sign_in(fake, path, **browser_options):
    open_url, seen = browser(fake, **browser_options)
    login.login(
        CLIENT_ID,
        CLIENT_SECRET,
        path=path,
        redirect_uri=LOOPBACK,
        open_browser=open_url,
        transport=fake.transport(),
        timeout=5,
    )
    return seen


def test_pkce_challenge_matches_the_example_in_ouras_docs():
    # https://cloud.ouraring.com/docs/authentication, "Example authorization request"
    verifier = "QUW2bsoMYITWSSPUgCDEc4CXCBfcVyu8TkujreHe8CXP4K4d4463gRztuxfo96YP"
    assert login.pkce_challenge(verifier) == "43Kb1gELSIjkvwzrPtAg0Lz2HqCG3BtS_SUMAQhjv7c"


def test_login_saves_a_token_pair_only_the_owner_can_read(tmp_path, fake):
    path = tmp_path / "mcp-oura" / "credentials.json"  # a first sign-in creates the directory
    seen = sign_in(fake, path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    creds = json.loads(path.read_text())
    assert creds["access_token"] == "access-1"
    assert creds["refresh_token"] == "refresh-1"
    assert (creds["client_id"], creds["client_secret"]) == (CLIENT_ID, CLIENT_SECRET)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert seen["code_challenge_method"] == "S256"
    # Every scope a tool reads, as listed in Oura's developer portal, and nothing more.
    assert set(seen["scope"].split()) == {
        "personal",
        "daily",
        "heartrate",
        "workout",
        "tag",
        "session",
        "spo2",
        "ring_configuration",
        "stress",
        "heart_health",
    }
    assert auth.OAuthCredentials(path)  # the server can load what login wrote


def test_redirects_for_another_sign_in_are_ignored_and_the_real_one_completes(tmp_path, fake):
    # Another program can send requests to the callback; without our state they change nothing.
    forged = ({"code": "stolen", "state": "guess"}, {"error": "access_denied"}, {"code": "x", "state": "é"})
    seen = sign_in(fake, tmp_path / "c.json", forged=forged)

    assert seen["statuses"] == [400, 400, 400, 200]
    assert json.loads((tmp_path / "c.json").read_text())["access_token"] == "access-1"
    assert fake.token_requests == 1  # only the real code was exchanged


def test_login_also_listens_on_the_ipv6_loopback_so_no_other_program_can(tmp_path, fake):
    # Browsers may resolve localhost to ::1 first.
    if not socket.has_ipv6:
        pytest.skip("no IPv6 on this computer")
    sign_in(fake, tmp_path / "c.json", host="[::1]")
    assert (tmp_path / "c.json").exists()


def test_login_refuses_to_start_when_another_program_holds_the_ipv6_loopback(tmp_path):
    if not socket.has_ipv6:
        pytest.skip("no IPv6 on this computer")
    with socket.socket(socket.AF_INET6) as squatter:
        squatter.bind(("::1", 0))
        squatter.listen()
        port = squatter.getsockname()[1]
        with pytest.raises(login.LoginError, match="in use"):
            login.login(
                CLIENT_ID,
                CLIENT_SECRET,
                path=tmp_path / "c.json",
                redirect_uri=f"http://localhost:{port}/cb",
                open_browser=lambda url: True,
                timeout=0.3,
            )


def test_declining_in_oura_says_so(tmp_path, fake):
    with pytest.raises(login.LoginError, match="access_denied"):
        sign_in(fake, tmp_path / "c.json", approve=False)
    assert not (tmp_path / "c.json").exists()


def test_login_gives_up_when_the_browser_never_comes_back(tmp_path, fake):
    with pytest.raises(login.LoginError, match="Timed out"):
        login.login(
            CLIENT_ID,
            CLIENT_SECRET,
            path=tmp_path / "c.json",
            redirect_uri=LOOPBACK,
            open_browser=lambda url: True,
            timeout=0.3,
        )


@pytest.mark.parametrize(
    "redirect_uri", ["https://example.com/cb", "http://localhost:8765", "http://localhost:99999/cb"]
)
def test_only_a_loopback_redirect_with_a_port_and_path_is_accepted(tmp_path, redirect_uri):
    with pytest.raises(login.LoginError, match="localhost"):
        login.login(
            CLIENT_ID,
            CLIENT_SECRET,
            path=tmp_path / "c.json",
            redirect_uri=redirect_uri,
            open_browser=lambda url: True,
            timeout=0.3,
        )


def test_logout_revokes_both_tokens_and_deletes_the_credentials(tmp_path, fake):
    path = tmp_path / "credentials.json"
    sign_in(fake, path)

    message = login.logout(path, transport=fake.transport())
    assert "Signed out" in message
    assert not path.exists()
    assert "access-1" not in fake.access_tokens
    assert "refresh-1" not in fake.refresh_tokens  # else a copy of the file could still mint tokens


def test_logout_deletes_credentials_whose_tokens_oura_already_refuses(tmp_path, fake):
    path = tmp_path / "credentials.json"
    sign_in(fake, path)
    fake.access_tokens.clear()  # expired, or revoked by an earlier logout
    fake.refresh_tokens.clear()

    assert "Signed out" in login.logout(path, transport=fake.transport())
    assert not path.exists()


@pytest.mark.parametrize("failure", ["unreachable", 503])
def test_logout_keeps_the_credentials_until_oura_confirms_both_tokens_are_revoked(tmp_path, fake, failure):
    path = tmp_path / "credentials.json"
    sign_in(fake, path)

    def refresh_revoke_fails(request):
        if request.url.params.get("access_token") == "refresh-1":
            if failure == "unreachable":
                raise httpx.ConnectError("no route to host", request=request)
            return httpx.Response(failure)
        return fake.handle(request)

    with pytest.raises(login.LoginError, match="refresh token.*kept"):
        login.logout(path, transport=httpx.MockTransport(refresh_revoke_fails))
    assert path.exists()
    assert "access-1" not in fake.access_tokens

    assert "Signed out" in login.logout(path, transport=fake.transport())  # Oura refuses access-1 now
    assert not path.exists()
    assert "refresh-1" not in fake.refresh_tokens


def test_logout_keeps_the_credentials_when_oura_sends_a_garbled_reply(tmp_path, fake):
    path = tmp_path / "credentials.json"
    sign_in(fake, path)

    def garbled(request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b"not gzip")

    with pytest.raises(login.LoginError, match="kept"):
        login.logout(path, transport=httpx.MockTransport(garbled))
    assert path.exists()


def test_a_logout_that_waited_for_another_to_finish_says_there_is_nothing_left(tmp_path, fake):
    path = tmp_path / "credentials.json"
    sign_in(fake, path)
    other = FileLock(f"{path}.lock")
    other.acquire()
    result: list[str] = []
    logging_out = threading.Thread(
        target=lambda: result.append(login.logout(path, transport=fake.transport()))
    )
    logging_out.start()
    time.sleep(0.2)  # waiting for the lock
    path.unlink()  # the other logout finishes
    other.release()
    logging_out.join(5)
    assert "Not signed in" in result[0]


def test_logout_deletes_an_unreadable_credentials_file(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text("{not json")

    assert "unreadable" in login.logout(path)
    assert not path.exists()


def held_refresh(fake):
    """A transport whose refresh requests wait at Oura until `release` is set. `asked` is set once
    Oura has received one, so the old refresh token is spent and the new pair is not yet saved."""
    asked, release = asyncio.Event(), asyncio.Event()

    async def handle(request):
        response = fake.handle(request)
        if request.url.path == "/oauth/token" and b"grant_type=refresh_token" in request.content:
            asked.set()
            await release.wait()
        return response

    return httpx.MockTransport(handle), asked, release


async def refresh_in_flight(fake, path):
    """A running server's refresh, stopped between Oura's answer and the save."""
    auth.save_credentials(path, {**json.loads(path.read_text()), "expires_at": 0})
    transport, asked, release = held_refresh(fake)
    refreshing = asyncio.create_task(auth.OAuthCredentials(path, transport=transport).token())
    async with asyncio.timeout(5):
        await asked.wait()
    return refreshing, release


async def test_logout_waits_for_a_refresh_in_flight_so_it_cannot_write_the_credentials_back(tmp_path, fake):
    path = tmp_path / "credentials.json"
    sign_in(fake, path)
    refreshing, release = await refresh_in_flight(fake, path)

    logging_out = asyncio.create_task(asyncio.to_thread(login.logout, path, transport=fake.transport()))
    await asyncio.sleep(0.2)
    assert not logging_out.done()  # held by the refresh's lock
    release.set()
    async with asyncio.timeout(5):
        assert await refreshing == "access-2"
        assert "Signed out" in await logging_out
    assert not path.exists()
    assert "access-2" not in fake.access_tokens
    assert "refresh-2" not in fake.refresh_tokens


async def test_login_waits_for_a_refresh_in_flight_so_old_credentials_cannot_overwrite_new(tmp_path, fake):
    path = tmp_path / "credentials.json"
    sign_in(fake, path)  # access-1
    refreshing, release = await refresh_in_flight(fake, path)  # the old grant's refresh: access-2

    signing_in = asyncio.create_task(asyncio.to_thread(sign_in, fake, path))  # a new grant: access-3
    await asyncio.sleep(0.2)
    assert not signing_in.done()  # holding its new credentials until the refresh has saved
    release.set()
    async with asyncio.timeout(5):
        await refreshing
        await signing_in
    assert json.loads(path.read_text())["access_token"] == "access-3"


def test_logout_without_a_sign_in_says_so(tmp_path):
    assert "Not signed in" in login.logout(tmp_path / "missing.json")


def test_the_cli_needs_a_client_id_to_sign_in(monkeypatch, capsys):
    monkeypatch.delenv("OURA_CLIENT_ID", raising=False)
    with pytest.raises(SystemExit):
        main(["login"])
    assert "OURA_CLIENT_ID" in capsys.readouterr().err


def test_the_cli_reports_its_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.startswith("mcp-oura ")


def test_the_cli_signs_in_and_says_where_it_saved(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setenv("OURA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("OURA_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("OURA_CREDENTIALS_FILE", str(tmp_path / "c.json"))
    monkeypatch.setattr(login, "login", lambda *args, **kwargs: calls.append((args, kwargs)))

    main(["login", "--no-browser"])
    ((args, kwargs),) = calls
    assert args == (CLIENT_ID, CLIENT_SECRET)
    assert kwargs["path"] == tmp_path / "c.json"
    assert kwargs["open_browser"]("https://example.com") is False
    assert "Signed in" in capsys.readouterr().out


def test_the_cli_turns_a_failed_sign_in_into_a_readable_exit(monkeypatch):
    def refused(*args, **kwargs):
        raise login.LoginError("Oura did not grant access (access_denied).")

    monkeypatch.setenv("OURA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("OURA_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setattr(login, "login", refused)
    with pytest.raises(SystemExit, match="access_denied"):
        main(["login"])


def test_a_sign_in_reply_without_an_access_token_is_refused(tmp_path, fake):
    fake.issue = lambda: {"token_type": "bearer", "refresh_token": "r"}
    with pytest.raises(login.LoginError, match="no usable access token"):
        sign_in(fake, tmp_path / "c.json")
    assert not (tmp_path / "c.json").exists()


def test_with_no_command_the_cli_runs_the_server(monkeypatch, tmp_path):
    # What Claude Code and Claude Desktop start: no subcommand at all.
    monkeypatch.delenv("OURA_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("OURA_CREDENTIALS_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(SystemExit, match="not signed in"):
        main([])


def test_the_cli_signs_out(monkeypatch, tmp_path, fake, capsys):
    path = tmp_path / "credentials.json"
    sign_in(fake, path)
    monkeypatch.setenv("OURA_CREDENTIALS_FILE", str(path))
    monkeypatch.setattr(login, "logout", lambda p: f"signed out of {p.name}")
    main(["logout"])
    assert capsys.readouterr().out.strip() == "signed out of credentials.json"


def test_the_cli_turns_ctrl_c_during_sign_in_into_a_short_message(monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setenv("OURA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("OURA_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setattr(login, "login", interrupted)
    with pytest.raises(SystemExit, match="cancelled"):
        main(["login"])


def test_a_sign_in_reply_without_a_refresh_token_is_refused(tmp_path, fake):
    fake.issue = lambda: {"token_type": "bearer", "access_token": "a", "expires_in": 60}
    with pytest.raises(login.LoginError, match="no refresh token"):
        sign_in(fake, tmp_path / "c.json")
    assert not (tmp_path / "c.json").exists()


def test_an_outage_during_sign_in_is_not_blamed_on_the_client_secret(tmp_path, fake):
    fake.fail_token_requests = [503]
    with pytest.raises(login.LoginError, match="try again") as error:
        sign_in(fake, tmp_path / "c.json")
    assert "client ID" not in str(error.value)


@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM, errno.EADDRINUSE])
def test_login_refuses_to_start_when_the_ipv6_loopback_cannot_be_secured(monkeypatch, tmp_path, error):
    # On Windows a port held exclusively by another program fails with EACCES, not EADDRINUSE.
    def unavailable(*args, **kwargs):
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(login, "_CallbackServer6", unavailable)
    with pytest.raises(login.LoginError, match="::1"):
        login.login(
            CLIENT_ID,
            CLIENT_SECRET,
            path=tmp_path / "c.json",
            redirect_uri=LOOPBACK,
            open_browser=lambda url: True,
            timeout=0.3,
        )


@pytest.mark.parametrize("error", [errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT])
def test_login_uses_127_0_0_1_alone_on_a_computer_without_ipv6(monkeypatch, tmp_path, fake, error):
    def no_ipv6(*args, **kwargs):
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(login, "_CallbackServer6", no_ipv6)
    sign_in(fake, tmp_path / "c.json", host="127.0.0.1")
    assert (tmp_path / "c.json").exists()


def test_two_logouts_at_once_do_not_crash(monkeypatch, tmp_path, fake):
    path = tmp_path / "credentials.json"
    sign_in(fake, path)
    read = auth.load_credentials

    def read_then_lose_the_race(p):
        creds = read(p)
        p.unlink()  # the other logout deletes the file first
        return creds

    monkeypatch.setattr(auth, "load_credentials", read_then_lose_the_race)
    assert "Signed out" in login.logout(path, transport=fake.transport())


def test_the_cli_turns_ctrl_c_at_the_secret_prompt_into_a_short_message(monkeypatch):
    def interrupted(prompt):
        raise KeyboardInterrupt

    monkeypatch.setenv("OURA_CLIENT_ID", CLIENT_ID)
    monkeypatch.delenv("OURA_CLIENT_SECRET", raising=False)
    monkeypatch.setattr("getpass.getpass", interrupted)
    with pytest.raises(SystemExit, match="cancelled"):
        main(["login"])
