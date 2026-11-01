"""Server-info resource for the BIM viewer MCP server.

Exposes a short, read-only description the model can consult to understand the
vocabulary (what an "element" vs a "room" is, how ids work, the zoom rule).
"""

from __future__ import annotations

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register the server-info resource(s) with the MCP server."""

    @mcp.resource("viewer://info/server")
    def server_info() -> str:
        """How to drive the BIM viewer through this server's tools."""
        return (
            "BIM Viewer MCP Server\n\n"
            "Controls a live Autodesk Platform Services (APS) 3D viewer showing "
            "a mechanical (MECH) building model.\n\n"
            "Addressing:\n"
            "  - Elements and rooms are identified by their durable Revit "
            "UniqueId, which is the viewer's externalId. Shape: an 8-4-4-4-12 "
            "hex GUID, a hyphen, then 8 hex digits "
            "(e.g. 7485c179-9364-48d7-b68b-6736718012c4-0005fbe8).\n"
            "  - Never use transient numeric ids.\n\n"
            "Tools:\n"
            "  - highlight_elements: color a set of elements in place.\n"
            "  - highlight_rooms: draw translucent room/space volumes.\n"
            "  - isolate: show only a set, hiding everything else.\n"
            "  - zoom_to: move the camera to frame a set or a room.\n"
            "  - reset: clear highlights, rooms, and focus; show the whole "
            "building.\n\n"
            "Zoom rule: highlighting an answer never moves the camera. Only "
            "zoom_to changes the view."
        )
