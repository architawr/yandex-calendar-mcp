"""Optional mutations, isolated from the reader's read-only HTTP boundary."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import unquote
from uuid import uuid4
from xml.etree.ElementTree import Element, SubElement

import httpx
from icalendar import Calendar, Event

from .caldav import CAL, DAV, CalendarError, Reader, dav_rows, safe_url

FIELDS = {"summary", "start", "end", "description", "location"}


@dataclass
class Draft:
    operation: str
    calendar: str
    uid: str
    href: str
    etag: str | None
    before: bytes | None
    after: bytes | None
    expires: float


def single_event(raw, owner=None):
    try:
        cal = Calendar.from_ical(raw)
        events = cal.walk("VEVENT")
        if len(events) != 1 or any(c.errors for c in cal.walk()):
            raise ValueError()
        event = events[0]
        organizer = str(event.get("ORGANIZER", "")).removeprefix("mailto:").casefold()
        if any(key in event for key in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID", "ATTENDEE")) or (
            organizer and (not owner or organizer != owner.casefold())
        ):
            raise CalendarError("Only single events without participants or recurrence can be changed.")
        if not event.get("UID") or not event.get("DTSTART"):
            raise ValueError()
        return cal, event
    except CalendarError:
        raise
    except Exception:
        raise CalendarError("Invalid event data; mutation refused.") from None


def view(raw, owner=None):
    if raw is None:
        return None
    _, event = single_event(raw, owner)
    result = {key: str(event.get(key.upper(), "")) for key in ("summary", "description", "location")}
    result["uid"] = str(event["UID"])
    result["availability"] = "free" if str(event.get("TRANSP", "OPAQUE")).upper() == "TRANSPARENT" else "busy"
    for field, tag in (("start", "DTSTART"), ("end", "DTEND")):
        value = event.decoded(tag, None)
        result[field] = value.isoformat() if value is not None else None
    return result


def equivalent(first, second, owner=None, zone=timezone.utc):
    """Servers may normalize timestamp offsets without changing the event."""
    left, right = view(first, owner), view(second, owner)
    if left is None or right is None:
        return left == right
    for obj in (left, right):
        for key in ("start", "end"):
            value = obj[key]
            if value and len(value) > 10:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=zone)
                obj[key] = parsed.astimezone(timezone.utc).isoformat()
    return left == right


class Writer:
    def __init__(self, reader: Reader, allowed=(), trash: Path | None = None):
        self.reader = reader
        self.allowed = frozenset(safe_url(x) for x in allowed)
        self.trash = trash or Path(".calendar-trash")
        self.drafts = {}
        self.lock = threading.Lock()

    def authorize(self, calendar):
        target = safe_url(calendar)
        if not target.endswith("/") or target not in self.allowed:
            raise CalendarError("Calendar is not explicitly allowed for writes (allowed_write_calendars).")
        if target not in {c["calendar_id"] for c in self.reader.calendars()}:
            raise CalendarError("Calendar is not available for this account.")
        return target

    def lookup(self, calendar, uid):
        root = Element(f"{{{CAL}}}calendar-query")
        prop = SubElement(root, f"{{{DAV}}}prop")
        SubElement(prop, f"{{{DAV}}}getetag")
        SubElement(prop, f"{{{CAL}}}calendar-data")
        filt = SubElement(root, f"{{{CAL}}}filter")
        outer = SubElement(filt, f"{{{CAL}}}comp-filter", {"name": "VCALENDAR"})
        inner = SubElement(outer, f"{{{CAL}}}comp-filter", {"name": "VEVENT"})
        uid_filter = SubElement(inner, f"{{{CAL}}}prop-filter", {"name": "UID"})
        SubElement(uid_filter, f"{{{CAL}}}text-match", {"collation": "i;octet"}).text = uid
        matches = []
        for href, props in dav_rows(self.reader.request("REPORT", calendar, root, depth="1")):
            data = props.get(f"{{{CAL}}}calendar-data")
            if data is None or not data.text:
                continue
            try:
                cal = Calendar.from_ical(data.text)
                events = cal.walk("VEVENT")
            except Exception:
                raise CalendarError("Invalid calendar response.") from None
            if not any(str(event.get("UID", "")) == uid for event in events):
                continue
            raw = data.text.encode()
            single_event(raw, self.reader.username)
            url = safe_url(href)
            if not url.startswith(calendar):
                raise CalendarError("Event URL is outside the selected calendar.")
            tail = unquote(url[len(calendar):])
            if not tail or "/" in tail or "\\" in tail or tail in (".", ".."):
                raise CalendarError("Invalid event resource URL.")
            etag_node = props.get(f"{{{DAV}}}getetag")
            etag = etag_node.text if etag_node is not None else None
            if not etag or etag.startswith("W/"):
                raise CalendarError("A strong ETag is required for safe mutation.")
            matches.append((url, etag, raw))
        if len(matches) > 1:
            raise CalendarError("Ambiguous event UID; mutation refused.")
        return matches[0] if matches else None

    def prepare(self, operation, calendar_id, uid=None, changes=None):
        if operation not in {"create", "update", "delete"}:
            raise CalendarError("Operation must be create, update or delete.")
        changes = changes or {}
        if set(changes) - FIELDS or any(not isinstance(x, str) for x in changes.values()):
            raise CalendarError("Only string summary/start/end/description/location fields are supported.")
        if any(len(value) > 16384 for value in changes.values()):
            raise CalendarError("Event field exceeds 16384 characters.")
        target = self.authorize(calendar_id)
        before = None
        etag = None
        if operation == "create":
            if uid is not None or not {"summary", "start", "end"} <= changes.keys():
                raise CalendarError("Create requires summary/start/end; UID is generated by the server.")
            uid = str(uuid4())
            href = target + uid + ".ics"
            cal = Calendar()
            cal.add("prodid", "-//local-yandex-calendar-mcp//EN")
            cal.add("version", "2.0")
            event = Event()
            event.add("uid", uid)
            availability = self.reader.calendar_profiles.get(target, {}).get("default_availability", "busy")
            event.add("transp", "TRANSPARENT" if availability == "free" else "OPAQUE")
            cal.add_component(event)
        else:
            if not uid or len(uid) > 1024:
                raise CalendarError("An event UID is required.")
            found = self.lookup(target, uid)
            if found is None:
                raise CalendarError("Event not found.")
            href, etag, before = found
            cal, event = single_event(before, self.reader.username)
            if operation == "delete" and changes:
                raise CalendarError("Delete does not accept changes.")
            if operation == "update" and not changes:
                raise CalendarError("Update requires at least one changed field.")
        after = None
        if operation != "delete":
            for key, value in changes.items():
                tag = {"start": "DTSTART", "end": "DTEND"}.get(key, key.upper())
                if key in ("start", "end"):
                    try:
                        value = date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
                        if isinstance(value, datetime) and value.tzinfo is None:
                            raise ValueError()
                        if isinstance(value, datetime):
                            # Fixed-offset tzinfo is otherwise serialized as floating time by icalendar.
                            value = value.astimezone(timezone.utc)
                    except ValueError:
                        raise CalendarError("Use ISO dates or timestamps with explicit UTC offsets.") from None
                event.pop(tag, None)
                event.add(tag, value)
            start, end = event.decoded("DTSTART"), event.decoded("DTEND", None)
            if type(start) is not type(end) or end <= start:
                raise CalendarError("Start/end must have the same date type and end must be after start.")
            event.pop("DURATION", None)
            sequence = int(event.get("SEQUENCE", 0)) + 1
            event.pop("SEQUENCE", None)
            event.add("SEQUENCE", sequence)
            event.pop("DTSTAMP", None)
            event.add("DTSTAMP", datetime.now(timezone.utc))
            after = cal.to_ical()
            single_event(after, self.reader.username)
        token = secrets.token_urlsafe(32)
        draft = Draft(operation, target, uid, href, etag, before, after, time.monotonic() + 300)
        with self.lock:
            self.drafts = {k: v for k, v in self.drafts.items() if v.expires > time.monotonic()}
            if len(self.drafts) >= 128:
                raise CalendarError("Too many pending changes; wait for expiry.")
            self.drafts[token] = draft
        return {"change_id": token, "expires_in_seconds": 300, "operation": operation,
                "calendar_id": target, "uid": uid, "before": view(before, self.reader.username), "after": view(after, self.reader.username),
                "timezone": str(self.reader.zone), "requires_user_approval": True, "content_is_untrusted": True}

    def apply(self, change_id):
        with self.lock:
            draft = self.drafts.pop(change_id, None)
        if draft is None or draft.expires <= time.monotonic():
            raise CalendarError("Change is expired, unknown or already consumed. Prepare a new change.")
        self.authorize(draft.calendar)
        backup = None
        if draft.before is not None:
            self.trash.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.trash.chmod(0o700)
            backup = self.trash / (str(uuid4()) + ".ics")
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(draft.before)
                stream.flush()
                os.fsync(stream.fileno())
        method = "DELETE" if draft.operation == "delete" else "PUT"
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        headers.update({"If-None-Match": "*"} if draft.operation == "create" else {"If-Match": draft.etag})
        try:
            with self.reader.http.stream(method, safe_url(draft.href), content=draft.after, headers=headers) as response:
                status = response.status_code
            if status == 412:
                raise CalendarError("Conflict: event changed or already exists. Re-read and prepare again.")
            if status not in (200, 201, 204):
                raise CalendarError(f"Write returned HTTP {status}. Re-read UID {draft.uid} before retrying.")
        except httpx.HTTPError:
            raise CalendarError(f"Write outcome is unknown. Re-read UID {draft.uid}; do not blindly repeat creation.") from None
        try:
            found = self.lookup(draft.calendar, draft.uid)
            verified = found is None if draft.operation == "delete" else found is not None and equivalent(found[2], draft.after, self.reader.username, self.reader.zone)
        except Exception:
            verified = False
        if not verified:
            raise CalendarError(f"Write was accepted but verification failed. Re-read UID {draft.uid} before retrying.")
        return {"operation": draft.operation, "uid": draft.uid, "verified": True,
                "backup_file": str(backup) if backup else None}
