"""Smoke tests for the BIM viewer MCP server.

These verify the server imports and every submodule registers without error,
and that the six viewer-control tools are discoverable.
"""

from __future__ import annotations

import pytest
from fastmcp import FastMCP

from bim_viewer_mcp.app import mcp
from bim_viewer_mcp.config import settings


def test_server_is_importable():
    assert mcp is not None


def test_server_has_name():
    assert mcp.name == "BIM Viewer MCP Server"


def test_default_transport_is_stdio():
    """Guard against accidentally shipping with a non-stdio default."""
    assert settings.mcp_transport == "stdio"


def test_tools_register_without_error():
    from bim_viewer_mcp.tools import register_tools

    register_tools(FastMCP("test"))  # must not raise


def test_prompts_register_without_error():
    from bim_viewer_mcp.prompts import register_prompts

    register_prompts(FastMCP("test"))  # must not raise


def test_resources_register_without_error():
    from bim_viewer_mcp.resources import register_resources

    register_resources(FastMCP("test"))  # must not raise


def test_routes_register_without_error():
    from bim_viewer_mcp.routes import register_routes

    register_routes(FastMCP("test"))  # must not raise


@pytest.mark.asyncio
async def test_viewer_tools_are_discoverable():
    """The viewer-control tools should be registered and discoverable."""
    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}
    assert {
        "highlight_elements",
        "highlight_rooms",
        "isolate",
        "zoom_to",
        "color_by",
        "reset",
        "find_elements",
        "find_by_category",
        "find_nearest",
        "find_extreme",
        "find_in_room",
        "find_clearance",
    } <= tool_names
