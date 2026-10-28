"""Bounded parsing worker. Receives calendar data only, never credentials."""

from datetime import datetime, time, timedelta
import json
import sys
import math
from zoneinfo import ZoneInfo

from icalendar import Calendar
import recurring_ical_events


def check_complexity(cal, first, last):
    """Reject dense recurrence before the library allocates an occurrence list."""
    events = cal.walk("VEVENT")
    if len(events) > 2000:
        raise ValueError("Too many event components")
    estimate = 0
    for event in events:
        estimate += 1
        for key in ("RDATE", "EXDATE"):
            values = event.get(key, [])
            values = values if isinstance(values, list) else [values]
            estimate += sum(len(value.dts) for value in values)
        rule = event.get("RRULE")
        if rule is None:
            continue
        if isinstance(rule, list):
            raise ValueError("Multiple recurrence rules are unsupported")
        freq = str(rule.get("FREQ", [""])[0])
        seconds = {"HOURLY": 3600, "DAILY": 86400, "WEEKLY": 604800,
                   "MONTHLY": 28 * 86400, "YEARLY": 365 * 86400}.get(freq)
        if seconds is None:
            raise ValueError("Sub-hourly or unknown recurrence frequency is unsupported")
        interval = int(rule.get("INTERVAL", [1])[0])
        if interval < 1:
            raise ValueError("Invalid recurrence interval")
        start = event.decoded("DTSTART")
        end = event.decoded("DTEND", start)
        duration = event.decoded("DURATION", end - start).total_seconds()
        if duration < 0 or duration > 366 * 86400:
            raise ValueError("Unsupported recurring event duration")
        # BY filters may reduce the count; conservatively treating them as expansions
        # avoids underestimating hostile combinations at the cost of rejecting some rules.
        combinations = 1
        for key, values in rule.items():
            if key.startswith("BY"):
                combinations *= max(1, len(values))
        estimate += (math.ceil(((last - first).total_seconds() + duration) / (seconds * interval)) + 2) * combinations
        if estimate > 2000:
            raise ValueError("Recurrence is too dense; reduce the date range")
    if estimate > 2000:
        raise ValueError("Too many recurrence values")


def expand(payload):
    zone = ZoneInfo(payload["zone"])
    first, last = datetime.fromisoformat(payload["first"]), datetime.fromisoformat(payload["last"])
    def as_datetime(value):
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=zone)
        return datetime.combine(value, time.min, zone)

    def render(value):
        return as_datetime(value).astimezone(zone).isoformat() if isinstance(value, datetime) else value.isoformat()

    items = []
    seen = set()
    count = 0
    for data in payload["calendars"]:
        cal = Calendar.from_ical(data)
        if any(component.errors for component in cal.walk()):
            raise ValueError("Invalid calendar")
        check_complexity(cal, first, last)
        for event in recurring_ical_events.of(cal).between(first, last):
            count += 1
            if count > 2000:
                raise ValueError("Too many occurrences")
            begin = event.decoded("DTSTART")
            finish = event.decoded("DTEND", None)
            if finish is None:
                finish = begin + event.decoded("DURATION", timedelta(0) if isinstance(begin, datetime) else timedelta(days=1))
            # Explicit half-open boundaries, including overlapping and instantaneous events.
            if as_datetime(begin) >= last or (as_datetime(finish) <= first and as_datetime(begin) != first):
                continue
            uid = str(event.get("UID", ""))
            if not uid:
                raise ValueError("Missing UID")
            if str(event.get("STATUS", "")).upper() == "CANCELLED":
                continue
            identity = (uid, render(begin), render(finish))
            if identity in seen:
                continue
            seen.add(identity)
            item = {"calendar_id": payload["calendar_id"], "uid": uid,
                    "start": render(begin), "end": render(finish),
                    "all_day": not isinstance(begin, datetime),
                    "availability": "free" if str(event.get("TRANSP", "OPAQUE")).upper() == "TRANSPARENT" else "busy",
                    "status": str(event.get("STATUS", "")), "start_sort": as_datetime(begin).timestamp()}
            for field, max_chars in (("summary", 2048), ("description", 16384), ("location", 2048)):
                value = str(event.get(field.upper(), ""))
                item[field] = value[:max_chars]
                if len(value) > max_chars:
                    item[field + "_truncated"] = True
            items.append(item)
    return items


if __name__ == "__main__":
    # Prevent unbounded memory growth in recurrence libraries (Linux supports RLIMIT_AS).
    if sys.platform.startswith("linux"):
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    try:
        print(json.dumps(expand(json.load(sys.stdin))))
    except Exception:
        sys.exit(1)
