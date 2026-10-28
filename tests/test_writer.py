import time

import httpx
import pytest

from test_reader import CALENDAR, calendar, event, multistatus, row
from xml.sax.saxutils import escape
from yandex_calendar_mcp.caldav import CalendarError, Reader
from yandex_calendar_mcp.writer import Writer, equivalent


def setup(tmp_path, initial=None, allowed=True):
    state = {"raw": initial, "etag": '"v1"', "conflict": False, "verify_wrong": False}
    calls = []
    def handle(req):
        calls.append(req)
        if req.method == "PUT":
            if state["conflict"]:
                return httpx.Response(412)
            assert req.url.host == "caldav.yandex.ru"
            if state["raw"] is None:
                assert req.headers["if-none-match"] == "*"
            else:
                assert req.headers["if-match"] == state["etag"]
            if not state["verify_wrong"]:
                state["raw"] = req.content.decode()
            state["etag"] = '"v2"'
            return httpx.Response(201)
        if req.method == "DELETE":
            assert req.headers["if-match"] == state["etag"]
            state["raw"] = None
            return httpx.Response(204)
        assert req.method == "REPORT"
        content = "" if state["raw"] is None else row(CALENDAR + "fixture.ics", '<d:getetag>' + escape(state["etag"]) + '</d:getetag><c:calendar-data>' + escape(state["raw"]) + '</c:calendar-data>')
        return httpx.Response(207, text=multistatus(content))
    reader = Reader("test", "secret", transport=httpx.MockTransport(handle))
    reader.calendars = lambda: [{"calendar_id": CALENDAR, "name": "Fixture"}]
    return Writer(reader, [CALENDAR] if allowed else [], tmp_path / "trash"), calls, state


def original(extra=""):
    return calendar(event("UID:fixture\nDTSTART:20261028T100000Z\nDTEND:20261028T110000Z\nSUMMARY:Original\nX-CUSTOM:preserve\n" + extra))


def test_verification_accepts_timezone_normalization():
    from icalendar import Calendar
    from datetime import timedelta, timezone
    from zoneinfo import ZoneInfo
    cal = Calendar.from_ical(original())
    item = cal.walk("VEVENT")[0]
    for key in ("DTSTART", "DTEND"):
        value = item.decoded(key).astimezone(timezone(timedelta(hours=1)))
        item.pop(key)
        item.add(key, value)
    assert equivalent(original().encode(), cal.to_ical(), zone=ZoneInfo("Europe/Podgorica"))


def test_yandex_added_owner_organizer_can_be_verified_and_updated(tmp_path):
    raw = original("ORGANIZER:mailto:test")
    assert equivalent(original().encode(), raw.encode(), owner="test")
    writer, calls, state = setup(tmp_path, raw)
    preview = writer.prepare("update", CALENDAR, "fixture", {"summary": "Changed"})
    assert writer.apply(preview["change_id"])["verified"]


def create(writer):
    return writer.prepare("create", CALENDAR, changes={"summary": "New", "start": "2026-10-28T10:00:00+01:00", "end": "2026-10-28T11:00:00+01:00"})


def test_default_denies_all_mutations(tmp_path):
    writer, calls, _ = setup(tmp_path, allowed=False)
    with pytest.raises(CalendarError, match="explicitly allowed"):
        create(writer)
    assert calls == []


def test_create_preview_and_single_use_apply(tmp_path):
    writer, calls, state = setup(tmp_path)
    preview = create(writer)
    assert calls == [] and preview["before"] is None
    assert preview["after"]["start"] == "2026-10-28T09:00:00+00:00"
    result = writer.apply(preview["change_id"])
    assert result["verified"] and [x.method for x in calls] == ["PUT", "REPORT"]
    with pytest.raises(CalendarError, match="already consumed"):
        writer.apply(preview["change_id"])


@pytest.mark.parametrize("availability,transparency", [("free", "TRANSPARENT"), ("busy", "OPAQUE")])
def test_new_event_uses_calendar_availability(tmp_path, availability, transparency):
    from icalendar import Calendar
    writer, calls, state = setup(tmp_path)
    writer.reader.calendar_profiles = {CALENDAR: {"default_availability": availability, "description": "Task calendar"}}
    preview = create(writer)
    assert preview["after"]["availability"] == availability
    writer.apply(preview["change_id"])
    assert str(Calendar.from_ical(state["raw"]).walk("VEVENT")[0]["TRANSP"]) == transparency


def test_update_preserves_custom_fields_and_backs_up(tmp_path):
    writer, calls, state = setup(tmp_path, original())
    preview = writer.prepare("update", CALENDAR, "fixture", {"summary": "Changed"})
    assert all(x.method == "REPORT" for x in calls)
    result = writer.apply(preview["change_id"])
    assert result["verified"] and "X-CUSTOM:preserve" in state["raw"]
    from pathlib import Path
    backup = Path(result["backup_file"])
    assert backup.read_text().replace("\n", "\r\n") == original()
    assert backup.stat().st_mode & 0o777 == 0o600


def test_delete_verifies_absence(tmp_path):
    writer, calls, state = setup(tmp_path, original())
    preview = writer.prepare("delete", CALENDAR, "fixture")
    assert writer.apply(preview["change_id"])["verified"]
    assert state["raw"] is None


@pytest.mark.parametrize("extra", ["RRULE:FREQ=DAILY;COUNT=3", "ATTENDEE:mailto:a@example.com", "ORGANIZER:mailto:a@example.com", "RECURRENCE-ID:20261028T100000Z"])
def test_rejects_participants_and_recurrence(tmp_path, extra):
    writer, calls, _ = setup(tmp_path, original(extra))
    with pytest.raises(CalendarError, match="participants or recurrence"):
        writer.prepare("delete", CALENDAR, "fixture")
    assert all(x.method == "REPORT" for x in calls)


def test_conflict_is_not_retried(tmp_path):
    writer, calls, state = setup(tmp_path, original())
    preview = writer.prepare("update", CALENDAR, "fixture", {"summary": "Changed"})
    state["conflict"] = True
    with pytest.raises(CalendarError, match="Conflict"):
        writer.apply(preview["change_id"])
    assert sum(x.method == "PUT" for x in calls) == 1
    assert "SUMMARY:Original" in state["raw"]


def test_expired_draft_does_not_write(tmp_path):
    writer, calls, _ = setup(tmp_path)
    preview = create(writer)
    writer.drafts[preview["change_id"]].expires = time.monotonic() - 1
    with pytest.raises(CalendarError, match="expired"):
        writer.apply(preview["change_id"])
    assert not calls


def test_revoked_calendar_permission_does_not_write(tmp_path):
    writer, calls, _ = setup(tmp_path)
    preview = create(writer)
    writer.allowed = frozenset()
    with pytest.raises(CalendarError, match="explicitly allowed"):
        writer.apply(preview["change_id"])
    assert not calls


def test_readback_mismatch_is_error(tmp_path):
    writer, calls, state = setup(tmp_path)
    preview = create(writer)
    state["verify_wrong"] = True
    with pytest.raises(CalendarError, match="verification failed"):
        writer.apply(preview["change_id"])


@pytest.mark.parametrize("changes", [{"attendees": "x"}, {"start": "2026-10-28T12:00:00"}, {"end": "2026-10-27T10:00:00Z"}])
def test_invalid_updates_cannot_write(tmp_path, changes):
    writer, calls, _ = setup(tmp_path, original())
    with pytest.raises(CalendarError):
        writer.prepare("update", CALENDAR, "fixture", changes)
    assert all(x.method == "REPORT" for x in calls)


def test_network_uncertainty_consumes_token(tmp_path):
    writer, calls, state = setup(tmp_path)
    preview = create(writer)
    def failure(request):
        raise httpx.ReadTimeout("hidden upstream details")
    writer.reader.http.close()
    writer.reader.http = httpx.Client(transport=httpx.MockTransport(failure))
    with pytest.raises(CalendarError, match="outcome is unknown") as error:
        writer.apply(preview["change_id"])
    assert "hidden upstream" not in str(error.value)
    with pytest.raises(CalendarError, match="already consumed"):
        writer.apply(preview["change_id"])


def test_missing_etag_cannot_be_prepared(tmp_path):
    writer, calls, state = setup(tmp_path, original())
    state["etag"] = ""
    with pytest.raises(CalendarError, match="strong ETag"):
        writer.prepare("delete", CALENDAR, "fixture")
    assert all(x.method == "REPORT" for x in calls)
