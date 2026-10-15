import webbrowser

import pytest

from fake_oura import TOKEN, FakeOura
from mcp_oura.client import OuraClient


@pytest.fixture
def fake():
    # A small page size so every multi-record call exercises pagination.
    return FakeOura(page_size=5)


@pytest.fixture
async def oura(fake):
    client = OuraClient(TOKEN, timezone="America/New_York", transport=fake.transport(), retry_delay=0)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    """A test that reaches the real browser fails instead of opening a tab on the developer's screen."""

    def refuse(url, *args, **kwargs):
        raise AssertionError(f"a test tried to open a real browser at {url}")

    monkeypatch.setattr(webbrowser, "open", refuse)
