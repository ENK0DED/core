"""Support for WebDav Calendar."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from functools import partial
import logging
from typing import Any, cast, override

from caldav.calendarobjectresource import Event
from caldav.davclient import DAVClient
from caldav.lib.error import (
    DAVError,
    ETagMismatchError,
    NotFoundError,
    ScheduleTagMismatchError,
)
from caldav.lib.http_sync import requests as caldav_requests
import probatio

from homeassistant.components.calendar import (
    ENTITY_ID_FORMAT,
    PLATFORM_SCHEMA as CALENDAR_PLATFORM_SCHEMA,
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
    is_offset_reached,
)
from homeassistant.const import (
    CONF_NAME,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    AddEntitiesCallback,
)
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import CalDavConfigEntry
from .api import async_get_calendar_color, async_get_calendars
from .const import DOMAIN, TIMEOUT
from .coordinator import CalDavUpdateCoordinator
from .event_edit import EditError, EditResult, delete_event, update_event

_LOGGER = logging.getLogger(__name__)

CONF_CALENDARS = "calendars"
CONF_CUSTOM_CALENDARS = "custom_calendars"
CONF_CALENDAR = "calendar"
CONF_SEARCH = "search"
CONF_DAYS = "days"

# Number of days to look ahead for next event when configured by ConfigEntry
CONFIG_ENTRY_DEFAULT_DAYS = 7

# Only allow VCALENDARs that support this component type
SUPPORTED_COMPONENT = "VEVENT"

THIS_AND_FUTURE = "THISANDFUTURE"

PLATFORM_SCHEMA = CALENDAR_PLATFORM_SCHEMA.extend(
    {
        probatio.Required(CONF_URL): probatio.Url(),
        probatio.Optional(CONF_CALENDARS, default=[]): probatio.All(
            probatio.EnsureList(), [cv.string]
        ),
        probatio.Inclusive(CONF_USERNAME, "authentication"): cv.string,
        probatio.Inclusive(probatio.Secret(CONF_PASSWORD), "authentication"): cv.string,
        probatio.Optional(CONF_CUSTOM_CALENDARS, default=[]): probatio.All(
            probatio.EnsureList(),
            [
                probatio.Schema(
                    {
                        probatio.Required(CONF_CALENDAR): cv.string,
                        probatio.Required(CONF_NAME): cv.string,
                        probatio.Required(CONF_SEARCH): cv.string,
                    }
                )
            ],
        ),
        probatio.Optional(CONF_VERIFY_SSL, default=True): cv.boolean,
        probatio.Optional(CONF_DAYS, default=1): cv.positive_int,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    disc_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the WebDav Calendar platform."""
    url = config[CONF_URL]
    username = config.get(CONF_USERNAME)
    password = config.get(CONF_PASSWORD)
    days = config[CONF_DAYS]

    client = await hass.async_add_executor_job(
        partial(
            DAVClient,
            url,
            None,
            username,
            password,
            ssl_verify_cert=config[CONF_VERIFY_SSL],
            timeout=TIMEOUT,
        )
    )

    calendars = await async_get_calendars(hass, client, SUPPORTED_COMPONENT)

    entities = []
    device_id: str | None
    for calendar, calendar_name in calendars:
        # If a calendar name was given in the configuration,
        # ignore all the others
        if config[CONF_CALENDARS] and calendar_name not in config[CONF_CALENDARS]:
            _LOGGER.debug("Ignoring calendar '%s'", calendar_name)
            continue

        # Create additional calendars based on custom filtering rules
        for cust_calendar in config[CONF_CUSTOM_CALENDARS]:
            # Check that the base calendar matches
            if cust_calendar[CONF_CALENDAR] != calendar_name:
                continue

            name = cust_calendar[CONF_NAME]
            device_id = f"{cust_calendar[CONF_CALENDAR]} {cust_calendar[CONF_NAME]}"
            entity_id = async_generate_entity_id(ENTITY_ID_FORMAT, device_id, hass=hass)
            coordinator = CalDavUpdateCoordinator(
                hass,
                None,
                calendar=calendar,
                calendar_name=calendar_name,
                days=days,
                include_all_day=True,
                search=cust_calendar[CONF_SEARCH],
            )
            entities.append(
                WebDavCalendarEntity(name, entity_id, coordinator, supports_offset=True)
            )

        # Create a default calendar if there was no custom one for all calendars
        # that support events.
        if not config[CONF_CUSTOM_CALENDARS]:
            name = calendar_name
            device_id = calendar_name
            entity_id = async_generate_entity_id(ENTITY_ID_FORMAT, device_id, hass=hass)
            coordinator = CalDavUpdateCoordinator(
                hass,
                None,
                calendar=calendar,
                calendar_name=calendar_name,
                days=days,
                include_all_day=False,
                search=None,
            )
            entities.append(
                WebDavCalendarEntity(name, entity_id, coordinator, supports_offset=True)
            )

    async_add_entities(entities, True)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalDavConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the CalDav calendar platform for a config entry."""
    calendars = await async_get_calendars(hass, entry.runtime_data, SUPPORTED_COMPONENT)
    entities = [
        WebDavCalendarEntity(
            calendar_name,
            async_generate_entity_id(ENTITY_ID_FORMAT, calendar_name, hass=hass),
            CalDavUpdateCoordinator(
                hass,
                entry,
                calendar=calendar,
                calendar_name=calendar_name,
                days=CONFIG_ENTRY_DEFAULT_DAYS,
                include_all_day=True,
                search=None,
            ),
            unique_id=f"{entry.entry_id}-{calendar.id}",
        )
        for calendar, calendar_name in calendars
        if calendar_name
    ]
    async_add_entities(entities, True)


class WebDavCalendarEntity(CoordinatorEntity[CalDavUpdateCoordinator], CalendarEntity):
    """A device for getting the next Task from a WebDav Calendar."""

    _attr_supported_features = (
        CalendarEntityFeature.CREATE_EVENT
        | CalendarEntityFeature.UPDATE_EVENT
        | CalendarEntityFeature.DELETE_EVENT
    )

    def __init__(
        self,
        name: str | None,
        entity_id: str,
        coordinator: CalDavUpdateCoordinator,
        unique_id: str | None = None,
        supports_offset: bool = False,
    ) -> None:
        """Create the WebDav Calendar Event Device."""
        super().__init__(coordinator)
        self.entity_id = entity_id
        self._event: CalendarEvent | None = None
        self._attr_name = name
        if unique_id is not None:
            self._attr_unique_id = unique_id
        self._supports_offset = supports_offset
        self._edit_lock = asyncio.Lock()
        if coordinator.search is not None:
            # Changes would not be checked against the search of the entity
            self._attr_supported_features = CalendarEntityFeature.CREATE_EVENT

    @override
    async def async_prepare_to_add_to_hass(self) -> None:
        """Read the server color of a calendar that has no entity yet.

        The color only applies when an entity is first registered.
        """
        await super().async_prepare_to_add_to_hass()
        if self.unique_id is None or er.async_get(self.hass).async_get_entity_id(
            self.platform.domain, self.platform.platform_name, self.unique_id
        ):
            return
        try:
            self._attr_initial_color = await async_get_calendar_color(
                self.hass, self.coordinator.calendar
            )
        except (caldav_requests.exceptions.RequestException, DAVError) as err:
            _LOGGER.debug("Cannot read the color of %s: %s", self.name, err)

    @property
    @override
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        return self._event

    @override
    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Get all events in a specific time frame."""
        return await self.coordinator.async_get_events(hass, start_date, end_date)

    @override
    async def async_create_event(self, **kwargs: Any) -> None:
        """Create a new event in the calendar."""
        _LOGGER.debug("Event: %s", kwargs)

        item_data: dict[str, Any] = {
            "summary": kwargs["summary"],
            "dtstart": kwargs["dtstart"],
            "dtend": kwargs["dtend"],
        }
        if description := kwargs.get("description"):
            item_data["description"] = description
        if location := kwargs.get("location"):
            item_data["location"] = location
        if rrule := kwargs.get("rrule"):
            item_data["rrule"] = rrule

        _LOGGER.debug("ICS data %s", item_data)

        try:
            await self.hass.async_add_executor_job(
                partial(self.coordinator.calendar.add_event, **item_data),
            )
        except (
            caldav_requests.exceptions.ConnectionError,
            caldav_requests.exceptions.Timeout,
            DAVError,
        ) as err:
            raise HomeAssistantError(f"CalDAV save error: {err}") from err

    @override
    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Update an event, one of its occurrences or those from one on."""
        await self._async_edit(
            partial(
                self._update_event,
                uid,
                event,
                recurrence_id,
                recurrence_range == THIS_AND_FUTURE,
            )
        )

    @override
    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete an event, one of its occurrences or those from one on."""
        await self._async_edit(
            partial(
                self._delete_event,
                uid,
                recurrence_id,
                recurrence_range == THIS_AND_FUTURE,
            )
        )

    async def _async_edit(self, edit: Callable[[], None]) -> None:
        """Run a change of an event on the server and refresh."""
        async with self._edit_lock:
            try:
                await self.hass.async_add_executor_job(edit)
            except EditError as err:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key=err.translation_key
                ) from err
            except NotFoundError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="event_not_found"
                ) from err
            except (ETagMismatchError, ScheduleTagMismatchError) as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="event_changed"
                ) from err
            except (
                caldav_requests.exceptions.ConnectionError,
                caldav_requests.exceptions.Timeout,
                DAVError,
            ) as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="request_error"
                ) from err
        await self.coordinator.async_refresh()

    def _get_event(self, uid: str) -> Event:
        """Fetch the CalDAV object of an event."""
        return cast(Event, self.coordinator.calendar.get_event_by_uid(uid))

    def _update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None,
        this_and_future: bool,
    ) -> None:
        """Change an event and save it."""
        item = self._get_event(uid)
        result = update_event(
            item.icalendar_instance,
            uid,
            event,
            recurrence_id,
            this_and_future,
            dt_util.get_default_time_zone(),
            dt_util.utcnow(),
        )
        self._save(item, result)

    def _save(self, item: Event, result: EditResult) -> None:
        """Save a changed event, after the occurrences split off it."""
        if result.new_calendar is None:
            _save_item(item)
            return
        # Created first, so a failure can't lose the occurrences split off
        new_item = cast(
            Event,
            self.coordinator.calendar.add_event(
                ical=result.new_calendar, increase_seqno=False
            ),
        )
        try:
            _save_item(item)
        except DAVError:
            # The server refused the change, so the split off copy is removed
            try:
                new_item.delete()
            except (
                caldav_requests.exceptions.ConnectionError,
                caldav_requests.exceptions.Timeout,
                DAVError,
            ):
                _LOGGER.warning(
                    "Could not remove %s after a failed change, its occurrences may"
                    " show twice",
                    new_item.url,
                )
            raise

    def _delete_event(
        self, uid: str, recurrence_id: str | None, this_and_future: bool
    ) -> None:
        """Delete an event or some of its occurrences."""
        item = self._get_event(uid)
        if recurrence_id is not None:
            calendar = delete_event(
                item.icalendar_instance,
                uid,
                recurrence_id,
                this_and_future,
                dt_util.get_default_time_zone(),
                dt_util.utcnow(),
            )
            if calendar is not None:
                _save_item(item)
                return
        etag = item.etag
        # Only the version that was read is deleted; If-Match can't use a weak ETag
        if_match = bool(item.schedule_tag or (etag and not etag.startswith("W/")))
        item.delete(if_match=if_match, precondition="etag")

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Update event data."""
        self._event = self.coordinator.data
        if self._supports_offset:
            self._attr_extra_state_attributes = {
                "offset_reached": is_offset_reached(
                    self._event.start_datetime_local,
                    self.coordinator.offset,  # type: ignore[arg-type]
                )
                if self._event
                else False
            }
        super()._handle_coordinator_update()

    @override
    async def async_added_to_hass(self) -> None:
        """When entity is added to hass update state from existing coordinator data."""
        await super().async_added_to_hass()
        self._handle_coordinator_update()


def _save_item(item: Event) -> None:
    """Save a changed CalDAV object, if it is still the version that was read.

    The components are saved as they are: by default the library would raise
    SEQUENCE again and merge only the first component when it is an override.
    """
    item.save(increase_seqno=False, only_this_recurrence=False, precondition="etag")
