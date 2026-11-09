"""Change and delete events in the iCalendar data of a CalDAV object.

A CalDAV object holds one event: its main component and the components that
override single occurrences of it, all with the same UID. Changes are made to
these components, so whatever Home Assistant doesn't show is kept, and times
are written the way the event stores them: as a date, floating, in UTC or with
its TZID.
"""

from collections.abc import Callable, Iterable, Iterator
import copy
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from itertools import islice
from typing import TYPE_CHECKING, Any, Literal, Self, cast
from uuid import uuid4

from dateutil.rrule import rrule, rruleset, rrulestr
from icalendar import Calendar, Event, Timezone, vDDDLists, vDDDTypes, vRecur

# Rules are walked from their start, which a dense rule makes slow
MAX_OCCURRENCES = 100_000

SUMMARY = "summary"
DESCRIPTION = "description"
LOCATION = "location"
DTSTART = "dtstart"
DTEND = "dtend"
RRULE = "rrule"
TEXT_FIELDS = (SUMMARY, DESCRIPTION, LOCATION)

type Time = date | datetime


class EditError(Exception):
    """A change that can't be made to the event."""

    def __init__(self, translation_key: str) -> None:
        """Initialize with the translation key of the reason."""
        super().__init__(translation_key)
        self.translation_key = translation_key


@dataclass
class EditResult:
    """The CalDAV objects to write for a change.

    The new object holds the occurrences split off a recurring event.
    """

    calendar: Calendar
    new_calendar: Calendar | None = None


@dataclass(frozen=True)
class _Form:
    """How a property stores a time: as a date, floating, in UTC or zoned."""

    kind: Literal["date", "floating", "utc", "zoned"]
    local: tzinfo
    zone: tzinfo | None = None
    tzid: str | None = None

    @classmethod
    def of(cls, prop: vDDDTypes, local: tzinfo) -> Self:
        """Return the form of a property."""
        value = prop.dt
        tzid = prop.params.get("TZID")
        if not isinstance(value, datetime):
            return cls("date", local)
        if value.tzinfo is None:
            if tzid:
                # A TZID that neither the object nor the tz database defines
                raise EditError("event_not_supported")
            return cls("floating", local)
        if tzid:
            return cls("zoned", local, value.tzinfo, tzid)
        return cls("utc", local, UTC)

    @classmethod
    def like(cls, value: Time, local: tzinfo) -> Self:
        """Return the form for a time sent by Home Assistant."""
        if not isinstance(value, datetime):
            return cls("date", local)
        return cls("zoned", local, local, str(local))

    def convert(self, value: Time) -> Time | None:
        """Return a time in this form, None for a date that has no time."""
        if self.kind == "date":
            if isinstance(value, datetime):
                return self._local(value).date()
            return value
        if not isinstance(value, datetime):
            return None
        if self.zone is None:
            return self._local(value).replace(tzinfo=None)
        if value.tzinfo is None:
            return value.replace(tzinfo=self.zone)
        return value.astimezone(self.zone)

    def _local(self, value: datetime) -> datetime:
        """Return a time as a local one, a floating time as it is."""
        return value.astimezone(self.local) if value.tzinfo else value

    def until(self, value: Time) -> Time:
        """Return the UNTIL of a rule as RFC 5545 has it for this form."""
        if self.kind == "date":
            return value.date() if isinstance(value, datetime) else value
        if not isinstance(value, datetime):
            value = datetime.combine(value, time(23, 59, 59))
        if self.zone is None:
            return self._local(value).replace(tzinfo=None)
        return value.replace(tzinfo=value.tzinfo or self.zone).astimezone(UTC)

    def until_before(self, cutoff: Time) -> Time:
        """Return an UNTIL that ends a rule just before an occurrence."""
        if not isinstance(cutoff, datetime):
            return cutoff - timedelta(days=1)
        return self.until(cutoff - timedelta(seconds=1))

    def delta(self, new: Time, old: Time) -> timedelta:
        """Return the difference of two times in wall time of this form."""
        if isinstance(new, datetime) and isinstance(old, datetime):
            return self._wall(new) - self._wall(old)
        if not isinstance(new, datetime) and not isinstance(old, datetime):
            return new - old
        raise EditError("all_day_change_not_supported")

    def _wall(self, value: datetime) -> datetime:
        """Return a time as wall time in this form's zone."""
        if self.zone is None:
            return self._local(value).replace(tzinfo=None)
        return _aware(value, self.zone).astimezone(self.zone).replace(tzinfo=None)

    def shift(self, value: Any, delta: timedelta) -> Any:
        """Move a time by a wall time difference, keeping how it is stored."""
        if isinstance(value, tuple):
            start, end = value
            if not isinstance(end, timedelta):
                end = self.shift(end, delta)
            return (self.shift(start, delta), end)
        if not isinstance(value, datetime) or value.tzinfo is None:
            return value + delta
        zone = self.zone or value.tzinfo
        wall = value.astimezone(zone).replace(tzinfo=None) + delta
        return wall.replace(tzinfo=zone).astimezone(value.tzinfo)

    def prop(self, value: Time) -> vDDDTypes:
        """Return a property for a time in this form."""
        prop = vDDDTypes(self._fit(value))
        self._set_params(prop, [value])
        return prop

    def props(self, values: list[Time]) -> vDDDLists:
        """Return a list property for times in this form."""
        prop = vDDDLists([self._fit(value) for value in values])
        self._set_params(prop, values)
        return prop

    def _fit(self, value: Time) -> Time:
        """Return a time converted to this form, when it has a time to convert."""
        return value if (converted := self.convert(value)) is None else converted

    def _set_params(self, prop: vDDDTypes | vDDDLists, values: list[Time]) -> None:
        """Set the parameters that tell how the times are stored."""
        prop.params.pop("TZID", None)
        if self.kind == "date":
            prop.params["VALUE"] = "DATE"
        elif self.kind == "zoned":
            for value in values:
                if (
                    isinstance(wall := self._fit(value), datetime)
                    and wall.fold
                    and wall.utcoffset() != wall.replace(fold=0).utcoffset()
                ):
                    # A wall time with a TZID is the first of two repeated hours
                    raise EditError("repeated_hour")
            prop.params["TZID"] = self.tzid


def _as_datetime(value: Time) -> datetime:
    """Return a time as dateutil handles it, a date as midnight."""
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time())


def _aware(value: datetime, zone: tzinfo) -> datetime:
    """Return a time with a zone, giving a floating one the zone."""
    return value if value.tzinfo else value.replace(tzinfo=zone)


def _props(component: Event, name: str) -> list[Any]:
    """Return all properties of a name, which may occur more than once."""
    value = component.get(name)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _between(start: Time, end: Time) -> timedelta:
    """Return the time from a start to an end of the same kind."""
    return cast(timedelta, end - start)  # type: ignore[operator]


def _span(component: Event) -> tuple[Time, Time]:
    """Return the start and end of a component as the coordinator shows it."""
    start: Time = component["DTSTART"].dt
    end: Time
    if "DTEND" in component:
        end = component["DTEND"].dt
    elif "DURATION" in component:
        end = start + component["DURATION"].dt
    else:
        end = start + timedelta(days=1)
    if not isinstance(end, datetime) and end == start:
        end += timedelta(days=1)
    return start, end


def _shown_as(value: Time, shown: Time, local: tzinfo) -> bool:
    """Return whether a time sent by the frontend is the one it showed.

    The frontend shows and sends whole minutes in Home Assistant's time zone,
    so it can't send seconds or the second of two hours that repeat.
    """
    if not isinstance(value, datetime) or not isinstance(shown, datetime):
        return type(value) is type(shown) and value == shown
    value = _aware(value, local).astimezone(local)
    shown = _aware(shown, local).astimezone(local)
    return value == shown or (
        value.second == value.microsecond == 0
        and value.replace(tzinfo=None)
        == shown.replace(tzinfo=None, second=0, microsecond=0)
    )


def _same_times(changes: dict[str, Any], start: Time, end: Time, local: tzinfo) -> bool:
    """Return whether a change keeps the times the frontend showed."""
    return _shown_as(changes[DTSTART], start, local) and _shown_as(
        changes[DTEND], end, local
    )


def _touch(component: Event, now: datetime) -> None:
    """Mark a component as changed."""
    for name in ("DTSTAMP", "LAST-MODIFIED"):
        component.pop(name, None)
        component.add(name, now)
    sequence = int(component.pop("SEQUENCE", 0))
    component.add("SEQUENCE", sequence + 1)


def _text(component: Event, key: str) -> str:
    """Return a text field of a component, empty if it has none."""
    return str(component.get(key, ""))


def _set_text(component: Event, key: str, value: str) -> None:
    """Write a text field, removing it if it is empty."""
    component.pop(key, None)
    if value:
        component.add(key, value)


def _set_fields(component: Event, changes: dict[str, Any]) -> list[str]:
    """Write the text fields that a change sets differently."""
    changed = [
        key
        for key in TEXT_FIELDS
        if key in changes and (changes[key] or "") != _text(component, key)
    ]
    for key in changed:
        _set_text(component, key, changes[key] or "")
    return changed


def _write_times(component: Event, start: Time, end: Time, form: _Form) -> None:
    """Write the start and end of a component, DTEND keeping its own zone."""
    end_form = form
    if "DTEND" in component and (
        (old := _Form.of(component["DTEND"], form.local)).kind == form.kind
    ):
        end_form = old
    component["DTSTART"] = form.prop(start)
    if "DURATION" in component:
        component.pop("DURATION")
        component.add("DURATION", _between(start, end))
    elif "DTEND" in component or end != start + timedelta(days=1):
        component["DTEND"] = end_form.prop(end)


def _parse_time(text: str) -> Time:
    """Parse a recurrence id as the coordinator or RFC 5545 writes it."""
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text)
    except ValueError as err:
        raise EditError("occurrence_not_found") from err


def _reaches(values: Iterator[Time], target: Time, *, exact: bool = False) -> bool:
    """Return whether ordered times reach a time, or have it if exact."""
    for value in values:
        if value >= target:
            return value == target or not exact
    return False


class _Series:
    """The occurrences of a main component, in the form of its start."""

    def __init__(self, component: Event, local: tzinfo) -> None:
        """Read the recurrence of a main component."""
        if "DTSTART" not in component or "EXRULE" in component:
            raise EditError("event_not_supported")
        self.form = _Form.of(component["DTSTART"], local)
        self.start: Time = component["DTSTART"].dt
        self.rules = [vRecur(recur) for recur in _props(component, "RRULE")]
        self.rdates = self._times(start for start, _ in component.rdates)
        self.exdates = self._times(component.exdates)
        self._rules = [self._rule(recur) for recur in self.rules]

    def _times(self, values: Iterable[Time]) -> list[Time]:
        """Return times in the form of the start, leaving out unfitting ones."""
        return [
            converted
            for value in values
            if (converted := self.form.convert(value)) is not None
        ]

    @property
    def recurs(self) -> bool:
        """Return whether the component has a rule or added dates."""
        return bool(self.rules or self.rdates)

    def _rule(self, recur: vRecur, start: Time | None = None) -> rrule:
        """Return a rule for dateutil, with an UNTIL that fits the start."""
        recur = vRecur(recur)
        if until := recur.get("UNTIL"):
            recur["UNTIL"] = [self.form.until(until[0])]
        try:
            return cast(
                rrule,
                rrulestr(
                    recur.to_ical().decode(),
                    dtstart=_as_datetime(self.start if start is None else start),
                ),
            )
        except (ValueError, TypeError) as err:
            raise EditError("event_not_supported") from err

    def _walk(self, values: Iterable[datetime]) -> Iterator[Time]:
        """Yield times of dateutil in the form of the start, up to a bound."""
        for count, value in enumerate(values):
            if count == MAX_OCCURRENCES:
                raise EditError("recurrence_too_dense")
            yield value.date() if self.form.kind == "date" else value

    def occurrences(self) -> Iterator[Time]:
        """Yield the occurrences in order, the start always being the first."""
        rule_set = rruleset()
        for rule in self._rules:
            rule_set.rrule(rule)
        for value in (self.start, *self.rdates):
            rule_set.rdate(_as_datetime(value))
        for value in self.exdates:
            rule_set.exdate(_as_datetime(value))
        return self._walk(rule_set)

    def contains(self, value: Time) -> bool:
        """Return whether a time is an occurrence."""
        return _reaches(self.occurrences(), value, exact=True)

    def has_occurrence_before(self, cutoff: Time) -> bool:
        """Return whether there is an occurrence before a time."""
        return any(value < cutoff for value in islice(self.occurrences(), 1))

    def has_occurrence_besides(self, value: Time) -> bool:
        """Return whether there is an occurrence other than one."""
        return any(other != value for other in islice(self.occurrences(), 2))

    def rule_reaches(self, index: int, cutoff: Time) -> bool:
        """Return whether a rule has an occurrence at or after a time."""
        return _reaches(self._walk(self._rules[index]), cutoff)

    def rule_contains(self, index: int, value: Time) -> bool:
        """Return whether a time is an occurrence of a rule."""
        return _reaches(self._walk(self._rules[index]), value, exact=True)

    def rule_count_before(self, index: int, cutoff: Time) -> int:
        """Return the number of occurrences of a rule before a time."""
        count = 0
        for value in self._walk(self._rules[index]):
            if value >= cutoff:
                break
            count += 1
        return count

    def rule_starts_at(self, recur: vRecur, start: Time) -> bool:
        """Return whether a rule that starts at a time has it as occurrence."""
        return next(self._walk(self._rule(recur, start)), None) == start


class _EventObject:
    """The components of one event in a CalDAV object."""

    def __init__(
        self, calendar: Calendar, uid: str, local: tzinfo, now: datetime
    ) -> None:
        """Find the components of an event."""
        self.calendar = calendar
        self.local = local
        self.now = now
        components = [
            component
            for component in calendar.subcomponents
            if component.name == "VEVENT" and str(component.get("UID")) == uid
        ]
        if not components:
            raise EditError("event_not_found")
        self.master = next(
            (component for component in components if "RECURRENCE-ID" not in component),
            None,
        )
        self.overrides = [
            component for component in components if "RECURRENCE-ID" in component
        ]
        if any("RANGE" in item["RECURRENCE-ID"].params for item in self.overrides):
            raise EditError("event_not_supported")
        self.series = None if self.master is None else _Series(self.master, local)
        self.form = (
            self.series.form
            if self.series is not None
            else _Form.of(self.overrides[0]["RECURRENCE-ID"], local)
        )

    def recurrence_id(self, text: str) -> Time:
        """Return a recurrence id in the form of the event."""
        if (value := self.form.convert(_parse_time(text))) is None:
            raise EditError("occurrence_not_found")
        return value

    def override_id(self, override: Event) -> Time | None:
        """Return the recurrence id of an override in the form of the event."""
        return self.form.convert(override["RECURRENCE-ID"].dt)

    def overrides_where(self, test: Callable[[Time], bool]) -> list[Event]:
        """Return the overrides with a recurrence id that passes a test."""
        return [
            override
            for override in self.overrides
            if (value := self.override_id(override)) is not None and test(value)
        ]

    def remove(self, components: list[Event]) -> None:
        """Remove overrides from the object."""
        for component in components:
            self.calendar.subcomponents.remove(component)
            self.overrides.remove(component)

    def find(self, rid: Time) -> tuple[Event | None, bool]:
        """Return the override of an occurrence and whether the series has it."""
        override = next(iter(self.overrides_where(lambda value: value == rid)), None)
        in_series = self.series is not None and self.series.contains(rid)
        if override is None and not in_series:
            raise EditError("occurrence_not_found")
        return override, in_series

    def edit_times(
        self,
        component: Event,
        name: str,
        keep: Callable[[Time], bool],
        delta: timedelta | None = None,
    ) -> None:
        """Keep the RDATE or EXDATE values that pass a test, moving them."""
        lines = _props(component, name)
        component.pop(name, None)
        for line in lines:
            values = []
            for value in (item.dt for item in line.dts):
                start = value[0] if isinstance(value, tuple) else value
                converted = self.form.convert(start)
                if converted is not None and keep(converted):
                    values.append(self.form.shift(value, delta) if delta else value)
            if values:
                prop = vDDDLists(values)
                prop.params = copy.copy(line.params)
                component.add(name, prop)

    def end_before(self, master: Event, series: _Series, cutoff: Time) -> bool:
        """End the occurrences of the main component before a time.

        Return whether the main component has any left, it is removed if not:
        a rule ending before its DTSTART would still keep that occurrence.
        """
        if not series.has_occurrence_before(cutoff):
            self.calendar.subcomponents.remove(master)
            return False
        for index, recur in enumerate(_props(master, "RRULE")):
            if series.rule_reaches(index, cutoff):
                recur.pop("COUNT", None)
                recur["UNTIL"] = [self.form.until_before(cutoff)]
        self.edit_times(master, "RDATE", lambda value: value < cutoff)
        self.edit_times(master, "EXDATE", lambda value: value < cutoff)
        _touch(master, self.now)
        return True

    def new_rules(self, changes: dict[str, Any], form: _Form) -> list[vRecur] | None:
        """Return the rule sent with a change, with an UNTIL that fits."""
        if not (text := changes.get(RRULE)):
            return None
        recur = vRecur.from_ical(text)
        if until := recur.get("UNTIL"):
            recur["UNTIL"] = [form.until(until[0])]
        return [recur]

    def same_rules(self, rules: list[vRecur]) -> bool:
        """Return whether rules are the ones of the series."""
        series_rules = self.series.rules if self.series is not None else []

        def key(recur: vRecur) -> bytes:
            recur = vRecur(recur)
            if until := recur.get("UNTIL"):
                recur["UNTIL"] = [self.form.until(until[0])]
            return bytes(recur.to_ical())

        return sorted(map(key, rules)) == sorted(map(key, series_rules))


def delete_event(
    calendar: Calendar,
    uid: str,
    recurrence_id: str,
    this_and_future: bool,
    local: tzinfo,
    now: datetime,
) -> Calendar | None:
    """Delete one occurrence of an event, or the occurrences from one on.

    Return the changed object, None if nothing is left of the event.
    """
    event = _EventObject(calendar, uid, local, now)
    rid = event.recurrence_id(recurrence_id)
    _, in_series = event.find(rid)
    if this_and_future:
        event.remove(event.overrides_where(lambda value: value >= rid))
    else:
        event.remove(event.overrides_where(lambda value: value == rid))
    remains = bool(event.overrides)
    master, series = event.master, event.series
    if master is None or series is None:
        return calendar if remains else None
    if this_and_future:
        remains |= event.end_before(master, series, rid)
    elif in_series:
        remains |= series.has_occurrence_besides(rid)
        master.add("EXDATE", event.form.props([rid]))
        _touch(master, now)
    else:
        remains = True
    return calendar if remains else None


def update_event(
    calendar: Calendar,
    uid: str,
    changes: dict[str, Any],
    recurrence_id: str | None,
    this_and_future: bool,
    local: tzinfo,
    now: datetime,
) -> EditResult:
    """Change an event, one of its occurrences or the occurrences from one on."""
    event = _EventObject(calendar, uid, local, now)
    master, series = event.master, event.series
    if recurrence_id is None:
        if master is None or series is None:
            raise EditError("occurrences_without_series")
        return _change_from(event, master, series, series.start, None, changes)
    rid = event.recurrence_id(recurrence_id)
    override, _ = event.find(rid)
    if not this_and_future and (series is None or series.recurs or event.overrides):
        return _change_occurrence(event, rid, override, changes)
    if master is None or series is None:
        # Occurrences without a main component have no rule to split
        raise EditError("occurrences_without_series")
    return _change_from(event, master, series, rid, override, changes)


def _change_occurrence(
    event: _EventObject, rid: Time, override: Event | None, changes: dict[str, Any]
) -> EditResult:
    """Change one occurrence of a recurring event."""
    rules = event.new_rules(changes, event.form)
    if rules is not None and not event.same_rules(rules):
        raise EditError("rrule_on_occurrence")
    if override is None:
        master = event.master
        if TYPE_CHECKING:
            # find() made sure that the series has the occurrence
            assert master is not None
        start, end = _span(master)
        override = copy.deepcopy(master)
        for name in ("RRULE", "RDATE", "EXDATE"):
            override.pop(name, None)
        _write_times(override, rid, rid + _between(start, end), event.form)
        override.add("RECURRENCE-ID", event.form.prop(rid))
        event.calendar.add_component(override)
        event.overrides.append(override)
    _set_fields(override, changes)
    start, end = _span(override)
    if not _same_times(changes, start, end, event.local):
        if isinstance(changes[DTSTART], datetime) != isinstance(start, datetime):
            raise EditError("all_day_change_not_supported")
        _write_times(
            override,
            changes[DTSTART],
            changes[DTEND],
            _Form.of(override["DTSTART"], event.local),
        )
    _touch(override, event.now)
    return EditResult(event.calendar)


def _change_from(
    event: _EventObject,
    master: Event,
    series: _Series,
    rid: Time,
    override: Event | None,
    changes: dict[str, Any],
) -> EditResult:
    """Change the occurrences of an event from one on, or the whole event.

    The occurrences before stay in the object, the others move to a new one.
    """
    if not series.recurs and not event.overrides:
        return _change_single(event, master, changes)
    form, local = event.form, event.local
    start, end = changes[DTSTART], changes[DTEND]
    master_start, master_end = _span(master)
    duration = _between(master_start, master_end)
    shown_start, shown_end = (
        _span(override) if override is not None else (rid, rid + duration)
    )
    moved = not _same_times(changes, shown_start, shown_end, local)
    delta: timedelta | None = None
    new_start, new_end = rid, rid + duration
    if moved:
        delta = form.delta(start, shown_start)
        new_start = form.shift(rid, delta)
        new_end = new_start + _between(start, end)
    rules = event.new_rules(changes, form)
    replaced = rules is not None and not event.same_rules(rules)
    old_text = {key: _text(master, key) for key in TEXT_FIELDS}

    split = series.has_occurrence_before(rid) or bool(
        event.overrides_where(lambda value: value < rid)
    )
    future = copy.deepcopy(master) if split else master
    future.pop("RRULE", None)
    if rules is not None and replaced:
        for recur in rules:
            future.add("RRULE", recur)
        future.pop("RDATE", None)
        future.pop("EXDATE", None)
    else:
        _move_rules(event, series, future, rid, delta, new_start)
    _write_times(future, new_start, new_end, form)
    changed = _set_fields(future, changes)

    later = event.overrides_where(lambda value: value >= rid)
    event.remove(later)
    kept = []
    for component in [] if replaced else later:
        if component is override:
            if moved:
                # The occurrence follows the change of the series
                continue
            _set_fields(component, changes)
        else:
            for key in changed:
                if _text(component, key) == old_text[key]:
                    _set_text(component, key, changes[key] or "")
        if delta:
            value = event.override_id(component)
            component["RECURRENCE-ID"] = form.prop(form.shift(value, delta))
        _touch(component, event.now)
        kept.append(component)
    _touch(future, event.now)

    if not split:
        for component in kept:
            event.calendar.add_component(component)
        return EditResult(event.calendar)

    event.end_before(master, series, rid)
    return EditResult(event.calendar, _new_calendar(event, [future, *kept]))


def _move_rules(
    event: _EventObject,
    series: _Series,
    future: Event,
    rid: Time,
    delta: timedelta | None,
    new_start: Time,
) -> None:
    """Write the rules and dates of a series that starts at an occurrence."""
    form = event.form
    for index, recur in enumerate(series.rules):
        if not series.rule_reaches(index, rid):
            continue
        recur = vRecur(recur)
        if rid != series.start:
            if not series.rule_contains(index, rid):
                # The rule would move to the day of the added date
                raise EditError("split_at_added_date")
            if count := recur.get("COUNT"):
                recur["COUNT"] = [count[0] - series.rule_count_before(index, rid)]
        if delta:
            if until := recur.get("UNTIL"):
                recur["UNTIL"] = [form.until(form.shift(form.until(until[0]), delta))]
            if not series.rule_starts_at(recur, new_start):
                raise EditError("start_not_on_rule")
        future.add("RRULE", recur)
    # The occurrence the series starts at is its DTSTART now
    event.edit_times(future, "RDATE", lambda value: value > rid, delta)
    event.edit_times(future, "EXDATE", lambda value: value >= rid, delta)


def _new_calendar(event: _EventObject, components: list[Event]) -> Calendar:
    """Return a new object for the components split off an event."""
    new_uid = str(uuid4())
    for component in components:
        component.pop("UID")
        component.add("UID", new_uid)
    future = components[0]
    future.pop("CREATED", None)
    future.add("CREATED", event.now)
    future.pop("SEQUENCE")
    future.add("SEQUENCE", 0)
    calendar = Calendar()
    for name, value in event.calendar.items():
        calendar[name] = copy.deepcopy(value)
    for component in event.calendar.subcomponents:
        if component.name == "VTIMEZONE":
            calendar.add_component(copy.deepcopy(component))
    for component in components:
        calendar.add_component(component)
    return calendar


def _add_timezone(calendar: Calendar, form: _Form) -> None:
    """Add the VTIMEZONE of a form's TZID if the object has none."""
    if form.tzid is None or any(
        component.name == "VTIMEZONE" and str(component.get("TZID")) == form.tzid
        for component in calendar.subcomponents
    ):
        return
    calendar.subcomponents.insert(0, Timezone.from_tzid(form.tzid))


def _change_single(
    event: _EventObject, master: Event, changes: dict[str, Any]
) -> EditResult:
    """Change an event that doesn't recur, which may make it recur."""
    _set_fields(master, changes)
    start, end = changes[DTSTART], changes[DTEND]
    shown_start, shown_end = _span(master)
    form = _Form.of(master["DTSTART"], event.local)
    if not _same_times(changes, shown_start, shown_end, event.local):
        if isinstance(start, datetime) != isinstance(shown_start, datetime):
            form = _Form.like(start, event.local)
            _add_timezone(event.calendar, form)
        _write_times(master, start, end, form)
    if (rules := event.new_rules(changes, form)) is not None:
        master.pop("RRULE", None)
        for recur in rules:
            master.add("RRULE", recur)
    _touch(master, event.now)
    return EditResult(event.calendar)
