"""A small async client for the Oura API v2 (https://cloud.ouraring.com/v2/docs).

Two quirks of the API, observed on 2026-10-13, are handled here so the rest
of the server never sees them:
- `end_date` is inclusive on the daily score endpoints (daily_sleep,
  daily_readiness, daily_stress, daily_spo2, ...) but exclusive on sleep,
  daily_activity and workout. `days()` asks for one extra day and trims by
  each record's `day`, so every endpoint answers for exactly [start, end].
- Results are paged; `collection()` follows `next_token` to the end.

Access tokens come from a `TokenSource`: a fixed personal access token, or
OAuth tokens from `mcp-oura login` (see `auth.py`) that renew themselves.
"""

import asyncio
from datetime import date, timedelta
from typing import Any, Protocol

import httpx

BASE_URL = "https://api.ouraring.com/v2/usercollection/"
MAX_RETRY_AFTER_SECONDS = 30.0


class OuraAPIError(RuntimeError):
    """The Oura API refused the request or could not be reached."""


class TokenSource(Protocol):
    async def token(self) -> str:
        """The access token to send."""
        ...

    async def refresh(self, rejected: str) -> str:
        """A new access token after Oura rejected `rejected`; raises OuraAPIError if there is none."""
        ...


class StaticToken:
    """A personal access token from OURA_ACCESS_TOKEN. It cannot be renewed."""

    def __init__(self, token: str):
        self._token = token

    async def token(self) -> str:
        return self._token

    async def refresh(self, rejected: str) -> str:
        raise OuraAPIError(
            "The Oura API rejected the access token (401). Check that OURA_ACCESS_TOKEN holds "
            "a valid, unrevoked Oura access token."
        )


class OuraClient:
    def __init__(
        self,
        tokens: TokenSource | str,
        *,
        timezone: str,
        transport: httpx.AsyncBaseTransport | None = None,
        retries: int = 3,
        retry_delay: float = 1.0,
        timeout: float = 30.0,
    ):
        self.timezone = timezone
        self.retries = retries
        self.retry_delay = retry_delay
        self._tokens = StaticToken(tokens) if isinstance(tokens, str) else tokens
        self._http = httpx.AsyncClient(base_url=BASE_URL, transport=transport, timeout=timeout)

    async def _get(self, endpoint: str, params: dict[str, str]) -> Any:
        token = await self._tokens.token()
        response = await self._send(endpoint, params, token)
        if response.status_code == 401:
            # An OAuth access token can be revoked or expire early: renew it once and try again.
            response = await self._send(endpoint, params, await self._tokens.refresh(token))
            if response.status_code == 401:
                raise OuraAPIError(
                    "The Oura API rejected the access token even after refreshing it (401). "
                    "Run `mcp-oura login` to sign in again."
                )
        if response.status_code >= 400:
            raise OuraAPIError(
                f"The Oura API refused {endpoint} (HTTP {response.status_code}): {_detail(response)}"
            )
        return response.json()

    async def _send(self, endpoint: str, params: dict[str, str], token: str) -> httpx.Response:
        """One GET, retried on connection failures, 429 and 5xx; any other response is returned."""
        last = ""
        for attempt in range(1, self.retries + 1):
            delay = self.retry_delay * attempt
            try:
                response = await self._http.get(
                    endpoint, params=params, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.TransportError as exc:
                last = f"couldn't connect: {type(exc).__name__}: {exc}"
            else:
                if response.status_code != 429 and response.status_code < 500:
                    return response
                last = f"HTTP {response.status_code}"
                retry_after = response.headers.get("retry-after", "")
                if retry_after.isdigit():
                    delay = min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
            if attempt < self.retries:
                await asyncio.sleep(delay)
        if last.startswith("couldn't connect"):
            raise OuraAPIError(f"Couldn't reach the Oura API after {self.retries} attempts ({last}).")
        raise OuraAPIError(f"The Oura API failed on {endpoint} after {self.retries} attempts ({last}).")

    async def collection(self, endpoint: str, params: dict[str, str]) -> list[dict[str, Any]]:
        """Every record the endpoint returns for `params`, across all pages."""
        params = dict(params)
        records: list[dict[str, Any]] = []
        while True:
            body = await self._get(endpoint, params)
            records.extend(body.get("data", []))
            next_token = body.get("next_token")
            if not next_token:
                return records
            params["next_token"] = next_token

    async def days(self, endpoint: str, start: date, end: date) -> list[dict[str, Any]]:
        """Records whose `day` falls in [start, end], both ends included, on any endpoint."""
        records = await self.collection(
            endpoint, {"start_date": start.isoformat(), "end_date": (end + timedelta(days=1)).isoformat()}
        )
        return [r for r in records if (day := _day(r)) is None or start <= day <= end]

    async def document(self, endpoint: str) -> dict[str, Any]:
        """A single-object endpoint such as personal_info."""
        document: dict[str, Any] = await self._get(endpoint, {})
        return document

    async def aclose(self) -> None:
        await self._http.aclose()


def _day(record: dict[str, Any]) -> date | None:
    """The day a record belongs to; rest_mode_period and enhanced_tag use start_day instead of day."""
    value = record.get("day") or record.get("start_day")
    return date.fromisoformat(value) if value else None


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    return str(body.get("detail", body)) if isinstance(body, dict) else str(body)[:300]
