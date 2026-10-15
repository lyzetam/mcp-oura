"""Oura MCP server: read-only tools over the live Oura API v2, run locally over stdio."""

import functools
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tzlocal
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_oura import __version__, auth, health
from mcp_oura.client import OuraAPIError, OuraClient, StaticToken, TokenSource

INSTRUCTIONS = (
    "Read-only, live access to the user's Oura Ring data through the Oura API: sleep, readiness, "
    "activity, stress, SpO2, resilience, heart rate, workouts, VO2 max and cardiovascular age. Start "
    "with get_daily_summary for one day or get_daily_metrics for a run of days; use get_trends for "
    "weeks, months or years. Oura files a night's sleep under the morning the user woke up, so 'last "
    "night' is today's date; new data appears only after the ring syncs through the Oura app. Times "
    "are the user's local time. Scores run 0-100. For anything else the API holds (tags, sessions, "
    "rest mode, ring details, raw records), use get_oura_data."
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)

MAX_RANGE_DAYS = 400
MAX_HEART_RATE_DAYS = 30  # the Oura API's own limit per heartrate request
MAX_HEART_RATE_BUCKETS = 1500

DateArg = Annotated[str | None, Field(description="A date as YYYY-MM-DD.")]


def _date(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"{name} must be a date written YYYY-MM-DD, like 2026-10-12; got {value!r}"
        ) from None


def _datetime(value: str, name: str, tz: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"{name} must be YYYY-MM-DD or YYYY-MM-DDTHH:MM in local time; got {value!r}"
        ) from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
    return parsed


def _utc(local: datetime, tz: str) -> datetime:
    return local.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


def _anticipated(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Pass bad-argument and Oura API errors to the model as readable tool errors; the SDK replaces
    the text of any other exception with a generic 'Error executing tool'."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except (ValueError, OuraAPIError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def create_server(oura: OuraClient, today: Callable[[], date] | None = None) -> MCPServer:
    today = today or (lambda: datetime.now(ZoneInfo(oura.timezone)).date())

    def date_range(start_date: str | None, end_date: str | None, max_days: int | None) -> tuple[date, date]:
        end = _date(end_date, "end_date") if end_date else today()
        start = _date(start_date, "start_date") if start_date else end - timedelta(days=6)
        if start > end:
            raise ValueError(f"start_date {start} is after end_date {end}.")
        if max_days and (end - start).days + 1 > max_days:
            raise ValueError(
                f"That range is {(end - start).days + 1} days; this tool takes at most {max_days}. "
                "Use get_trends for longer spans."
            )
        return start, end

    def span(start: date, end: date) -> dict[str, str]:
        return {"start_date": start.isoformat(), "end_date": end.isoformat()}

    server = MCPServer("mcp-oura", version=__version__, instructions=INSTRUCTIONS)

    def tool(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        return server.tool(annotations=READ_ONLY)(_anticipated(fn))

    @tool
    async def get_daily_summary(
        day: Annotated[
            str | None, Field(description="YYYY-MM-DD. Omit for the most recent day with data.")
        ] = None,
    ) -> dict[str, Any]:
        """Everything Oura recorded for one day: sleep score and what drove it, the night's details
        (bedtime, hours, deep/REM/light, efficiency, latency, HRV, heart rate, breathing), naps,
        readiness and its contributors, activity and steps, stress, SpO2, resilience, cardiovascular
        age, latest VO2 max, and workouts. `missing` lists categories with no data that day."""
        return await health.daily_summary(oura, _date(day, "day") if day else None, today=today())

    @tool
    async def get_daily_metrics(start_date: DateArg = None, end_date: DateArg = None) -> dict[str, Any]:
        """One row per day with the headline numbers: sleep, readiness and activity scores, sleep hours,
        deep and REM hours, sleep efficiency, HRV, lowest heart rate, temperature deviation, steps,
        active calories, stress and recovery minutes, SpO2, resilience, and VO2 max on the days Oura
        measured it. Defaults to the last 7 days; at most 400 days per call. Days with no data are
        left out."""
        start, end = date_range(start_date, end_date, MAX_RANGE_DAYS)
        return {**span(start, end), **await health.daily_metrics(oura, start, end)}

    @tool
    async def get_trends(
        start_date: DateArg = None,
        end_date: DateArg = None,
        period: Annotated[str, Field(description="week, month, quarter or year.")] = "month",
    ) -> dict[str, Any]:
        """Averages of the daily headline numbers per week, month, quarter or year, with how many days
        of data each period has. Weeks start on Monday. VO2 max is averaged over the days it was
        measured, and `vo2_max_readings` says how many that is. Use it to compare stretches of time or
        spot long-term change, such as whether VO2 max or HRV has improved; any range length works,
        years included."""
        start, end = date_range(start_date, end_date, None)
        return {**span(start, end), **await health.trends(oura, start, end, period)}

    @tool
    async def get_sleep_sessions(
        start_date: DateArg = None,
        end_date: DateArg = None,
        include_naps: Annotated[bool, Field(description="Include naps and short rests.")] = True,
    ) -> dict[str, Any]:
        """Each sleep session in the range with bedtime, wake time, total/deep/REM/light/awake hours,
        efficiency, latency, restlessness, heart rate, HRV and breathing rate. Defaults to the last
        7 days; at most 400 days per call."""
        start, end = date_range(start_date, end_date, MAX_RANGE_DAYS)
        return {**span(start, end), **await health.sleep_sessions(oura, start, end, include_naps)}

    @tool
    async def get_heart_rate(
        start: Annotated[str, Field(description="Local time, YYYY-MM-DD or YYYY-MM-DDTHH:MM.")],
        end: Annotated[
            str | None, Field(description="Local time, exclusive. Defaults to start + 1 day.")
        ] = None,
        bucket_minutes: Annotated[int, Field(ge=1, le=1440, description="Bucket size in minutes.")] = 60,
        source: Annotated[
            str | None,
            Field(description="Only readings of this kind: awake, rest, sleep, session or workout."),
        ] = None,
    ) -> dict[str, Any]:
        """Heart rate (average, minimum, maximum, number of readings) in time buckets between two local
        times. At most 30 days per call, the Oura API's limit; use 1440-minute buckets for daily figures."""
        start_dt = _datetime(start, "start", oura.timezone)
        end_dt = _datetime(end, "end", oura.timezone) if end else start_dt + timedelta(days=1)
        if end_dt <= start_dt:
            raise ValueError(f"end {end_dt} must be after start {start_dt}.")
        if _utc(end_dt, oura.timezone) - _utc(start_dt, oura.timezone) > timedelta(days=MAX_HEART_RATE_DAYS):
            raise ValueError(f"Heart rate takes at most {MAX_HEART_RATE_DAYS} days per call.")
        if (end_dt - start_dt) / timedelta(minutes=bucket_minutes) > MAX_HEART_RATE_BUCKETS:
            raise ValueError(
                f"That would be more than {MAX_HEART_RATE_BUCKETS} buckets; use a larger bucket_minutes."
            )
        return await health.heart_rate(oura, start_dt, end_dt, bucket_minutes, source)

    @tool
    async def get_workouts(start_date: DateArg = None, end_date: DateArg = None) -> dict[str, Any]:
        """Workouts in the range, logged or auto-detected: activity, intensity, start and end, duration,
        calories and distance. Defaults to the last 7 days."""
        start, end = date_range(start_date, end_date, None)
        return {**span(start, end), **await health.workouts(oura, start, end)}

    @tool
    async def get_oura_data(
        endpoint: Annotated[
            str,
            Field(
                description=(
                    "One of: personal_info, ring_configuration, daily_sleep, sleep, sleep_time, "
                    "daily_readiness, daily_activity, daily_stress, daily_resilience, daily_spo2, "
                    "daily_cardiovascular_age, vO2_max, workout, session, tag, enhanced_tag, "
                    "rest_mode_period."
                )
            ),
        ],
        start_date: DateArg = None,
        end_date: DateArg = None,
        include_time_series: Annotated[
            bool,
            Field(
                description=(
                    "Keep the per-5-minute and per-30-second arrays (heart rate, HRV, MET, sleep "
                    "phases). Large."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Raw records from any Oura API v2 collection, exactly as Oura returns them (durations in
        seconds, times with UTC offsets), for anything the other tools leave out: tags, meditation
        sessions, rest mode, bedtime recommendations, ring details, contributor breakdowns. Dated
        endpoints default to the last 7 days. Returns at most 500 records."""
        start = end = None
        if endpoint in health.DATED_ENDPOINTS:
            start, end = date_range(start_date, end_date, MAX_RANGE_DAYS)
        return await health.oura_data(oura, endpoint, start, end, include_time_series)

    return server


def _system_timezone() -> str | None:
    try:
        return tzlocal.get_localzone_name()
    except LookupError:  # ZoneInfoNotFoundError: the computer's zone is missing or unreadable
        return None


def resolve_timezone() -> str:
    """OURA_TIMEZONE if set, else the computer's own timezone, else UTC. Oura's daily records carry no
    timezone, so this decides what "today" means and how heart-rate times are shown."""
    timezone = os.environ.get("OURA_TIMEZONE") or _system_timezone() or "UTC"
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"{timezone!r} is not an IANA timezone name, such as Europe/London.") from None
    return timezone


def token_source() -> TokenSource:
    """OURA_ACCESS_TOKEN if set, else the credentials saved by `mcp-oura login`."""
    if token := os.environ.get("OURA_ACCESS_TOKEN"):
        return StaticToken(token)
    path = auth.credentials_path()
    if path.exists():
        return auth.OAuthCredentials(path)
    raise OuraAPIError(
        f"not signed in: there are no saved credentials at {auth.display(path)}. "
        "Run `mcp-oura login` (see the README), or set OURA_ACCESS_TOKEN to a legacy personal access token."
    )


def main() -> None:
    # httpx logs every request at INFO; Claude's MCP log only needs failures.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        tokens = token_source()
    except OuraAPIError as exc:
        sys.exit(f"mcp-oura: {exc}")
    try:
        timezone = resolve_timezone()
    except ValueError as exc:
        sys.exit(f"mcp-oura: OURA_TIMEZONE: {exc}")
    oura = OuraClient(tokens, timezone=timezone)
    create_server(oura).run()
