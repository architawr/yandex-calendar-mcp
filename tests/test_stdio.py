"""Real SDK client/server exchange over pipes, with an in-process mock DAV transport."""

import asyncio
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_separates_read_preview_and_apply_permissions(tmp_path):
    fixture = Path(__file__).with_name("stdio_fixture.py")
    async def scenario():
        parameters = StdioServerParameters(command=sys.executable, args=[str(fixture)], env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src") + os.pathsep + str(fixture.parent),
        })
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert {tool.name for tool in tools} == {"list_calendars", "list_events", "prepare_event_change", "apply_event_change"}
                assert all(tool.annotations.readOnlyHint for tool in tools if tool.name != "apply_event_change")
                assert next(tool for tool in tools if tool.name == "apply_event_change").annotations.destructiveHint
                result = await session.call_tool("list_calendars", {})
                assert not result.isError
                assert result.structuredContent["calendars"][0]["name"] == "Работа"
                events = await session.call_tool("list_events", {"calendar_id": "https://caldav.yandex.ru/calendars/test/events-1/", "start": "2026-10-28", "end": "2026-10-29"})
                assert not events.isError
                assert events.structuredContent["matched"] == 1
                blocked = await session.call_tool("prepare_event_change", {"operation": "delete", "calendar_id": "https://caldav.yandex.ru/calendars/test/events-1/", "uid": "fixture"})
                assert blocked.isError
                for name in ("create_event", "delete_event", "activity_undo"):
                    result = await session.call_tool(name, {})
                    assert result.isError
    asyncio.run(scenario())


def test_stdio_prepare_apply_and_replay(tmp_path):
    async def scenario():
        fixture = Path(__file__).with_name("stdio_fixture.py")
        params = StdioServerParameters(command=sys.executable, args=[str(fixture)], env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src") + os.pathsep + str(fixture.parent),
            "TEST_WRITE_FIXTURE": str(tmp_path),
        })
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                preview = await session.call_tool("prepare_event_change", {
                    "operation": "create", "calendar_id": "https://caldav.yandex.ru/calendars/test/events-1/",
                    "changes": {"summary": "Test", "start": "2026-10-28", "end": "2026-10-29"},
                })
                assert not preview.isError
                token = preview.structuredContent["change_id"]
                result = await session.call_tool("apply_event_change", {"change_id": token})
                assert not result.isError and result.structuredContent["verified"]
                replay = await session.call_tool("apply_event_change", {"change_id": token})
                assert replay.isError
    asyncio.run(scenario())
