"""Test fixtures for caldav."""

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import Mock, patch

from aiohttp import ClientWebSocketResponse
from caldav.lib.url import URL
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import WebSocketGenerator

from homeassistant.components.caldav.const import DOMAIN
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant

TEST_URL = "https://example.com/url-1"
TEST_USERNAME = "username-1"
TEST_PASSWORD = "password-1"


@pytest.fixture(name="platforms")
def mock_platforms() -> list[Platform]:
    """Fixture to specify platforms to test."""
    return []


@pytest.fixture(autouse=True)
async def mock_patch_platforms(platforms: list[str]) -> None:
    """Fixture to set up the integration."""
    with patch(f"homeassistant.components.{DOMAIN}.PLATFORMS", platforms):
        yield


@pytest.fixture(name="calendars")
def mock_calendars() -> list[Mock]:
    """Fixture to provide calendars returned by CalDAV client."""
    return []


@pytest.fixture(name="dav_client", autouse=True)
def mock_dav_client(calendars: list[Mock]) -> Mock:
    """Fixture to mock the DAVClient."""
    with (
        patch("homeassistant.components.caldav.DAVClient") as mock_client,
        patch("homeassistant.components.caldav.calendar.DAVClient", mock_client),
        patch("homeassistant.components.caldav.config_flow.DAVClient", mock_client),
    ):
        mock_client.url = URL(TEST_URL)
        mock_client.return_value.url = URL(TEST_URL)
        mock_client.return_value.get_principal.return_value.get_calendars.return_value = calendars
        yield mock_client


@pytest.fixture(name="config_entry")
def mock_config_entry() -> MockConfigEntry:
    """Fixture for a config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_URL: TEST_URL,
            CONF_USERNAME: TEST_USERNAME,
            CONF_PASSWORD: TEST_PASSWORD,
            CONF_VERIFY_SSL: True,
        },
    )


class Client:
    """Test client with helper methods for calendar websocket."""

    def __init__(self, client: ClientWebSocketResponse) -> None:
        """Initialize Client."""
        self.client = client
        self.id = 0

    async def cmd(self, cmd: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a command and receive the json result."""
        self.id += 1
        await self.client.send_json(
            {"id": self.id, "type": f"calendar/event/{cmd}", **payload}
        )
        resp = await self.client.receive_json()
        assert resp.get("id") == self.id
        return resp


type ClientFixture = Callable[[], Awaitable[Client]]


@pytest.fixture
async def ws_client(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> ClientFixture:
    """Fixture for creating the test websocket client."""

    async def create_client() -> Client:
        return Client(await hass_ws_client(hass))

    return create_client
