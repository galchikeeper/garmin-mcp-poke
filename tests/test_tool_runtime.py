import asyncio
import inspect
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tool_runtime import GarminMCP, offload


def test_blocked_garmin_handler_does_not_block_http_event_loop():
    started, release = threading.Event(), threading.Event()

    async def blocking(date: str) -> dict:
        started.set()
        assert release.wait(timeout=3)
        return {"date": date}

    wrapped = offload(blocking)
    assert inspect.signature(wrapped) == inspect.signature(blocking)

    async def scenario():
        task = asyncio.create_task(wrapped("2026-09-28"))
        assert await asyncio.to_thread(started.wait, 1)
        # This coroutine represents concurrent health processing. It cannot run
        # if blocking() accidentally executes on the HTTP event-loop thread.
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.wait_for(task, 1) == {"date": "2026-09-28"}

    asyncio.run(scenario())


def test_registered_tool_preserves_schema_and_execution():
    app = GarminMCP("test")

    @app.tool()
    async def read_value(date: str) -> str:
        """A read-only fixture."""
        return date

    assert read_value.name == "read_value"
    assert read_value.parameters["properties"]["date"]["type"] == "string"
    result = asyncio.run(read_value.run({"date": "2026-09-28"}))
    assert result.content[0].text == "2026-09-28"
