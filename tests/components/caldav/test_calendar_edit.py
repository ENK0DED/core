"""Tests for changing and deleting CalDAV events over the websocket API."""

from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID

from caldav.calendarobjectresource import Event
from caldav.elements import cdav, dav
from caldav.lib.error import (
    DAVError,
    ETagMismatchError,
    NotFoundError,
    PutError,
    ScheduleTagMismatchError,
)
from caldav.lib.http_sync import requests as caldav_requests
from caldav.lib.url import URL
import pytest
from pytest_homeassistant.common import MockConfigEntry
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.calendar import CalendarEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES, Platform
from homeassistant.core import HomeAssistant

from .conftest import ClientFixture

pytestmark = pytest.mark.freeze_time("2026-11-09 12:00:00+00:00")

TEST_ENTITY = "calendar.example"

VTIMEZONE = """BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:STANDARD
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
END:DAYLIGHT
END:VTIMEZONE
"""

DAILY = """BEGIN:VEVENT
UID:daily
DTSTAMP:20261101T000000Z
DTSTART;TZID=Europe/Berlin:20261110T100000
DTEND;TZID=Europe/Berlin:20261110T110000
{rule}
SUMMARY:Daily
CATEGORIES:Work
ATTENDEE;CN=Bob;X-NUM-GUESTS=0:mailto:bob@example.com
X-CUSTOM:Kept
SEQUENCE:1
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER;RELATED=END:-PT15M
DESCRIPTION:Reminder
END:VALARM
END:VEVENT
"""

OVERRIDE = """BEGIN:VEVENT
UID:daily
DTSTAMP:20261101T000000Z
RECURRENCE-ID;TZID=Europe/Berlin:202611{day:02d}T100000
DTSTART;TZID=Europe/Berlin:202611{day:02d}T{start}00
DTEND;TZID=Europe/Berlin:202611{day:02d}T{end}00
SUMMARY:{summary}
END:VEVENT
"""

ALL_DAY = """BEGIN:VEVENT
UID:bins
DTSTAMP:20261101T000000Z
DTSTART;VALUE=DATE:20261102
DTEND;VALUE=DATE:20261103
RRULE:FREQ=WEEKLY;UNTIL=20261130
SUMMARY:Bins
END:VEVENT
"""

FLOATING = """BEGIN:VEVENT
UID:float
DTSTAMP:20261101T000000Z
DTSTART:20261110T080000
DURATION:PT30M
RRULE:FREQ=DAILY
SUMMARY:Float
END:VEVENT
"""

UTC_SERIES = """BEGIN:VEVENT
UID:utc
DTSTAMP:20261101T000000Z
DTSTART:20261110T090000Z
DTEND:20261110T100000Z
RRULE:FREQ=DAILY;COUNT=5
SUMMARY:UTC
END:VEVENT
"""

RDATE_ONLY = """BEGIN:VEVENT
UID:dates
DTSTAMP:20261101T000000Z
DTSTART;TZID=Europe/Berlin:20261110T100000
DTEND;TZID=Europe/Berlin:20261110T110000
RDATE;TZID=Europe/Berlin:20261112T100000,20261115T100000
RDATE;VALUE=PERIOD:20261120T090000Z/PT2H,20261122T090000Z/20261122T110000Z
SUMMARY:Dates
END:VEVENT
"""

ORPHANS = """BEGIN:VEVENT
UID:orphans
DTSTAMP:20261101T000000Z
RECURRENCE-ID:20261110T090000Z
DTSTART:20261110T090000Z
DTEND:20261110T100000Z
SUMMARY:First
END:VEVENT
BEGIN:VEVENT
UID:orphans
DTSTAMP:20261101T000000Z
RECURRENCE-ID:20261117T090000Z
DTSTART:20261117T090000Z
DTEND:20261117T100000Z
SUMMARY:Second
END:VEVENT
"""

SINGLE = """BEGIN:VEVENT
UID:single
DTSTAMP:20261101T000000Z
DTSTART{start}
{end}
SUMMARY:Single
DESCRIPTION:Old description
LOCATION:Office
X-CUSTOM;X-PARAM=1:Kept
END:VEVENT
"""

WEEKLY_MONDAY = """BEGIN:VEVENT
UID:weekly
DTSTAMP:20261101T000000Z
DTSTART;TZID=Europe/Berlin:20261102T100000
DTEND;TZID=Europe/Berlin:20261102T110000
RRULE:FREQ=WEEKLY;BYDAY=MO
RDATE;TZID=Europe/Berlin:20261111T100000
SUMMARY:Weekly
END:VEVENT
"""

NEW_YORK = """BEGIN:VEVENT
UID:new-york
DTSTAMP:20261101T000000Z
DTSTART;TZID=America/New_York:20261030T090000
DTEND;TZID=America/New_York:20261030T100000
SUMMARY:New York
END:VEVENT
"""


def _ics(*components: str, timezone: bool = True) -> str:
    """Return a CalDAV object holding components."""
    return "".join(
        (
            "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Example//Test//EN\n",
            VTIMEZONE if timezone else "",
            *components,
            "END:VCALENDAR\n",
        )
    )


def _daily(rule: str = "RRULE:FREQ=DAILY;COUNT=5", *overrides: str) -> str:
    """Return a daily event at 10:00 from Nov 10, with overrides."""
    return _ics(DAILY.format(rule=rule), *overrides)


def _override(
    day: int, start: str = "1000", end: str = "1100", summary: str = "Daily"
) -> str:
    """Return an override of the daily event."""
    return OVERRIDE.format(day=day, start=start, end=end, summary=summary)


def _single(start: str, end: str = "", *, timezone: bool = True) -> str:
    """Return an event that doesn't repeat."""
    return _ics(
        SINGLE.format(start=start, end=end).replace("\n\n", "\n"), timezone=timezone
    )


@pytest.fixture(name="ics")
def mock_ics() -> str:
    """Fixture for the data of the CalDAV object."""
    return _daily()


@pytest.fixture(name="etag")
def mock_etag() -> str | None:
    """Fixture for the ETag of the CalDAV object."""
    return '"etag-1"'


@pytest.fixture(name="item")
def mock_item(ics: str, etag: str | None) -> Event:
    """Fixture for the CalDAV object of the event."""
    item = Event(None, URL("http://test.local/calendar/event.ics"), ics)
    if etag is not None:
        item.props[dav.GetEtag.tag] = etag
    item.save = MagicMock()  # type: ignore[method-assign]
    item.delete = MagicMock()  # type: ignore[method-assign]
    return item


@pytest.fixture(name="new_item")
def mock_new_item() -> Mock:
    """Fixture for the CalDAV object created for split off occurrences."""
    return Mock(url="http://test.local/calendar/new.ics")


@pytest.fixture(name="calendars")
def mock_calendars(item: Event, new_item: Mock) -> list[Mock]:
    """Fixture for the CalDAV calendar holding the event."""
    calendar = Mock()
    calendar.url = URL("http://test.local/calendar/")
    calendar.get_display_name = MagicMock(return_value="Example")
    calendar.get_supported_components = MagicMock(return_value=["VEVENT"])
    calendar.search = MagicMock(return_value=[])
    calendar.get_property = MagicMock(return_value=None)
    calendar.get_event_by_uid = MagicMock(return_value=item)
    calendar.add_event = MagicMock(return_value=new_item)
    return [calendar]


@pytest.fixture(name="platforms")
def mock_platforms() -> list[Platform]:
    """Fixture to specify platforms to test."""
    return [Platform.CALENDAR]


@pytest.fixture(autouse=True)
async def setup_calendar(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Set up the CalDAV calendar."""
    await hass.config.async_set_time_zone("Europe/Berlin")
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture(autouse=True)
def mock_uuid() -> Generator[None]:
    """Fixture for the UID of split off occurrences."""
    with patch(
        "homeassistant.components.caldav.event_edit.uuid4",
        return_value=UUID("12345678-1234-5678-1234-567812345678"),
    ):
        yield


async def _send(
    ws_client: ClientFixture,
    cmd: str,
    uid: str,
    recurrence_id: str | None = None,
    recurrence_range: str | None = None,
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send an update or delete command."""
    payload: dict[str, Any] = {"entity_id": TEST_ENTITY, "uid": uid}
    if recurrence_id is not None:
        payload["recurrence_id"] = recurrence_id
    if recurrence_range is not None:
        payload["recurrence_range"] = recurrence_range
    if event is not None:
        payload["event"] = event
    client = await ws_client()
    return await client.cmd(cmd, payload)


def _assert_saved(item: Event) -> None:
    """Assert that the object was saved as it is, with its ETag."""
    item.save.assert_called_once_with(
        increase_seqno=False, only_this_recurrence=False, precondition="etag"
    )
    item.delete.assert_not_called()


def _new_object(calendar: Mock) -> str:
    """Return the object created for split off occurrences."""
    calendar.add_event.assert_called_once()
    assert calendar.add_event.call_args.kwargs["increase_seqno"] is False
    return calendar.add_event.call_args.kwargs["ical"].to_ical().decode()


async def test_supported_features(hass: HomeAssistant) -> None:
    """Test that a calendar supports changing and deleting events."""
    state = hass.states.get(TEST_ENTITY)
    assert state
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == (
        CalendarEntityFeature.CREATE_EVENT
        | CalendarEntityFeature.UPDATE_EVENT
        | CalendarEntityFeature.DELETE_EVENT
    )


@pytest.mark.parametrize(
    ("etag", "props", "if_match"),
    [
        pytest.param('"etag-1"', {}, True, id="etag"),
        pytest.param('W/"etag-1"', {}, False, id="weak_etag"),
        pytest.param(None, {}, False, id="no_etag"),
        pytest.param(None, {cdav.ScheduleTag.tag: '"tag-1"'}, True, id="schedule_tag"),
    ],
)
async def test_delete_event(
    ws_client: ClientFixture,
    calendars: list[Mock],
    item: Event,
    props: dict[str, str],
    if_match: bool,
) -> None:
    """Test deleting a whole event, only if it wasn't changed since it was read."""
    item.props.update(props)
    search_calls = calendars[0].search.call_count

    resp = await _send(ws_client, "delete", "daily")

    assert resp["success"]
    calendars[0].get_event_by_uid.assert_called_once_with("daily")
    item.delete.assert_called_once_with(if_match=if_match, precondition="etag")
    item.save.assert_not_called()
    # The coordinator refreshed the next event
    assert calendars[0].search.call_count > search_calls


@pytest.mark.parametrize(
    ("ics", "uid", "recurrence_id", "recurrence_range"),
    [
        pytest.param(
            _daily(), "daily", "2026-11-12 10:00:00+01:00", "", id="one_occurrence"
        ),
        pytest.param(
            _daily(),
            "daily",
            "2026-11-13 10:00:00+01:00",
            "THISANDFUTURE",
            id="this_and_future",
        ),
        pytest.param(_daily(), "daily", "20261112T090000Z", None, id="rfc5545_id"),
        pytest.param(_daily(), "daily", "2026-11-12 10:00:00", None, id="floating_id"),
        pytest.param(
            _daily("RRULE:FREQ=DAILY;COUNT=5", _override(12, "1500", "1600", "Moved")),
            "daily",
            "2026-11-12 10:00:00+01:00",
            "",
            id="overridden_occurrence",
        ),
        pytest.param(
            _daily(
                "RRULE:FREQ=DAILY;COUNT=5\n"
                "EXDATE;TZID=Europe/Berlin:20261111T100000,20261113T100000\n"
                "EXDATE:20261114T090000Z\n"
                "RDATE;TZID=Europe/Berlin:20261120T100000",
                _override(12, "1500", "1600", "Moved"),
                _override(14, summary="Special"),
            ),
            "daily",
            "2026-11-12 10:00:00+01:00",
            "THISANDFUTURE",
            id="this_and_future_dates_and_overrides",
        ),
        pytest.param(
            _daily(
                "RRULE:FREQ=DAILY;COUNT=3\nRDATE;TZID=Europe/Berlin:20261120T100000"
            ),
            "daily",
            "2026-11-20 10:00:00+01:00",
            "THISANDFUTURE",
            id="this_and_future_after_rule",
        ),
        pytest.param(
            _ics(ALL_DAY), "bins", "2026-11-16", "", id="all_day_one_occurrence"
        ),
        pytest.param(
            _ics(ALL_DAY),
            "bins",
            "2026-11-16",
            "THISANDFUTURE",
            id="all_day_this_and_future",
        ),
        pytest.param(
            _ics(ALL_DAY),
            "bins",
            "2026-11-16 00:00:00+01:00",
            "",
            id="all_day_timed_id",
        ),
        pytest.param(
            _ics(FLOATING),
            "float",
            "2026-11-11 08:00:00",
            "",
            id="floating_one_occurrence",
        ),
        pytest.param(
            _ics(FLOATING),
            "float",
            "2026-11-11 08:00:00",
            "THISANDFUTURE",
            id="floating_this_and_future",
        ),
        pytest.param(
            _ics(UTC_SERIES),
            "utc",
            "2026-11-12 09:00:00+00:00",
            "THISANDFUTURE",
            id="utc_this_and_future",
        ),
        pytest.param(
            _ics(RDATE_ONLY),
            "dates",
            "2026-11-12 10:00:00+01:00",
            "",
            id="added_dates_one_occurrence",
        ),
        pytest.param(
            _ics(RDATE_ONLY),
            "dates",
            "2026-11-15 10:00:00+01:00",
            "THISANDFUTURE",
            id="added_dates_this_and_future",
        ),
        pytest.param(
            _ics(ORPHANS),
            "orphans",
            "2026-11-17 09:00:00+00:00",
            "",
            id="occurrence_without_series",
        ),
        pytest.param(
            _ics(ORPHANS),
            "orphans",
            "2026-11-17 09:00:00+00:00",
            "THISANDFUTURE",
            id="occurrences_without_series",
        ),
        pytest.param(
            _daily(
                "RRULE:FREQ=DAILY;COUNT=5\nEXDATE;TZID=Europe/Berlin:20261112T100000",
                _override(12, "1500", "1600", "Moved"),
            ),
            "daily",
            "2026-11-12 10:00:00+01:00",
            "",
            id="excluded_overridden_occurrence",
        ),
        pytest.param(
            _daily("RRULE:FREQ=DAILY;COUNT=5", _override(5, summary="Before")),
            "daily",
            "2026-11-10 10:00:00+01:00",
            "THISANDFUTURE",
            id="this_and_future_keeps_earlier_override",
        ),
        pytest.param(
            _daily("RRULE:FREQ=DAILY;COUNT=6\nEXDATE;VALUE=DATE:20261111"),
            "daily",
            "2026-11-14 10:00:00+01:00",
            "THISANDFUTURE",
            id="this_and_future_keeps_date_exclusion",
        ),
        pytest.param(
            _ics(
                DAILY.format(rule="RRULE:FREQ=DAILY;COUNT=5")
                .replace("20261110T100000", "20270326T023000")
                .replace("20261110T110000", "20270326T033000")
            ),
            "daily",
            "2027-03-28 02:30:00+02:00",
            "",
            id="skipped_hour",
        ),
        pytest.param(
            _ics(
                DAILY.format(rule="RRULE:FREQ=DAILY;COUNT=20").replace(
                    "20261110T", "20261020T"
                )
            ),
            "daily",
            "2026-11-02 10:00:00+01:00",
            "THISANDFUTURE",
            id="this_and_future_after_dst_change",
        ),
    ],
)
async def test_delete_occurrences(
    ws_client: ClientFixture,
    item: Event,
    uid: str,
    recurrence_id: str,
    recurrence_range: str | None,
    snapshot: SnapshotAssertion,
) -> None:
    """Test deleting occurrences, which is saved in the form of the event."""
    resp = await _send(ws_client, "delete", uid, recurrence_id, recurrence_range)

    assert resp["success"]
    _assert_saved(item)
    assert item.data == snapshot


@pytest.mark.parametrize(
    ("ics", "uid", "recurrence_id", "recurrence_range"),
    [
        pytest.param(
            _daily(),
            "daily",
            "2026-11-10 10:00:00+01:00",
            "THISANDFUTURE",
            id="from_first_occurrence",
        ),
        pytest.param(
            _daily("RRULE:FREQ=DAILY;COUNT=1"),
            "daily",
            "2026-11-10 10:00:00+01:00",
            "",
            id="only_occurrence",
        ),
        pytest.param(
            _daily(
                "RRULE:FREQ=DAILY;COUNT=2\nEXDATE;TZID=Europe/Berlin:20261110T100000"
            ),
            "daily",
            "2026-11-11 10:00:00+01:00",
            "",
            id="last_occurrence",
        ),
        pytest.param(
            _daily(
                "RRULE:FREQ=DAILY;COUNT=2\nEXDATE;TZID=Europe/Berlin:20261110T100000"
            ),
            "daily",
            "2026-11-11 10:00:00+01:00",
            "THISANDFUTURE",
            id="from_first_shown_occurrence",
        ),
        pytest.param(
            _ics(ORPHANS),
            "orphans",
            "2026-11-10 09:00:00+00:00",
            "THISANDFUTURE",
            id="all_occurrences_without_series",
        ),
        pytest.param(
            _single(";TZID=Europe/Berlin:20261110T100000"),
            "single",
            "2026-11-10 10:00:00+01:00",
            "",
            id="single_event",
        ),
    ],
)
async def test_delete_last_occurrences(
    ws_client: ClientFixture,
    item: Event,
    uid: str,
    recurrence_id: str,
    recurrence_range: str,
) -> None:
    """Test that deleting all remaining occurrences deletes the event."""
    resp = await _send(ws_client, "delete", uid, recurrence_id, recurrence_range)

    assert resp["success"]
    item.delete.assert_called_once_with(if_match=True, precondition="etag")
    item.save.assert_not_called()


@pytest.mark.parametrize(
    ("ics", "uid", "recurrence_id", "message"),
    [
        pytest.param(
            _daily(),
            "daily",
            "2026-11-20 10:00:00+01:00",
            "The occurrence was not found in the event",
            id="not_an_occurrence",
        ),
        pytest.param(
            _daily(),
            "daily",
            "2026-11-12",
            "The occurrence was not found in the event",
            id="date_of_timed_event",
        ),
        pytest.param(
            _daily(),
            "daily",
            "next tuesday",
            "The occurrence was not found in the event",
            id="not_a_time",
        ),
        pytest.param(
            _daily(),
            "other",
            "2026-11-12 10:00:00+01:00",
            "The event was not found on the CalDAV server",
            id="other_uid",
        ),
        pytest.param(
            _daily(
                "RRULE:FREQ=DAILY;COUNT=5",
                _override(12).replace(
                    "RECURRENCE-ID;", "RECURRENCE-ID;RANGE=THISANDFUTURE;"
                ),
            ),
            "daily",
            "2026-11-12 10:00:00+01:00",
            "The event uses iCalendar features that Home Assistant can't change",
            id="range_override",
        ),
        pytest.param(
            _daily("RRULE:FREQ=DAILY;COUNT=5\nEXRULE:FREQ=WEEKLY"),
            "daily",
            "2026-11-12 10:00:00+01:00",
            "The event uses iCalendar features that Home Assistant can't change",
            id="exrule",
        ),
        pytest.param(
            _daily("RRULE:FREQ=DAILY;SKIP=FORWARD"),
            "daily",
            "2026-11-12 10:00:00+01:00",
            "The event uses iCalendar features that Home Assistant can't change",
            id="unknown_rule_part",
        ),
        pytest.param(
            _daily("RRULE:FREQ=SECONDLY"),
            "daily",
            "2026-11-12 10:00:00+01:00",
            "The event repeats too often to be changed",
            id="dense_rule",
        ),
    ],
)
async def test_delete_errors(
    ws_client: ClientFixture,
    item: Event,
    uid: str,
    recurrence_id: str,
    message: str,
) -> None:
    """Test that occurrences that can't be deleted raise and change nothing."""
    resp = await _send(ws_client, "delete", uid, recurrence_id, "")

    assert resp["error"] == {"code": "failed", "message": message}
    item.save.assert_not_called()
    item.delete.assert_not_called()


@pytest.mark.parametrize(
    ("ics", "recurrence_id", "event"),
    [
        pytest.param(
            _single(
                ";TZID=Europe/Berlin:20261110T100000",
                "DTEND;TZID=Europe/Berlin:20261110T110000",
            ),
            None,
            {
                "summary": "Changed",
                "dtstart": "2026-11-10T12:00:00",
                "dtend": "2026-11-10T13:30:00",
                "description": "",
            },
            id="zoned",
        ),
        pytest.param(
            _single(
                ";TZID=America/New_York:20261110T100000",
                "DTEND;TZID=America/New_York:20261110T110000",
            ),
            None,
            {
                "summary": "Single",
                "dtstart": "2026-11-10T17:00:00",
                "dtend": "2026-11-10T18:00:00",
            },
            id="other_zone",
        ),
        pytest.param(
            _single(":20261110T090000Z", "DTEND:20261110T100000Z"),
            None,
            {
                "summary": "Single",
                "dtstart": "2026-11-10T12:00:00",
                "dtend": "2026-11-10T13:00:00",
            },
            id="utc",
        ),
        pytest.param(
            _single(":20261110T100000", "DTEND:20261110T110000"),
            None,
            {
                "summary": "Single",
                "dtstart": "2026-11-10T12:00:00",
                "dtend": "2026-11-10T13:00:00",
                "location": "Home",
            },
            id="floating",
        ),
        pytest.param(
            _single(";TZID=Europe/Berlin:20261110T100000", "DURATION:PT1H"),
            None,
            {
                "summary": "Single",
                "dtstart": "2026-11-10T12:00:00",
                "dtend": "2026-11-10T14:00:00",
            },
            id="duration",
        ),
        pytest.param(
            _single(";TZID=Europe/Berlin:20261110T100000"),
            None,
            {
                "summary": "Single",
                "dtstart": "2026-11-10T12:00:00",
                "dtend": "2026-11-11T12:00:00",
            },
            id="without_end",
        ),
        pytest.param(
            _single(
                ";TZID=Europe/Berlin:20261110T100030",
                "DTEND;TZID=Europe/Berlin:20261110T110030",
            ),
            None,
            {
                "summary": "Changed",
                "dtstart": "2026-11-10T10:00:00",
                "dtend": "2026-11-10T11:00:00",
            },
            id="times_as_shown",
        ),
        pytest.param(
            _single(
                ";TZID=Europe/Berlin:20261110T100000",
                "DTEND;TZID=Europe/Berlin:20261110T110000",
            ),
            None,
            {
                "summary": "Single",
                "dtstart": "2026-11-10",
                "dtend": "2026-11-12",
            },
            id="to_all_day",
        ),
        pytest.param(
            _single(
                ";VALUE=DATE:20261110", "DTEND;VALUE=DATE:20261111", timezone=False
            ),
            None,
            {
                "summary": "Single",
                "dtstart": "2026-11-10T09:00:00",
                "dtend": "2026-11-10T10:00:00",
            },
            id="to_timed",
        ),
        pytest.param(
            _single(
                ";TZID=Europe/Berlin:20261110T100000",
                "DTEND;TZID=Europe/Berlin:20261110T110000",
            ),
            None,
            {
                "summary": "Single",
                "dtstart": "2026-11-10T10:00:00",
                "dtend": "2026-11-10T11:00:00",
                "rrule": "FREQ=WEEKLY;UNTIL=20261231",
            },
            id="add_rule",
        ),
        pytest.param(
            _single(";VALUE=DATE:20261110", "DTEND;VALUE=DATE:20261111"),
            None,
            {
                "summary": "Single",
                "dtstart": "2026-11-10",
                "dtend": "2026-11-11",
                "rrule": "FREQ=DAILY;UNTIL=20261120T230000Z",
            },
            id="add_rule_all_day",
        ),
        pytest.param(
            _single(
                ";TZID=Europe/Berlin:20261110T100000",
                "DTEND;TZID=Europe/Berlin:20261110T110000",
            ),
            "2026-11-10 10:00:00+01:00",
            {
                "summary": "Changed",
                "dtstart": "2026-11-10T10:00:00",
                "dtend": "2026-11-10T11:00:00",
            },
            id="with_recurrence_id",
        ),
        pytest.param(
            _single(";VALUE=DATE:20261110", "DTEND;VALUE=DATE:20261110"),
            None,
            {
                "summary": "Changed",
                "dtstart": "2026-11-10",
                "dtend": "2026-11-11",
            },
            id="all_day_without_length",
        ),
    ],
)
async def test_update_event(
    ws_client: ClientFixture,
    item: Event,
    recurrence_id: str | None,
    event: dict[str, Any],
    snapshot: SnapshotAssertion,
) -> None:
    """Test changing an event that doesn't repeat, keeping how it is stored."""
    resp = await _send(ws_client, "update", "single", recurrence_id, "", event)

    assert resp["success"]
    _assert_saved(item)
    assert item.data == snapshot


@pytest.mark.parametrize(
    ("ics", "uid", "recurrence_id", "event"),
    [
        pytest.param(
            _daily(),
            "daily",
            "2026-11-12 10:00:00+01:00",
            {
                "summary": "Moved",
                "dtstart": "2026-11-12T15:00:00",
                "dtend": "2026-11-12T16:00:00",
            },
            id="new_override",
        ),
        pytest.param(
            _daily("RRULE:FREQ=DAILY;COUNT=5", _override(12, "1500", "1600", "Moved")),
            "daily",
            "2026-11-12 10:00:00+01:00",
            {
                "summary": "Moved again",
                "dtstart": "2026-11-12T15:00:00",
                "dtend": "2026-11-12T16:00:00",
                "rrule": "FREQ=DAILY;COUNT=5",
            },
            id="existing_override",
        ),
        pytest.param(
            _ics(ALL_DAY),
            "bins",
            "2026-11-16",
            {
                "summary": "Bins",
                "dtstart": "2026-11-17",
                "dtend": "2026-11-18",
            },
            id="all_day",
        ),
        pytest.param(
            _ics(FLOATING),
            "float",
            "2026-11-12 08:00:00",
            {
                "summary": "Float",
                "dtstart": "2026-11-12T09:00:00",
                "dtend": "2026-11-12T10:00:00",
            },
            id="floating",
        ),
        pytest.param(
            _ics(ORPHANS),
            "orphans",
            "2026-11-17 09:00:00+00:00",
            {
                "summary": "Second changed",
                "dtstart": "2026-11-17T10:00:00",
                "dtend": "2026-11-17T11:00:00",
            },
            id="occurrence_without_series",
        ),
    ],
)
async def test_update_occurrence(
    ws_client: ClientFixture,
    item: Event,
    uid: str,
    recurrence_id: str,
    event: dict[str, Any],
    snapshot: SnapshotAssertion,
) -> None:
    """Test changing one occurrence with an override of the series."""
    resp = await _send(ws_client, "update", uid, recurrence_id, "", event)

    assert resp["success"]
    _assert_saved(item)
    assert item.data == snapshot


@pytest.mark.parametrize(
    ("ics", "uid", "recurrence_id", "event"),
    [
        pytest.param(
            _daily(),
            "daily",
            "2026-11-13 10:00:00+01:00",
            {
                "summary": "Later",
                "dtstart": "2026-11-13T10:00:00",
                "dtend": "2026-11-13T11:00:00",
            },
            id="split",
        ),
        pytest.param(
            _daily(
                "RRULE:FREQ=DAILY;COUNT=5\n"
                "EXDATE;TZID=Europe/Berlin:20261111T100000,20261114T100000"
            ),
            "daily",
            "2026-11-12 10:00:00+01:00",
            {
                "summary": "Daily",
                "dtstart": "2026-11-12T11:00:00",
                "dtend": "2026-11-12T12:30:00",
            },
            id="split_moved",
        ),
        pytest.param(
            _daily(
                "RRULE:FREQ=DAILY;UNTIL=20261114T090000Z",
                _override(13, "1500", "1600"),
                _override(14, summary="Special"),
            ),
            "daily",
            "2026-11-12 10:00:00+01:00",
            {
                "summary": "Renamed",
                "dtstart": "2026-11-12T11:00:00",
                "dtend": "2026-11-12T12:00:00",
            },
            id="split_moved_with_overrides",
        ),
        pytest.param(
            _daily(
                "RRULE:FREQ=DAILY;COUNT=5\nEXDATE;TZID=Europe/Berlin:20261114T100000",
                _override(13, "1500", "1600"),
            ),
            "daily",
            "2026-11-12 10:00:00+01:00",
            {
                "summary": "Weekly",
                "dtstart": "2026-11-12T10:00:00",
                "dtend": "2026-11-12T11:00:00",
                "rrule": "FREQ=WEEKLY;COUNT=3",
            },
            id="split_new_rule",
        ),
        pytest.param(
            _daily("RRULE:FREQ=DAILY;COUNT=5", _override(13, "1500", "1600", "Moved")),
            "daily",
            "2026-11-13 10:00:00+01:00",
            {
                "summary": "Later",
                "dtstart": "2026-11-13T15:00:00",
                "dtend": "2026-11-13T16:00:00",
            },
            id="split_at_override",
        ),
        pytest.param(
            _daily("RRULE:FREQ=DAILY;COUNT=5", _override(13, "1500", "1600", "Moved")),
            "daily",
            "2026-11-13 10:00:00+01:00",
            {
                "summary": "Later",
                "dtstart": "2026-11-13T16:00:00",
                "dtend": "2026-11-13T17:00:00",
            },
            id="split_at_moved_override",
        ),
        pytest.param(
            _ics(ALL_DAY),
            "bins",
            "2026-11-16",
            {
                "summary": "Bins on Tuesday",
                "dtstart": "2026-11-17",
                "dtend": "2026-11-18",
            },
            id="all_day_split",
        ),
        pytest.param(
            _ics(FLOATING),
            "float",
            "2026-11-12 08:00:00",
            {
                "summary": "Float",
                "dtstart": "2026-11-12T09:00:00",
                "dtend": "2026-11-12T09:30:00",
            },
            id="floating_split",
        ),
        pytest.param(
            _ics(FLOATING.replace("FREQ=DAILY", "FREQ=DAILY;UNTIL=20261114T080000Z")),
            "float",
            "2026-11-12 08:00:00",
            {
                "summary": "Float",
                "dtstart": "2026-11-12T09:00:00",
                "dtend": "2026-11-12T09:30:00",
            },
            id="floating_split_utc_until",
        ),
        pytest.param(
            _daily("RRULE:FREQ=DAILY;COUNT=6\nEXDATE;VALUE=DATE:20261114"),
            "daily",
            "2026-11-12 10:00:00+01:00",
            {
                "summary": "Daily",
                "dtstart": "2026-11-12T11:00:00",
                "dtend": "2026-11-12T12:00:00",
            },
            id="split_moves_date_exclusion",
        ),
        pytest.param(
            _ics(RDATE_ONLY),
            "dates",
            "2026-11-12 10:00:00+01:00",
            {
                "summary": "Dates",
                "dtstart": "2026-11-12T11:00:00",
                "dtend": "2026-11-12T12:00:00",
            },
            id="added_dates_split",
        ),
        pytest.param(
            _daily(
                "RRULE:FREQ=DAILY;COUNT=3\nRDATE;TZID=Europe/Berlin:20261120T100000"
            ),
            "daily",
            "2026-11-20 10:00:00+01:00",
            {
                "summary": "Last",
                "dtstart": "2026-11-20T10:00:00",
                "dtend": "2026-11-20T11:00:00",
            },
            id="split_after_rule",
        ),
        pytest.param(
            _daily("RRULE:FREQ=DAILY;COUNT=5", _override(5, summary="Before")),
            "daily",
            "2026-11-10 10:00:00+01:00",
            {
                "summary": "Later",
                "dtstart": "2026-11-10T10:00:00",
                "dtend": "2026-11-10T11:00:00",
            },
            id="split_keeps_earlier_override",
        ),
    ],
)
async def test_update_this_and_future_split(
    ws_client: ClientFixture,
    calendars: list[Mock],
    item: Event,
    uid: str,
    recurrence_id: str,
    event: dict[str, Any],
    snapshot: SnapshotAssertion,
) -> None:
    """Test changing the occurrences from one on, which split off the series."""
    resp = await _send(ws_client, "update", uid, recurrence_id, "THISANDFUTURE", event)

    assert resp["success"]
    _assert_saved(item)
    assert item.data == snapshot(name="series")
    assert _new_object(calendars[0]) == snapshot(name="new_series")


@pytest.mark.parametrize(
    ("ics", "recurrence_id", "recurrence_range", "event"),
    [
        pytest.param(
            _daily(
                "RRULE:FREQ=DAILY;COUNT=5\nEXDATE;TZID=Europe/Berlin:20261112T100000",
                _override(13, "1500", "1600"),
            ),
            "2026-11-10 10:00:00+01:00",
            "THISANDFUTURE",
            {
                "summary": "Daily",
                "dtstart": "2026-11-10T09:00:00",
                "dtend": "2026-11-10T10:00:00",
            },
            id="from_first_moved",
        ),
        pytest.param(
            _daily("RRULE:FREQ=DAILY;COUNT=5", _override(13, "1500", "1600")),
            None,
            None,
            {
                "summary": "Renamed",
                "dtstart": "2026-11-10T10:00:00",
                "dtend": "2026-11-10T11:00:00",
                "description": "New",
            },
            id="whole_series",
        ),
        pytest.param(
            _daily(
                "RRULE:FREQ=DAILY;COUNT=5\nEXDATE;TZID=Europe/Berlin:20261110T100000"
            ),
            "2026-11-11 10:00:00+01:00",
            "THISANDFUTURE",
            {
                "summary": "Daily",
                "dtstart": "2026-11-11T10:00:00",
                "dtend": "2026-11-11T11:00:00",
                "rrule": "FREQ=DAILY;COUNT=5",
            },
            id="from_first_shown",
        ),
        pytest.param(
            _daily("RRULE:FREQ=DAILY;COUNT=5", _override(13, "1500", "1600")),
            "2026-11-10 10:00:00+01:00",
            "THISANDFUTURE",
            {
                "summary": "Weekly",
                "dtstart": "2026-11-10T10:00:00",
                "dtend": "2026-11-10T11:00:00",
                "rrule": "FREQ=WEEKLY;UNTIL=20261231",
            },
            id="new_rule",
        ),
    ],
)
async def test_update_whole_series(
    ws_client: ClientFixture,
    calendars: list[Mock],
    item: Event,
    recurrence_id: str | None,
    recurrence_range: str | None,
    event: dict[str, Any],
    snapshot: SnapshotAssertion,
) -> None:
    """Test changing all occurrences, which keeps the series in its object."""
    resp = await _send(
        ws_client, "update", "daily", recurrence_id, recurrence_range, event
    )

    assert resp["success"]
    _assert_saved(item)
    calendars[0].add_event.assert_not_called()
    assert item.data == snapshot


@pytest.mark.parametrize(
    ("ics", "uid", "recurrence_id", "recurrence_range", "event", "message"),
    [
        pytest.param(
            _daily(),
            "daily",
            "2026-11-12 10:00:00+01:00",
            "",
            {
                "summary": "Daily",
                "dtstart": "2026-11-12T10:00:00",
                "dtend": "2026-11-12T11:00:00",
                "rrule": "FREQ=WEEKLY",
            },
            "A single occurrence of a recurring event can't get a repeat rule of"
            " its own",
            id="rule_for_occurrence",
        ),
        pytest.param(
            _daily(),
            "daily",
            "2026-11-12 10:00:00+01:00",
            "",
            {"summary": "Daily", "dtstart": "2026-11-12", "dtend": "2026-11-13"},
            "A recurring event can't be changed between all day and a time of day",
            id="occurrence_to_all_day",
        ),
        pytest.param(
            _daily(),
            "daily",
            "2026-11-12 10:00:00+01:00",
            "THISANDFUTURE",
            {"summary": "Daily", "dtstart": "2026-11-12", "dtend": "2026-11-13"},
            "A recurring event can't be changed between all day and a time of day",
            id="series_to_all_day",
        ),
        pytest.param(
            _ics(WEEKLY_MONDAY),
            "weekly",
            "2026-11-09 10:00:00+01:00",
            "THISANDFUTURE",
            {
                "summary": "Weekly",
                "dtstart": "2026-11-10T10:00:00",
                "dtend": "2026-11-10T11:00:00",
            },
            "The new start doesn't match the repeat rule, change the repeat rule"
            " as well",
            id="start_not_on_rule",
        ),
        pytest.param(
            _ics(WEEKLY_MONDAY),
            "weekly",
            "2026-11-11 10:00:00+01:00",
            "THISANDFUTURE",
            {
                "summary": "Weekly",
                "dtstart": "2026-11-11T10:00:00",
                "dtend": "2026-11-11T11:00:00",
            },
            "The following occurrences can't be changed from an occurrence that was"
            " added to the repeat rule",
            id="split_at_added_date",
        ),
        pytest.param(
            _ics(ORPHANS),
            "orphans",
            "2026-11-10 09:00:00+00:00",
            "THISANDFUTURE",
            {
                "summary": "First",
                "dtstart": "2026-11-10T10:00:00",
                "dtend": "2026-11-10T11:00:00",
            },
            "This occurrence isn't part of a repeat rule, so it can only be changed"
            " on its own",
            id="occurrences_without_series",
        ),
        pytest.param(
            _ics(ORPHANS),
            "orphans",
            None,
            None,
            {
                "summary": "First",
                "dtstart": "2026-11-10T10:00:00",
                "dtend": "2026-11-10T11:00:00",
            },
            "This occurrence isn't part of a repeat rule, so it can only be changed"
            " on its own",
            id="all_occurrences_without_series",
        ),
        pytest.param(
            _daily("RRULE:FREQ=DAILY;COUNT=5", _override(20, "1500", "1600")),
            "daily",
            "2026-11-20 10:00:00+01:00",
            "THISANDFUTURE",
            {
                "summary": "Daily",
                "dtstart": "2026-11-20T15:00:00",
                "dtend": "2026-11-20T16:00:00",
            },
            "This occurrence isn't part of a repeat rule, so it can only be changed"
            " on its own",
            id="occurrence_outside_series",
        ),
        pytest.param(
            _ics(NEW_YORK),
            "new-york",
            None,
            None,
            {
                "summary": "New York",
                "dtstart": "2026-11-01T07:30:00",
                "dtend": "2026-11-01T08:30:00",
            },
            "The time is in the hour that repeats when clocks go back, which the"
            " time zone of the event can't store",
            id="repeated_hour",
        ),
        pytest.param(
            _daily(),
            "daily",
            "2026-11-20 10:00:00+01:00",
            "",
            {
                "summary": "Daily",
                "dtstart": "2026-11-20T10:00:00",
                "dtend": "2026-11-20T11:00:00",
            },
            "The occurrence was not found in the event",
            id="not_an_occurrence",
        ),
        pytest.param(
            _single(";TZID=Unknown/Zone:20261110T100000"),
            "single",
            None,
            None,
            {
                "summary": "Single",
                "dtstart": "2026-11-10T12:00:00",
                "dtend": "2026-11-10T13:00:00",
            },
            "The event uses iCalendar features that Home Assistant can't change",
            id="unknown_time_zone",
        ),
    ],
)
async def test_update_errors(
    ws_client: ClientFixture,
    calendars: list[Mock],
    item: Event,
    uid: str,
    recurrence_id: str | None,
    recurrence_range: str | None,
    event: dict[str, Any],
    message: str,
) -> None:
    """Test that changes that can't be made raise and change nothing."""
    resp = await _send(ws_client, "update", uid, recurrence_id, recurrence_range, event)

    assert resp["error"] == {"code": "failed", "message": message}
    item.save.assert_not_called()
    calendars[0].add_event.assert_not_called()


SPLIT_EVENT = {
    "summary": "Later",
    "dtstart": "2026-11-13T10:00:00",
    "dtend": "2026-11-13T11:00:00",
}


@pytest.mark.parametrize(
    ("error", "message"),
    [
        pytest.param(
            PutError("refused"), "Error communicating with the CalDAV server", id="put"
        ),
        pytest.param(
            ETagMismatchError("changed"),
            "The event was changed on the CalDAV server in the meantime, reload it"
            " and try again",
            id="etag",
        ),
    ],
)
async def test_split_rolled_back(
    ws_client: ClientFixture,
    item: Event,
    new_item: Mock,
    error: DAVError,
    message: str,
) -> None:
    """Test that the split off occurrences are removed if the series isn't saved."""
    item.save.side_effect = error

    resp = await _send(
        ws_client,
        "update",
        "daily",
        "2026-11-13 10:00:00+01:00",
        "THISANDFUTURE",
        SPLIT_EVENT,
    )

    assert resp["error"] == {"code": "failed", "message": message}
    new_item.delete.assert_called_once_with()


async def test_split_creation_fails(
    ws_client: ClientFixture, calendars: list[Mock], item: Event
) -> None:
    """Test that the series stays as it was if the new object can't be created."""
    calendars[0].add_event.side_effect = PutError("refused")

    resp = await _send(
        ws_client,
        "update",
        "daily",
        "2026-11-13 10:00:00+01:00",
        "THISANDFUTURE",
        SPLIT_EVENT,
    )

    assert resp["error"]["message"] == "Error communicating with the CalDAV server"
    item.save.assert_not_called()


async def test_split_rollback_fails(
    ws_client: ClientFixture,
    item: Event,
    new_item: Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that a failed rollback is logged and the first error raised."""
    item.save.side_effect = PutError("refused")
    new_item.delete.side_effect = DAVError("gone")

    resp = await _send(
        ws_client,
        "update",
        "daily",
        "2026-11-13 10:00:00+01:00",
        "THISANDFUTURE",
        SPLIT_EVENT,
    )

    assert resp["error"]["message"] == "Error communicating with the CalDAV server"
    assert "Could not remove http://test.local/calendar/new.ics" in caplog.text


async def test_split_kept_when_outcome_unknown(
    ws_client: ClientFixture, item: Event, new_item: Mock
) -> None:
    """Test that the split off occurrences stay when the series may be saved.

    Without a response the series may have been saved, and removing the new
    object would lose the occurrences.
    """
    item.save.side_effect = caldav_requests.exceptions.Timeout()

    resp = await _send(
        ws_client,
        "update",
        "daily",
        "2026-11-13 10:00:00+01:00",
        "THISANDFUTURE",
        SPLIT_EVENT,
    )

    assert resp["error"]["message"] == "Error communicating with the CalDAV server"
    new_item.delete.assert_not_called()


@pytest.mark.parametrize(
    ("cmd", "event"),
    [
        pytest.param("delete", None, id="delete"),
        pytest.param(
            "update",
            {
                "summary": "Daily",
                "dtstart": "2026-11-10T10:00:00",
                "dtend": "2026-11-10T11:00:00",
            },
            id="update",
        ),
    ],
)
@pytest.mark.parametrize(
    ("error", "message"),
    [
        pytest.param(
            NotFoundError("missing"),
            "The event was not found on the CalDAV server",
            id="not_found",
        ),
        pytest.param(
            caldav_requests.exceptions.ConnectionError(),
            "Error communicating with the CalDAV server",
            id="connection_error",
        ),
        pytest.param(
            caldav_requests.exceptions.Timeout(),
            "Error communicating with the CalDAV server",
            id="timeout",
        ),
        pytest.param(
            DAVError("error"),
            "Error communicating with the CalDAV server",
            id="dav_error",
        ),
    ],
)
async def test_lookup_errors(
    ws_client: ClientFixture,
    calendars: list[Mock],
    item: Event,
    cmd: str,
    event: dict[str, Any] | None,
    error: Exception,
    message: str,
) -> None:
    """Test errors looking up the event."""
    calendars[0].get_event_by_uid.side_effect = error

    resp = await _send(ws_client, cmd, "daily", event=event)

    assert resp["error"] == {"code": "failed", "message": message}
    item.save.assert_not_called()
    item.delete.assert_not_called()


@pytest.mark.parametrize(
    ("cmd", "recurrence_id", "event", "method"),
    [
        pytest.param("delete", None, None, "delete", id="delete"),
        pytest.param(
            "delete", "2026-11-12 10:00:00+01:00", None, "save", id="delete_occurrence"
        ),
        pytest.param(
            "update",
            "2026-11-12 10:00:00+01:00",
            {
                "summary": "Moved",
                "dtstart": "2026-11-12T15:00:00",
                "dtend": "2026-11-12T16:00:00",
            },
            "save",
            id="update",
        ),
    ],
)
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ETagMismatchError("changed"), id="etag"),
        pytest.param(ScheduleTagMismatchError("changed"), id="schedule_tag"),
    ],
)
async def test_changed_in_the_meantime(
    ws_client: ClientFixture,
    item: Event,
    cmd: str,
    recurrence_id: str | None,
    event: dict[str, Any] | None,
    method: str,
    error: Exception,
) -> None:
    """Test that an event changed since it was read isn't overwritten."""
    getattr(item, method).side_effect = error

    resp = await _send(ws_client, cmd, "daily", recurrence_id, "", event)

    assert resp["error"] == {
        "code": "failed",
        "message": "The event was changed on the CalDAV server in the meantime,"
        " reload it and try again",
    }
