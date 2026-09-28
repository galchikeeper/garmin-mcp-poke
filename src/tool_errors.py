"""Turn legacy tool error strings into real MCP isError responses."""
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware


class LegacyToolErrors(Middleware):
    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        for block in result.content:
            if getattr(block, "type", None) == "text" and block.text.startswith("Error "):
                raise ToolError(block.text)
        return result
