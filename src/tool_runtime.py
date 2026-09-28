"""Keep process health responsive while synchronous Garmin I/O is running."""
import asyncio
import functools
import inspect

from fastmcp import FastMCP


def offload(function):
    # Existing tool handlers are async declarations containing synchronous
    # Garmin calls and no awaits. Preserve their signatures/schemas while
    # executing the entire handler away from the HTTP event loop.
    def run(*args, **kwargs):
        result = function(*args, **kwargs)
        return asyncio.run(result) if inspect.isawaitable(result) else result

    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        return await asyncio.to_thread(run, *args, **kwargs)

    return wrapped


class GarminMCP(FastMCP):
    def tool(self, name_or_fn=None, **kwargs):
        if callable(name_or_fn):
            return super().tool(offload(name_or_fn), **kwargs)
        register = super().tool(name_or_fn, **kwargs)
        return lambda function: register(offload(function))
