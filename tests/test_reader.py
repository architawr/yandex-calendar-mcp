import base64
from datetime import datetime, timezone
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import httpx
import pytest

from yandex_calendar_mcp.caldav import CalendarError, Reader, dav_rows, safe_url, window

CALENDAR = "https://caldav.yandex.ru/calendars/test/events-1/"


def multistatus(content):
    return f'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">{content}</d:multistatus>'


def row(href, prop):
    return f'<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop>{prop}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'


def make_reader(ics="", allowed=(), report_status=207):
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url.host == "caldav.yandex.ru"
        assert request.url.scheme == "https"
        assert request.headers["authorization"] == "Basic " + base64.b64encode(b"test@yandex.ru:fixture-secret").decode()
        assert request.method in {"PROPFIND", "REPORT", "GET"}
        if request.method == "REPORT":
            data = row("/calendars/test/events-1/a.ics", f'<c:calendar-data>{escape(ics)}</c:calendar-data>')
            return httpx.Response(report_status, text=multistatus(data))
        if request.url.path == "/":
            data = row("/", '<d:current-user-principal><d:href>/principals/test/</d:href></d:current-user-principal>')
        elif request.url.path == "/principals/test/":
            data = row("/principals/test/", '<c:calendar-home-set><d:href>/calendars/test/</d:href></c:calendar-home-set>')
        else:
            data = row("/calendars/test/events-1/", '<d:displayname>Работа</d:displayname><d:resourcetype><c:calendar/></d:resourcetype>')
        return httpx.Response(207, text=multistatus(data))
    return Reader("test@yandex.ru", "fixture-secret", allowed=allowed, transport=httpx.MockTransport(handle)), requests


def calendar(*events):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n" + "\r\n".join(events) + "\r\nEND:VCALENDAR\r\n"


def event(lines):
    return "BEGIN:VEVENT\r\n" + lines.replace("\n", "\r\n") + "\r\nEND:VEVENT"


@pytest.mark.parametrize("href", ["https://evil.example/x", "http://caldav.yandex.ru/x", "//evil.example/x",
    "https://caldav.yandex.ru:8443/x", "https://u:p@caldav.yandex.ru/x", "/x?token=secret", "/x#frag", "\\evil", "/\nx"])
def test_rejects_unsafe_hrefs(href):
    with pytest.raises(CalendarError):
        safe_url(href)


@pytest.mark.parametrize("method", ["PUT", "DELETE", "POST", "MKCALENDAR", "PROPPATCH", "MOVE", "COPY"])
def test_network_boundary_rejects_every_write(method):
    reader, requests = make_reader()
    with pytest.raises(CalendarError, match="Write methods"):
        reader.request(method, CALENDAR)
    assert not requests


def test_discovery_and_calendar_allowlist():
    reader, _ = make_reader()
    assert reader.calendars() == [{"calendar_id": CALENDAR, "name": "Работа"}]
    reader, requests = make_reader(allowed=("/calendars/test/private/",))
    assert reader.calendars() == []
    with pytest.raises(CalendarError, match="excluded"):
        reader.events(CALENDAR, "2026-10-25", "2026-10-26")
    assert all(r.method != "REPORT" for r in requests)


def test_redirects_do_not_forward_credentials():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/"})
    reader = Reader("user", "secret", transport=httpx.MockTransport(handle))
    with pytest.raises(CalendarError, match="redirects are disabled"):
        reader.request("PROPFIND", "/")
    assert len(calls) == 1


def test_hostile_discovery_does_not_leak_password():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(207, text=multistatus(row("/", '<d:current-user-principal><d:href>https://evil.example/</d:href></d:current-user-principal>')))
    reader = Reader("user", "secret", transport=httpx.MockTransport(handle))
    with pytest.raises(CalendarError):
        reader.calendars()
    assert len(calls) == 1


def test_xml_external_entities_are_rejected():
    with pytest.raises(CalendarError, match="unsafe"):
        dav_rows(b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><d:multistatus xmlns:d="DAV:">&xxe;</d:multistatus>')


def test_upstream_errors_hide_bodies_and_secrets():
    reader = Reader("user", "secret", transport=httpx.MockTransport(lambda _: httpx.Response(500, text="sensitive response secret")))
    with pytest.raises(CalendarError) as result:
        reader.request("REPORT", CALENDAR)
    assert "sensitive" not in str(result.value) and "secret" not in str(result.value)


def test_response_size_is_bounded(monkeypatch):
    monkeypatch.setattr("yandex_calendar_mcp.caldav.MAX_BYTES", 10)
    reader = Reader("user", "secret", transport=httpx.MockTransport(lambda _: httpx.Response(207, content=b"x" * 20)))
    with pytest.raises(CalendarError, match="exceeds"):
        reader.request("REPORT", CALENDAR)


@pytest.mark.parametrize("start,end", [("2026-10-28", "2026-10-28"), ("2026-01-01", "2026-12-31"), ("2026-10-28T12:00:00", "2026-10-29"), ("bad", "2026-10-29")])
def test_invalid_windows(start, end):
    with pytest.raises(CalendarError):
        window(start, end, ZoneInfo("Europe/Podgorica"))


def test_dates_respect_local_timezone_and_dst():
    first, last = window("2026-10-25", "2026-10-26", ZoneInfo("Europe/Podgorica"))
    assert (last - first).total_seconds() == 25 * 3600


def test_recurrence_exceptions_all_day_overlap_and_exclusive_end():
    ics = calendar(
        event("UID:repeat\nDTSTART;TZID=Europe/Podgorica:20261024T090000\nDTEND;TZID=Europe/Podgorica:20261024T100000\nRRULE:FREQ=DAILY;COUNT=4\nEXDATE;TZID=Europe/Podgorica:20261025T090000\nSUMMARY:Daily"),
        event("UID:repeat\nRECURRENCE-ID;TZID=Europe/Podgorica:20261026T090000\nDTSTART;TZID=Europe/Podgorica:20261026T110000\nDTEND;TZID=Europe/Podgorica:20261026T120000\nSUMMARY:Moved"),
        event("UID:all\nDTSTART;VALUE=DATE:20261025\nDTEND;VALUE=DATE:20261027\nSUMMARY:Holiday"),
        event("UID:overlap\nDTSTART:20261023T230000Z\nDTEND:20261024T230000Z\nSUMMARY:Overlapping"),
        event("UID:boundary\nDTSTART;TZID=Europe/Podgorica:20261027T000000\nDTEND;TZID=Europe/Podgorica:20261027T010000"),
    )
    reader, requests = make_reader(ics)
    result = reader.events(CALENDAR, "2026-10-24", "2026-10-27")
    assert result["matched"] == 4
    assert not result["truncated"]
    assert any(x["uid"] == "overlap" for x in result["events"])
    assert any(x["all_day"] and x["start"] == "2026-10-25" for x in result["events"])
    repeats = [x for x in result["events"] if x["uid"] == "repeat"]
    assert [x["start"] for x in repeats] == ["2026-10-24T09:00:00+02:00", "2026-10-26T11:00:00+01:00"]
    assert requests[-1].method == "REPORT"
    assert b'20261023T220000Z' in requests[-1].content


def test_truncation_is_explicit():
    reader, _ = make_reader(calendar(event("UID:a\nDTSTART:20261028T100000Z\nRRULE:FREQ=HOURLY;COUNT=4")))
    result = reader.events(CALENDAR, "2026-10-28", "2026-10-29", limit=2)
    assert len(result["events"]) == 2 and result["truncated"] and result["matched"] == 4


def test_invalid_calendar_fails_closed():
    reader, _ = make_reader("not a calendar")
    with pytest.raises(CalendarError, match="schedule was not returned"):
        reader.events(CALENDAR, "2026-10-28", "2026-10-29")


def test_worker_timeout_fails_closed(monkeypatch):
    import subprocess
    def timeout(*args, **kwargs):
        assert "fixture-secret" not in kwargs["input"].decode()
        assert "YANDEX_CALENDAR_USERNAME" not in kwargs["env"]
        raise subprocess.TimeoutExpired("worker", 10)
    monkeypatch.setattr(subprocess, "run", timeout)
    reader, _ = make_reader()
    with pytest.raises(CalendarError, match="exceeded limits"):
        reader.events(CALENDAR, "2026-10-28", "2026-10-29")
