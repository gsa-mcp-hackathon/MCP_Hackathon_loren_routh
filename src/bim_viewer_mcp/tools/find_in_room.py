"""Tool: find_in_room — the tracked assets inside a room's 3D volume.

Like find_by_category, this tool RETURNS data rather than performing a viewer
action: it answers "what's in this room?" by asking the viewer backend, which
tests each asset for membership in the room's 3D volume. Membership is
centroid-in-prism: the room's footprint (its WKT polygon) is extruded from its
floor elevation up by its height, and an asset counts as "in" the room when its
bounding-box center lies inside that prism. This is real 3D containment math
done server-side in Python — a Level 2 asset is not matched to a Level 1 room
that shares the same footprint.

A room is addressed by its durable id (externalId) or its room number; an
optional category restricts the result to one asset family. No free-form field,
coordinate, or SQL is ever passed through; the backend enforces the same
allowlists. The viewer backend origin is fixed by server config, never a tool
argument.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP

from bim_viewer_mcp.config import settings
from bim_viewer_mcp.models import (
    ALLOWED_CATEGORIES,
    MAX_INROOM_LIMIT,
    valid_external_id,
)
from bim_viewer_mcp.utils import UpstreamServiceError, post_json


def register(mcp: FastMCP) -> None:
    """Register the find_in_room tool with the MCP server."""

    @mcp.tool(
        name="find_in_room",
        annotations={
            "title": "Find the assets inside a room",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def find_in_room(
        room_number: Annotated[
            str | None,
            "The room's number as shown on the plans (e.g. '54' for 'EDP I'). "
            "Provide this or room_external_id.",
        ] = None,
        room_external_id: Annotated[
            str | None,
            "The room's durable externalId. An alternative to room_number.",
        ] = None,
        category: Annotated[
            Literal[
                "air_terminals",
                "supply_diffusers",
                "return_grilles",
                "exhaust_grilles",
                "heat_pumps",
                "cooling_tower",
                "air_handlers",
                "fans",
                "transformers",
                "electrical_panels",
                "water_heaters",
                "sprinklers",
                "lighting",
                "receptacles",
                "plumbing_fixtures",
            ]
            | None,
            "Optional category to restrict the result to (e.g. only the "
            "receptacles in the room). Omit for all tracked assets.",
        ] = None,
        limit: Annotated[
            int,
            "Max assets to return (1-500).",
        ] = 50,
    ) -> dict[str, Any]:
        """Find the tracked assets that sit inside a room's 3D volume.

        Use this to answer room-contents questions (e.g. "what equipment is in
        room 54?", "the receptacles in the electrical room"), then pass the
        returned external_ids to highlight_elements or isolate. This tool does
        NOT change the view by itself.

        Membership is by true 3D containment: an asset's bounding-box center
        must fall inside the room footprint AND within its floor-to-ceiling
        band, so assets on other floors are correctly excluded.

        Examples:
            - Everything in room 54: room_number='54'
            - Just the receptacles:  room_number='54', category='receptacles'

        Returns:
            {"room": {external_id, room_name, room_number, level_name},
             "count": int,
             "elements": [{external_id, asset_type, description, level_name}]}.
        """
        if not room_number and not room_external_id:
            return {
                "room": None,
                "count": 0,
                "elements": [],
                "hint": "Provide room_number or room_external_id.",
            }
        if room_external_id is not None and not valid_external_id(
            room_external_id
        ):
            return {
                "room": None,
                "count": 0,
                "elements": [],
                "hint": "room_external_id is not a valid room id.",
            }
        if category is not None and category not in ALLOWED_CATEGORIES:
            return {
                "room": None,
                "count": 0,
                "elements": [],
                "hint": "Unsupported category.",
            }
        bounded_limit = max(1, min(int(limit or 50), MAX_INROOM_LIMIT))
        payload: dict[str, Any] = {"limit": bounded_limit}
        if room_number:
            payload["room_number"] = str(room_number)
        if room_external_id:
            payload["room_external_id"] = room_external_id
        if category is not None:
            payload["category"] = category
        try:
            data = await post_json(
                settings.viewer_base_url,
                "api/assets/in-room",
                payload,
            )
        except (UpstreamServiceError, ValueError):
            return {
                "room": None,
                "count": 0,
                "elements": [],
                "hint": "The viewer backend could not be reached.",
            }

        room = data.get("room") if isinstance(data, dict) else None
        raw = data.get("elements", []) if isinstance(data, dict) else []
        elements: list[dict[str, Any]] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            ext = row.get("external_id")
            if not valid_external_id(ext):
                continue
            elements.append(
                {
                    "external_id": ext,
                    "asset_type": row.get("asset_type"),
                    "description": row.get("description"),
                    "level_name": row.get("level_name"),
                }
            )
        return {
            "room": room,
            "count": len(elements),
            "elements": elements,
        }
