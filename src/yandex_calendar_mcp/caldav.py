"""Small DAV reader with a fail-closed network boundary."""

from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from urllib.parse import urljoin, urlsplit
from xml.etree.ElementTree import Element, SubElement, tostring
from zoneinfo import ZoneInfo

import httpx
from defusedxml.ElementTree import fromstring

ORIGIN = "https://caldav.yandex.ru"
DAV = "DAV:"
CAL = "urn:ietf:params:xml:ns:caldav"
MAX_BYTES = 8 * 1024 * 1024
READ_METHODS = frozenset({"PROPFIND", "REPORT", "GET"})


class CalendarError(Exception):
    """Safe error message, never includes credentials or upstream response body."""


def safe_url(href: str) -> str:
    if not href or any(ord(c) < 32 for c in href) or "\\" in href:
        raise CalendarError("Invalid DAV path.")
    url = urljoin(ORIGIN + "/", href)
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https" and parsed.hostname == "caldav.yandex.ru"
            and parsed.port in (None, 443) and parsed.username is None
            and parsed.password is None and not parsed.query and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise CalendarError("DAV URL must use https://caldav.yandex.ru:443 without credentials or query.")
    return url


def window(start: str, end: str, zone: ZoneInfo) -> tuple[datetime, datetime]:
    def parse(value: str) -> datetime:
        try:
            result = datetime.fromisoformat(value)
        except ValueError:
            raise CalendarError("Use ISO 8601 dates or timestamps.") from None
        if result.tzinfo is None:
            # Bare dates mean midnight in the configured zone. Timed values need an offset,
            # avoiding ambiguous/nonexistent wall times around DST transitions.
            if len(value) != 10:
                raise CalendarError("Timestamps require a UTC offset; bare YYYY-MM-DD dates are allowed.")
            result = result.replace(tzinfo=zone)
        return result.astimezone(timezone.utc)

    first, last = parse(start), parse(end)
    if not timedelta(0) < last - first <= timedelta(days=92):
        raise CalendarError("Range must be positive and at most 92 days (end exclusive).")
    return first, last


def xml_request(name: str, props: list[str]) -> Element:
    root = Element(f"{{{DAV}}}{name}")
    prop = SubElement(root, f"{{{DAV}}}prop")
    for tag in props:
        SubElement(prop, tag)
    return root


def dav_rows(data: bytes) -> list[tuple[str, dict]]:
    try:
        root = fromstring(data)
        if root.tag != f"{{{DAV}}}multistatus":
            raise ValueError("not multistatus")
        rows = []
        for response in root.findall(f"{{{DAV}}}response"):
            href = response.findtext(f"{{{DAV}}}href", "")
            props = {}
            for propstat in response.findall(f"{{{DAV}}}propstat"):
                status = propstat.findtext(f"{{{DAV}}}status", "").split()
                if len(status) >= 2 and status[1] == "200":
                    prop = propstat.find(f"{{{DAV}}}prop")
                    if prop is not None:
                        props.update({child.tag: child for child in prop})
            if props:
                rows.append((href, props))
        return rows
    except Exception:
        raise CalendarError("Invalid or unsafe CalDAV XML response.") from None


class Reader:
    def __init__(self, username: str, password: str, zone: str = "Europe/Podgorica",
                 allowed: tuple[str, ...] = (), transport=None):
        self.username = username
        self.zone = ZoneInfo(zone)
        self.allowed = frozenset(safe_url(value) for value in allowed)
        self.calendar_profiles = {}
        self.http = httpx.Client(
            auth=(username, password), timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False, trust_env=False, transport=transport,
            headers={"User-Agent": "local-yandex-calendar-mcp/0.1.0"},
        )

    def close(self):
        self.http.close()

    def request(self, method: str, href: str, body: Element | None = None, depth="0") -> bytes:
        # This is the only outbound request path. Keep the allowlist even if new tools appear.
        if method not in READ_METHODS:
            raise CalendarError("Write methods are disabled in this server.")
        url = safe_url(href)
        try:
            with self.http.stream(method, url, content=tostring(body) if body is not None else None,
                                  headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"}) as response:
                if response.status_code in (401, 403):
                    raise CalendarError("Yandex denied access. Check the Calendar app password and account permissions.")
                if response.status_code not in (200, 207):
                    raise CalendarError(f"CalDAV returned HTTP {response.status_code}; redirects are disabled.")
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise CalendarError("CalDAV response exceeds 8 MiB; reduce the date range.")
                return bytes(data)
        except httpx.HTTPError:
            raise CalendarError("CalDAV network/TLS request failed; credentials and response details are hidden.") from None

    def calendars(self) -> list[dict]:
        root = xml_request("propfind", [f"{{{DAV}}}current-user-principal"])
        rows = dav_rows(self.request("PROPFIND", "/", root))
        principal = self._property_href(rows, f"{{{DAV}}}current-user-principal")
        rows = dav_rows(self.request("PROPFIND", principal,
                                   xml_request("propfind", [f"{{{CAL}}}calendar-home-set"])))
        home = self._property_href(rows, f"{{{CAL}}}calendar-home-set")
        rows = dav_rows(self.request("PROPFIND", home, xml_request("propfind", [
            f"{{{DAV}}}displayname", f"{{{DAV}}}resourcetype",
        ]), depth="1"))
        result = []
        for href, props in rows:
            kind = props.get(f"{{{DAV}}}resourcetype")
            if kind is None or kind.find(f"{{{CAL}}}calendar") is None:
                continue
            url = safe_url(href)
            if self.allowed and url not in self.allowed:
                continue
            name = props.get(f"{{{DAV}}}displayname")
            item = {"calendar_id": url, "name": name.text if name is not None else ""}
            if url in self.calendar_profiles:
                item["usage"] = self.calendar_profiles[url]["description"]
                item["default_availability"] = self.calendar_profiles[url].get("default_availability", "busy")
            result.append(item)
        return sorted(result, key=lambda item: item["calendar_id"])

    @staticmethod
    def _property_href(rows, tag):
        for _, props in rows:
            node = props.get(tag)
            if node is not None:
                href = node.findtext(f"{{{DAV}}}href")
                if href:
                    return safe_url(href)
        raise CalendarError("CalDAV discovery did not return the required collection.")

    def events(self, calendar_id: str, start: str, end: str, limit: int = 100) -> dict:
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise CalendarError("limit must be between 1 and 500.")
        first, last = window(start, end, self.zone)
        target = safe_url(calendar_id)
        # Never accept an arbitrary same-host URL as a calendar or bypass the configured allowlist.
        if target not in {item["calendar_id"] for item in self.calendars()}:
            raise CalendarError("Calendar is unavailable or excluded by the calendar allowlist.")
        root = Element(f"{{{CAL}}}calendar-query")
        prop = SubElement(root, f"{{{DAV}}}prop")
        SubElement(prop, f"{{{DAV}}}getetag")
        SubElement(prop, f"{{{CAL}}}calendar-data")
        filt = SubElement(root, f"{{{CAL}}}filter")
        outer = SubElement(filt, f"{{{CAL}}}comp-filter", {"name": "VCALENDAR"})
        inner = SubElement(outer, f"{{{CAL}}}comp-filter", {"name": "VEVENT"})
        SubElement(inner, f"{{{CAL}}}time-range", {
            "start": first.strftime("%Y%m%dT%H%M%SZ"), "end": last.strftime("%Y%m%dT%H%M%SZ"),
        })
        rows = dav_rows(self.request("REPORT", target, root, depth="1"))
        calendars = []
        for href, props in rows:
            data = props.get(f"{{{CAL}}}calendar-data")
            if data is None or not data.text:
                continue
            # Hrefs are metadata only; never fetch URLs embedded inside calendar data.
            safe_url(href)
            calendars.append(data.text)
        try:
            # Keep hostile/expensive recurrence rules outside the credential-bearing process.
            result = subprocess.run(
                [sys.executable, "-m", "yandex_calendar_mcp.recurrence"],
                input=json.dumps({"calendars": calendars, "first": first.isoformat(),
                                  "last": last.isoformat(), "zone": str(self.zone),
                                  "calendar_id": calendar_id}).encode(),
                capture_output=True, timeout=10, check=True,
                env={"PATH": "/usr/bin:/bin", "PYTHONUTF8": "1"},
            )
            items = json.loads(result.stdout)
        except (subprocess.SubprocessError, ValueError):
            raise CalendarError("Calendar/recurrence parsing failed or exceeded limits; schedule was not returned.") from None
        items.sort(key=lambda item: (item["start_sort"], item["uid"]))
        total = len(items)
        for item in items:
            item.pop("start_sort")
        return {"timezone": str(self.zone), "start": start, "end_exclusive": end,
                "events": items[:limit], "matched": total, "truncated": total > limit,
                "content_is_untrusted": True}

