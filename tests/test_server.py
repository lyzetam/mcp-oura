"""The MCP layer: tool inventory, annotations, argument parsing, and a real
round trip through the SDK's in-memory client against the fake Oura API."""

from datetime import date
from zoneinfo import ZoneInfoNotFoundError

import pytest
from mcp.client import Client

from mcp_oura import auth
from mcp_oura.client import StaticToken
from mcp_oura.server import create_server, main, resolve_timezone, token_source

EXPECTED_TOOLS = {
    "get_daily_summary",
    "get_daily_metrics",
    "get_trends",
    "get_sleep_sessions",
    "get_heart_rate",
    "get_workouts",
    "get_oura_data",
}


@pytest.fixture
def server(oura):
    return create_server(oura, today=lambda: date(2026, 10, 13))


async def test_every_tool_is_registered_and_marked_read_only(server):
    async with Client(server) as client:
        tools = (await client.list_tools()).tools

    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)


async def test_a_tool_call_round_trips_through_the_sdk(server):
    async with Client(server) as client:
        result = await client.call_tool("get_daily_summary", {"day": "2026-10-12"})

    assert not result.is_error
    assert result.structured_content["readiness"]["score"] == 75


async def test_date_ranges_default_to_the_last_seven_days(server):
    async with Client(server) as client:
        result = await client.call_tool("get_daily_metrics", {})

    assert not result.is_error
    assert result.structured_content["start_date"] == "2026-10-07"
    assert result.structured_content["end_date"] == "2026-10-13"
    assert len(result.structured_content["days"]) == 3


async def test_a_badly_formatted_date_is_explained(server):
    async with Client(server) as client:
        result = await client.call_tool("get_daily_metrics", {"start_date": "10/12/2026"})

    assert result.is_error
    assert "YYYY-MM-DD" in result.content[0].text


async def test_a_start_after_the_end_is_refused(server):
    async with Client(server) as client:
        result = await client.call_tool(
            "get_workouts", {"start_date": "2026-10-12", "end_date": "2026-10-01"}
        )

    assert result.is_error
    assert "after" in result.content[0].text


async def test_heart_rate_ranges_are_capped(server):
    async with Client(server) as client:
        result = await client.call_tool("get_heart_rate", {"start": "2026-08-01", "end": "2026-10-12"})

    assert result.is_error
    assert "30 days" in result.content[0].text


async def test_heart_rate_accepts_dates_and_datetimes(server):
    async with Client(server) as client:
        result = await client.call_tool(
            "get_heart_rate", {"start": "2026-10-11T23:00", "end": "2026-10-12", "bucket_minutes": 30}
        )

    assert not result.is_error
    assert result.structured_content["readings"] == 12


async def test_an_oura_failure_comes_back_as_a_readable_tool_error(server, fake):
    fake.fail_next = [500] * 100  # the summary fans out ~10 requests at once; fail all of them
    async with Client(server) as client:
        result = await client.call_tool("get_daily_summary", {"day": "2026-10-12"})

    assert result.is_error
    assert "Oura API" in result.content[0].text


async def test_the_heart_rate_limit_is_counted_in_utc_like_oura_does(server, fake):
    # 30 local days across the November clock change are 30 days and 1 hour in UTC.
    async with Client(server) as client:
        result = await client.call_tool("get_heart_rate", {"start": "2026-10-15", "end": "2026-11-14"})

    assert result.is_error
    assert "30 days" in result.content[0].text
    assert fake.requests == []


def test_an_unknown_timezone_stops_the_server_with_a_reason(monkeypatch):
    monkeypatch.setenv("OURA_ACCESS_TOKEN", "t")
    monkeypatch.setenv("OURA_TIMEZONE", "Eastern")
    with pytest.raises(SystemExit, match="OURA_TIMEZONE"):
        main()


def test_without_a_token_or_sign_in_the_server_says_how_to_get_one(monkeypatch, tmp_path):
    monkeypatch.delenv("OURA_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("OURA_CREDENTIALS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(SystemExit, match="mcp-oura login") as stopped:
        main()
    assert "OURA_ACCESS_TOKEN" in str(stopped.value)
    assert "~/missing.json" in str(stopped.value)


def test_a_personal_access_token_wins_over_a_saved_sign_in(monkeypatch, tmp_path):
    monkeypatch.setenv("OURA_ACCESS_TOKEN", "pat")
    monkeypatch.setenv("OURA_CREDENTIALS_FILE", str(tmp_path / "missing.json"))
    assert isinstance(token_source(), StaticToken)


def test_a_saved_sign_in_is_used_when_no_token_is_set(monkeypatch, tmp_path):
    path = tmp_path / "credentials.json"
    auth.save_credentials(
        path,
        {"client_id": "c", "client_secret": "s", "access_token": "a", "refresh_token": "r", "expires_at": 0},
    )
    monkeypatch.delenv("OURA_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("OURA_CREDENTIALS_FILE", str(path))
    assert isinstance(token_source(), auth.OAuthCredentials)


def test_the_timezone_comes_from_oura_timezone_when_set(monkeypatch):
    monkeypatch.setenv("OURA_TIMEZONE", "Europe/Helsinki")
    assert resolve_timezone() == "Europe/Helsinki"


def test_the_timezone_defaults_to_the_computers_own(monkeypatch):
    monkeypatch.delenv("OURA_TIMEZONE", raising=False)
    monkeypatch.setattr("tzlocal.get_localzone_name", lambda: "Asia/Tokyo")
    assert resolve_timezone() == "Asia/Tokyo"


def test_the_timezone_falls_back_to_utc_when_the_computer_has_none(monkeypatch):
    monkeypatch.delenv("OURA_TIMEZONE", raising=False)
    monkeypatch.setattr("tzlocal.get_localzone_name", lambda: None)
    assert resolve_timezone() == "UTC"


def test_the_timezone_falls_back_to_utc_when_the_lookup_fails(monkeypatch):
    def broken():
        raise ZoneInfoNotFoundError("no zone configured")

    monkeypatch.delenv("OURA_TIMEZONE", raising=False)
    monkeypatch.setattr("tzlocal.get_localzone_name", broken)
    assert resolve_timezone() == "UTC"
