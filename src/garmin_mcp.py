"""Keep synchronous Garmin I/O out of the HTTP server's event loop."""
import asyncio
import functools
import inspect

from fastmcp import FastMCP


class GarminMCP(FastMCP):
    def tool(self, *args, **kwargs):
        register = super().tool(*args, **kwargs)

        def decorate(fn):
            def run_in_worker(call_args, call_kwargs):
                result = fn(*call_args, **call_kwargs)
                return asyncio.run(result) if inspect.isawaitable(result) else result

            @functools.wraps(fn)
            async def offloaded(*call_args, **call_kwargs):
                return await asyncio.to_thread(run_in_worker, call_args, call_kwargs)

            return register(offloaded)

        return decorate
