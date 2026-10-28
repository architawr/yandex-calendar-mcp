"""Only launched by tests; never included in the installed package."""
from test_reader import make_reader, calendar, event
from yandex_calendar_mcp import server
import os
from pathlib import Path
from test_writer import setup

server.load_reader = lambda: make_reader(calendar(event("UID:fixture\nDTSTART:20261028T100000Z\nDTEND:20261028T110000Z")))[0]
if os.environ.get("TEST_WRITE_FIXTURE"):
    writer, _, _ = setup(Path(os.environ["TEST_WRITE_FIXTURE"]))
    server.load_reader = lambda: writer.reader
    server.config = lambda: {"allowed_write_calendars": ["https://caldav.yandex.ru/calendars/test/events-1/"]}

server.mcp.run(transport="stdio")
