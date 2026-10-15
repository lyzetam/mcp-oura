from datetime import date

import httpx
import pytest

from fake_oura import FakeOura
from mcp_oura.client import OuraAPIError, OuraClient

D = date.fromisoformat


async def test_days_includes_the_end_date_on_every_endpoint(oura):
    # The real API drops the end date on sleep, daily_activity and workout
    # but keeps it on the daily scores; the client evens that out.
    for endpoint in ("sleep", "daily_activity", "daily_sleep", "daily_readiness"):
        docs = await oura.days(endpoint, D("2026-10-12"), D("2026-10-12"))
        assert docs and {d["day"] for d in docs} == {"2026-10-12"}, endpoint


async def test_days_follows_every_page():
    fake = FakeOura(page_size=2)
    client = OuraClient("test-token", timezone="America/New_York", transport=fake.transport(), retry_delay=0)
    docs = await client.days("sleep", D("2026-10-01"), D("2026-10-31"))
    await client.aclose()

    assert [d["id"] for d in docs] == ["p10", "p11", "p12", "p12nap", "p12del"]
    assert len(fake.requests) == 3


async def test_document_endpoint(oura):
    info = await oura.document("personal_info")
    assert info["age"] == 38


async def test_a_rejected_token_names_where_to_fix_it(fake):
    client = OuraClient("wrong", timezone="America/New_York", transport=fake.transport(), retry_delay=0)
    with pytest.raises(OuraAPIError, match="OURA_ACCESS_TOKEN"):
        await client.days("daily_sleep", D("2026-10-12"), D("2026-10-12"))
    await client.aclose()


async def test_rate_limits_and_server_errors_are_retried(oura, fake):
    fake.fail_next = [429, 502]
    docs = await oura.days("daily_sleep", D("2026-10-12"), D("2026-10-12"))
    assert docs[0]["score"] == 70


async def test_persistent_server_errors_fail_loudly(oura, fake):
    fake.fail_next = [500, 500, 500]
    with pytest.raises(OuraAPIError, match="after 3 attempts"):
        await oura.days("daily_sleep", D("2026-10-12"), D("2026-10-12"))


async def test_a_bad_request_passes_oura_detail_through(oura):
    with pytest.raises(OuraAPIError, match="less than or equal to 30 days"):
        await oura.collection(
            "heartrate", {"start_datetime": "2026-08-01T00:00:00Z", "end_datetime": "2026-10-01T00:00:00Z"}
        )


async def test_network_failures_are_retried_then_reported():
    calls = []

    def unreachable(request):
        calls.append(request)
        raise httpx.ConnectError("no route to host")

    client = OuraClient(
        "t", timezone="America/New_York", transport=httpx.MockTransport(unreachable), retry_delay=0
    )
    with pytest.raises(OuraAPIError, match="Couldn't reach the Oura API"):
        await client.document("personal_info")
    assert len(calls) == 3
    await client.aclose()


def test_fake_matches_the_real_end_date_rules():
    """Guards the fake itself: if this drifts, the end-date test above proves nothing."""
    fake = FakeOura()
    sleep = fake.handle(
        httpx.Request(
            "GET",
            "https://x/v2/usercollection/sleep?start_date=2026-10-12&end_date=2026-10-12",
            headers={"authorization": "Bearer test-token"},
        )
    )
    scores = fake.handle(
        httpx.Request(
            "GET",
            "https://x/v2/usercollection/daily_sleep?start_date=2026-10-12&end_date=2026-10-12",
            headers={"authorization": "Bearer test-token"},
        )
    )
    assert sleep.json()["data"] == []
    assert len(scores.json()["data"]) == 1


async def test_an_error_body_that_is_not_an_object_still_reports_the_status():
    transport = httpx.MockTransport(lambda request: httpx.Response(403, json=["Membership required"]))
    client = OuraClient("t", timezone="America/New_York", transport=transport, retry_delay=0)
    with pytest.raises(OuraAPIError, match="HTTP 403"):
        await client.document("personal_info")
    await client.aclose()
