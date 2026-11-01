"""Tools submodule — one tool per file.

Each tool module exposes a `register(mcp)` function that attaches its
`@mcp.tool`-decorated function to the server. This file aggregates them into a
single `register_tools(mcp)` that `app.py` calls.

The tools wrap the viewer's fixed command vocabulary. Each validates its
arguments (all treated as untrusted) and POSTs one allowlisted command to the
viewer backend, which broadcasts it to every connected 3D viewer.
"""

from __future__ import annotations

from bim_viewer_mcp.tools import (
    color_by,
    find_by_category,
    find_clearance,
    find_elements,
    find_extreme,
    find_in_room,
    find_nearest,
    find_workorders,
    highlight_elements,
    highlight_rooms,
    isolate,
    reset,
    zoom_to,
)


def register_tools(mcp) -> None:
    """Register all tools with the MCP server."""
    highlight_elements.register(mcp)
    highlight_rooms.register(mcp)
    isolate.register(mcp)
    zoom_to.register(mcp)
    color_by.register(mcp)
    reset.register(mcp)
    find_elements.register(mcp)
    find_by_category.register(mcp)
    find_nearest.register(mcp)
    find_extreme.register(mcp)
    find_in_room.register(mcp)
    find_clearance.register(mcp)
    find_workorders.register(mcp)


__all__ = ["register_tools"]
