"""OAuth sign-in for the Oura API (https://cloud.ouraring.com/docs/authentication).

Oura marks personal access tokens as deprecated; OAuth is the supported way in. `mcp-oura login`
saves a token pair to a credentials file readable only by its owner, and `OAuthCredentials` hands
the server a current access token from it, renewing it when it is about to expire or Oura rejects
it. Oura's refresh tokens work once, so every renewal is written back to the file at once.
"""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, TypedDict, cast

import httpx
from filelock import AsyncFileLock, Timeout

from mcp_oura.client import OuraAPIError

TOKEN_URL = "https://api.ouraring.com/oauth/token"
RENEW_BEFORE_SECONDS = 300
# Assumed when Oura's reply omits the lifetime; a token that dies sooner is renewed on its first 401.
DEFAULT_LIFETIME_SECONDS = 3600
HTTP_TIMEOUT = httpx.Timeout(10.0)
# Longer than a renewal can take (connect, write and read at 10 s each), so a waiting process
# never gives up while another is still legitimately renewing.
LOCK_TIMEOUT_SECONDS = 60
SIGN_IN_AGAIN = "Run `mcp-oura login` to sign in again."


class Credentials(TypedDict):
    """The credentials file, as `mcp-oura login` writes it."""

    client_id: str
    client_secret: str
    access_token: str
    refresh_token: str
    expires_at: float  # Unix time


def credentials_path() -> Path:
    """OURA_CREDENTIALS_FILE, else $XDG_CONFIG_HOME/mcp-oura/credentials.json (~/.config by default)."""
    if explicit := os.environ.get("OURA_CREDENTIALS_FILE"):
        return Path(explicit).expanduser()
    config = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config) / "mcp-oura" / "credentials.json"


def display(path: Path) -> str:
    """The path for messages, with the home directory shortened to ~ so usernames stay out of them."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def save_credentials(path: Path, creds: Credentials) -> None:
    """Write atomically and durably, owner-only: losing a write would lose the one valid refresh token."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # created with mode 0600
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(creds, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    if hasattr(os, "O_DIRECTORY"):  # make the rename itself durable; Windows cannot open directories
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def load_credentials(path: Path) -> Credentials:
    """Read the credentials file, making it owner-only if it was written by hand with looser permissions."""
    try:
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
        creds = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) else str(exc)  # an OSError's text repeats the path
        raise OuraAPIError(
            f"Couldn't read the saved Oura credentials at {display(path)} ({reason}). {SIGN_IN_AGAIN}"
        ) from exc
    if not isinstance(creds, dict) or not Credentials.__required_keys__ <= creds.keys():
        raise OuraAPIError(f"The saved Oura credentials at {display(path)} are incomplete. {SIGN_IN_AGAIN}")
    return cast(Credentials, creds)


def token_fields(body: Any) -> dict[str, Any]:
    """The parts of an Oura token response to keep, with the lifetime turned into a deadline.
    Raises TypeError if the response carries no access token."""
    if not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
        raise TypeError("the reply has no access_token")
    try:
        lifetime = float(body["expires_in"])
    except (KeyError, TypeError, ValueError):
        lifetime = DEFAULT_LIFETIME_SECONDS
    fields: dict[str, Any] = {"access_token": body["access_token"], "expires_at": time.time() + lifetime}
    if isinstance(body.get("refresh_token"), str):
        fields["refresh_token"] = body["refresh_token"]
    return fields


class OAuthCredentials:
    """A TokenSource backed by the credentials file `mcp-oura login` writes.

    Oura's refresh tokens work once, so a refresh token must never be sent twice. Two guards:
    - Within one server, concurrent calls (tools fetch several collections in parallel) share a
      single renewal task. It is shielded from cancellation, because once Oura has answered, the
      new pair must reach the file even if the tool call that started the renewal is abandoned.
    - Across servers (Claude Code and Claude Desktop each start one), a renewal holds a file lock
      and re-reads the file first, so the second server reuses the first one's new tokens.
    The file lock alone would not do the first job: a filelock object lets its own holder re-enter.

    One gap remains: a server killed after Oura answers a renewal but before the new pair is saved
    leaves a spent refresh token on disk, and the next renewal asks the user to sign in again.
    """

    def __init__(self, path: Path, *, transport: httpx.AsyncBaseTransport | None = None):
        self.path = path
        self._transport = transport
        self._lock = AsyncFileLock(f"{path}.lock", timeout=LOCK_TIMEOUT_SECONDS)
        self._creds = load_credentials(path)
        self._renewal: asyncio.Task[Credentials | OuraAPIError] | None = None

    async def token(self) -> str:
        creds = self._creds
        if not _expiring(creds):
            return creds["access_token"]
        try:
            return await self.refresh(creds["access_token"])
        except OuraAPIError:
            if creds["expires_at"] > time.time():
                return creds["access_token"]  # renewing early failed, but this one still works
            raise

    async def refresh(self, rejected: str) -> str:
        if self._renewal is None or self._renewal.done():
            self._renewal = asyncio.create_task(self._renewal_outcome(rejected))
        outcome = await asyncio.shield(self._renewal)
        if isinstance(outcome, OuraAPIError):
            raise OuraAPIError(str(outcome))  # a fresh instance per caller; the outcome is shared
        self._creds = outcome
        return outcome["access_token"]

    async def _renewal_outcome(self, rejected: str) -> Credentials | OuraAPIError:
        # Returned rather than raised: when the caller was cancelled, nobody awaits the task, and an
        # unretrieved exception would end up as a traceback in the MCP client's log.
        try:
            return await self._renew_unless_done(rejected)
        except OuraAPIError as exc:
            return exc

    async def _renew_unless_done(self, rejected: str) -> Credentials:
        try:
            async with self._lock:
                current = load_credentials(self.path)
                if current["access_token"] != rejected and not _expiring(current):
                    return current  # another server renewed it while this one waited for the lock
                return await self._renew(current)
        except Timeout:
            raise OuraAPIError(
                f"Another mcp-oura process held {display(Path(self._lock.lock_file))} "
                f"for {LOCK_TIMEOUT_SECONDS} s; try again."
            ) from None

    async def _renew(self, creds: Credentials) -> Credentials:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": creds["refresh_token"],
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        }
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=HTTP_TIMEOUT) as http:
                response = await http.post(TOKEN_URL, data=form)
        except httpx.TransportError as exc:
            raise OuraAPIError(
                f"Couldn't reach Oura to refresh the access token ({type(exc).__name__}); try again."
            ) from exc
        if response.status_code in (400, 401):
            raise OuraAPIError(
                f"Oura refused to refresh the access token (HTTP {response.status_code}). {SIGN_IN_AGAIN}"
            )
        if response.status_code != 200:
            raise OuraAPIError(
                f"Oura's token service failed (HTTP {response.status_code}); try again shortly."
            )
        try:
            fields = token_fields(response.json())
        except (TypeError, ValueError):  # not JSON, or no access token in it
            raise OuraAPIError(
                f"Oura's reply to the token refresh had no usable access token. {SIGN_IN_AGAIN}"
            ) from None
        renewed = cast(Credentials, {**creds, **fields})
        save_credentials(self.path, renewed)
        return renewed


def _expiring(creds: Credentials) -> bool:
    return creds["expires_at"] - time.time() < RENEW_BEFORE_SECONDS
