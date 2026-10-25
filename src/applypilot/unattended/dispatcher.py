"""Finite provider session helpers and Temporal schedule controls."""
import asyncio
from applypilot import postgres, config
from .browser import BrowserOS

def connection():
    # Each async worker owns its connection; database.get_connection is
    # thread-local and would share one transaction across all these tasks.
    conn = postgres.connect(config.DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = postgres.Row
    return conn



class TaskBrowser:
    """One MCP connection entered and exited by the same finite asyncio task."""
    def __init__(self):
        self.browser = None

    async def get(self):
        if self.browser is None:
            candidate = BrowserOS()
            async with asyncio.timeout(45):
                self.browser = await candidate.__aenter__()
        return self.browser

    async def close(self):
        if self.browser is not None:
            await self.browser.__aexit__(None, None, None)



async def serve(profile=None, stop_event=None):
    """Compatibility control request; networking semantics continue in Part 3."""
    from applypilot.orchestration.client import change_schedules
    return await change_schedules("resume", names=[
        "network_prepare", "network_research", "network_delivery"])
