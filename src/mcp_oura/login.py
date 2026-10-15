"""Browser sign-in (`mcp-oura login`) and sign-out (`mcp-oura logout`) for the Oura API.

Sign-in is the OAuth authorization-code flow with PKCE (RFC 7636) and a loopback redirect
(RFC 8252): you approve access on Oura's site, Oura sends the browser back to a one-shot HTTP
server on this computer, and the code it carries is exchanged for a token pair that
`auth.save_credentials` writes for the server to use.
"""

import base64
import errno
import hashlib
import secrets
import select
import socket
import sys
import time
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from filelock import FileLock, Timeout

from mcp_oura import auth
from mcp_oura.client import OuraAPIError

AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
REVOKE_URL = "https://api.ouraring.com/oauth/revoke"
DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
# Everything the tools read, as Oura's developer portal lists the scopes. Not `email`: nothing here
# needs it. `ring_configuration` and `stress` are newer than the published OpenAPI scope list.
SCOPES = "personal daily heartrate workout tag session spo2 ring_configuration stress heart_health"


class LoginError(Exception):
    """Sign-in did not complete; the message says why and what to do next."""


def pkce_challenge(verifier: str) -> str:
    """The S256 code challenge for a PKCE code verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def login(
    client_id: str,
    client_secret: str,
    *,
    path: Path,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    open_browser: Callable[[str], bool] | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 300.0,
) -> None:
    """Sign in through the browser and save the token pair to `path`."""
    target = urlsplit(redirect_uri)
    try:
        port = target.port
    except ValueError:  # not a number, or out of range
        port = None
    if (
        target.scheme != "http"
        or target.hostname not in ("localhost", "127.0.0.1")
        or port is None
        or not target.path
    ):
        raise LoginError(
            f"The redirect URI must be http://localhost:<port>/<path>, the address this computer "
            f"listens on during sign-in; got {redirect_uri!r}."
        )
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    servers = _listen(target.hostname, port, target.path, state)
    try:
        # Port 0 asks the system for a free port; the redirect must name the one we got.
        redirect_uri = target._replace(netloc=f"{target.hostname}:{servers[0].server_address[1]}").geturl()
        query = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        url = f"{AUTHORIZE_URL}?{urlencode(query)}"
        print(f"Approve access in your browser. If it doesn't open, visit:\n\n  {url}\n", file=sys.stderr)
        (open_browser or webbrowser.open)(url)
        answer = _wait_for_redirect(servers, timeout)
    finally:
        for server in servers:
            server.server_close()

    if "error" in answer:
        raise LoginError(f"Oura did not grant access ({answer['error']}).")
    tokens = _exchange(answer["code"], verifier, redirect_uri, client_id, client_secret, transport)
    creds = {"client_id": client_id, "client_secret": client_secret, **auth.token_fields(tokens)}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # before the lock file creates it 0755
    with _locked(path):  # a running server's renewal must not overwrite the new sign-in
        auth.save_credentials(path, cast(auth.Credentials, creds))


def logout(path: Path, *, transport: httpx.BaseTransport | None = None) -> str:
    """Revoke both saved tokens with Oura, delete the saved credentials, and say what happened.

    Revoking the access token leaves the refresh token working, so each is revoked on its own. Until
    Oura confirms both, the file is kept and `logout` can be run again."""
    if not path.exists():
        return f"Not signed in: there are no saved credentials at {auth.display(path)}."
    # Waits for a refresh in progress, so it cannot write live credentials back after the delete.
    # The lock file stays: deleting a lock file another process may hold breaks the lock.
    with _locked(path):
        if not path.exists():  # another logout finished while this one waited for the lock
            return f"Not signed in: there are no saved credentials at {auth.display(path)}."
        try:
            creds = auth.load_credentials(path)
        except OuraAPIError:
            path.unlink(missing_ok=True)
            return f"Deleted {auth.display(path)} without revoking anything: it was unreadable or incomplete."
        with httpx.Client(transport=transport, timeout=30.0) as http:
            unconfirmed = [
                f"the {kind} token ({reason})"
                for kind, token in (("access", creds["access_token"]), ("refresh", creds["refresh_token"]))
                if (reason := _revoke(http, token))
            ]
        if unconfirmed:
            raise LoginError(
                f"Oura did not confirm revoking {' or '.join(unconfirmed)}, so the saved credentials are "
                f"kept at {auth.display(path)}. Run `mcp-oura logout` again later."
            )
        path.unlink(missing_ok=True)  # another logout may have finished first
    return f"Signed out: Oura no longer accepts the saved tokens, and {auth.display(path)} is deleted."


def _revoke(http: httpx.Client, token: str) -> str | None:
    """Revoke one token; None once Oura no longer accepts it, else why that is not confirmed."""
    try:
        # Oura's revoke endpoint takes either token in this parameter and revokes only that one.
        response = http.post(REVOKE_URL, params={"access_token": token})
    except httpx.RequestError as exc:  # no connection, or a reply that could not be read
        return f"no usable reply from Oura: {type(exc).__name__}"
    if response.status_code in (200, 401):  # 401: already revoked, expired or unknown
        return None
    return f"HTTP {response.status_code}"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """The lock `OAuthCredentials` takes while renewing, held here from another process."""
    try:
        with FileLock(f"{path}.lock", timeout=auth.LOCK_TIMEOUT_SECONDS):
            yield
    except Timeout:
        raise LoginError(
            f"Another mcp-oura process held the lock on {auth.display(path)} for "
            f"{auth.LOCK_TIMEOUT_SECONDS} s; try again."
        ) from None


def _exchange(
    code: str,
    verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": verifier,
    }
    try:
        with httpx.Client(transport=transport, timeout=30.0) as http:
            response = http.post(auth.TOKEN_URL, data=form)
    except httpx.TransportError as exc:
        raise LoginError(f"Couldn't reach Oura to finish signing in ({type(exc).__name__}).") from exc
    if response.status_code in (400, 401):
        raise LoginError(
            f"Oura refused the sign-in (HTTP {response.status_code}): {response.text[:200]}. "
            "Check the client ID and secret."
        )
    if response.status_code != 200:
        raise LoginError(f"Oura's token service failed (HTTP {response.status_code}); try again shortly.")
    try:
        body = response.json()
        auth.token_fields(body)
    except (TypeError, ValueError):  # not JSON, or no access token in it
        raise LoginError("Oura's reply had no usable access token. Run `mcp-oura login` again.") from None
    if not isinstance(body.get("refresh_token"), str):
        raise LoginError(
            "Oura's reply had no refresh token, so the access token could not be refreshed later."
        )
    return cast(dict[str, Any], body)


def _listen(host: str, port: int, path: str, state: str) -> list["_CallbackServer"]:
    """Listen for the redirect on 127.0.0.1 and, for `localhost`, on ::1 as well: browsers may try ::1
    first, and a port left free there could be taken by another program to receive the code."""
    try:
        servers = [_CallbackServer(("127.0.0.1", port), path, state)]
    except OSError as exc:
        raise LoginError(
            f"Couldn't listen on port {port} ({exc.strerror}). Free the port, or register another "
            "redirect URI on your Oura application and pass it with --redirect-uri."
        ) from None
    if host == "localhost":
        try:
            servers.append(_CallbackServer6(("::1", servers[0].server_address[1]), path, state))
        except OSError as exc:
            # Only a computer without an IPv6 loopback may go ahead on 127.0.0.1 alone. Any other failure
            # can mean another program holds the port (on Windows an exclusive hold is EACCES).
            if exc.errno not in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
                servers[0].server_close()
                raise LoginError(
                    f"Port {port} is in use or unavailable on the IPv6 loopback (::1) ({exc.strerror}), so "
                    "another program could receive the sign-in. Stop it, or pass another redirect URI with "
                    "--redirect-uri."
                ) from None
    return servers


def _wait_for_redirect(servers: list["_CallbackServer"], timeout: float) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while not (answers := [s.answer for s in servers if s.answer]):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LoginError(
                f"Timed out after {timeout:g} s waiting for the browser to come back from Oura. "
                "Run `mcp-oura login` again."
            )
        ready, _, _ = select.select(servers, [], [], remaining)
        for server in ready:
            server.handle_request()
    return answers[0]


class _CallbackServer(HTTPServer):
    """Takes Oura's redirect on the loopback interface. Only a redirect carrying this sign-in's state
    is accepted: one with the wrong state gets a 400, any other request a 404, and the wait goes on."""

    timeout = 0  # handle_request is only called once select says a connection is waiting
    # On Windows, SO_REUSEADDR lets a second program bind the same port; exclusive use is the default
    # elsewhere and is asked for explicitly there.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address: tuple[str, int], path: str, state: str):
        self.callback_path = path
        self.state = state.encode()
        self.answer: dict[str, str] = {}
        super().__init__(address, _CallbackHandler)

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _CallbackServer6(_CallbackServer):
    address_family = socket.AF_INET6


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer
    timeout = 10  # a connection that never sends a request must not stall the sign-in

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        answer = dict(parse_qsl(url.query))
        if url.path != self.server.callback_path or not {"code", "error"} & answer.keys():
            self.send_error(404)  # favicon requests, prefetches, anything but Oura's redirect
            return
        if not secrets.compare_digest(answer.get("state", "").encode(), self.server.state):
            self._page(400, "This link does not belong to the sign-in in progress.")
            return
        self.server.answer = answer
        if "code" in answer:
            self._page(200, "Signed in to Oura. You can close this tab.")
        else:
            self._page(400, "Oura sign-in did not complete. The terminal says why.")

    def _page(self, status: int, message: str) -> None:
        body = f"<!doctype html><meta charset=utf-8><title>mcp-oura</title><p>{message}</p>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Log nothing: the request line carries the authorization code."""
