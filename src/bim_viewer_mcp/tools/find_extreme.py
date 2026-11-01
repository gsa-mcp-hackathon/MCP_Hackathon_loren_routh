"""Tool: find_extreme — the single most-extreme element by geometry.

Like find_nearest, this tool RETURNS data rather than performing a viewer
action. It answers "which element is the most ___?" by asking the viewer
backend, which ranks candidates by TRUE 3D geometry computed server-side in
Python:

  - a compass extreme (northmost / southmost / eastmost / westmost), ranked by
    the bounding box's extent along that axis in the model's coordinate frame
    (+Y is North, -X is West); and
  - a size extreme (largest / smallest), ranked by 3D bounding-box volume.

For size queries the answer carries its volume_cuft. For compass queries we
return only WHICH element is most extreme (no spurious length), because the raw
axis coordinate is a signed sort key, not a measurement to show the user.

An optional category/level restricts the field. No coordinate, free-form field,
or SQL is ever passed through; the backend enforces the same allowlists. The
viewer backend origin is fixed by server config, never a tool argument.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP

from bim_viewer_mcp.config import settings
from bim_viewer_mcp.models import (
    ALLOWED_CATEGORIES,
    ALLOWED_DIRECTIONS,
    ALLOWED_LEVELS,
    ALLOWED_SIZE_OPS,
    MAX_SPATIAL_LIMIT,
    valid_external_id,
)
from bim_viewer_mcp.utils import UpstreamServiceError, post_json


def register(mcp: FastMCP) -> None:
    """Register the find_extreme tool with the MCP server."""

    @mcp.tool(
        name="find_extreme",
        annotations={
            "title": "Find the most-extreme element by geometry",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def find_extreme(
        op: Annotated[
            Literal[
                "northmost",
                "southmost",
                "eastmost",
                "westmost",
                "largest",
                "smallest",
            ],
            "Which extreme: a compass direction (northmost/southmost/eastmost/"
            "westmost) ranks by position; largest/smallest ranks by 3D "
            "bounding-box volume.",
        ],
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
            "Optional category to restrict to (e.g. the largest air handler). "
            "Omit to rank across all tracked equipment.",
        ] = None,
        level: Annotated[
            Literal["Level 1", "Level 2", "Level 3", "Roof Level"] | None,
            "Optional building level to restrict to. Omit for all levels.",
        ] = None,
        limit: Annotated[
            int,
            "How many ranked elements to return (1-50). Use 1 for the single "
            "most-extreme element.",
        ] = 1,
    ) -> dict[str, Any]:
        """Find the most-extreme element(s) by position or size.

        Use this to answer superlative questions (e.g. "the largest piece of
        equipment", "the northmost air handler", "the smallest transformer"),
        then pass the returned external_ids to highlight_elements or zoom_to.
        This tool does NOT change the view by itself.

        For largest/smallest, each element carries its volume_cuft. For compass
        directions the answer is simply which element is most extreme.

        Examples:
            - Largest equipment:   op='largest'
            - Northmost air handler: op='northmost', category='air_handlers'
            - Three smallest fans: op='smallest', category='fans', limit=3

        Returns:
            {"op": str, "count": int,
             "elements": [{external_id, asset_type, description, level_name,
             volume_cuft?}]} ranked most-extreme first.
        """
        if op not in ALLOWED_DIRECTIONS and op not in ALLOWED_SIZE_OPS:
            return {
                "op": op,
                "count": 0,
                "elements": [],
                "hint": "Unsupported op.",
            }
        if category is not None and category not in ALLOWED_CATEGORIES:
            return {
                "op": op,
                "count": 0,
                "elements": [],
                "hint": "Unsupported category.",
            }
        if level is not None and level not in ALLOWED_LEVELS:
            return {
                "op": op,
                "count": 0,
                "elements": [],
                "hint": "Unsupported level.",
            }
        bounded_limit = max(1, min(int(limit or 1), MAX_SPATIAL_LIMIT))
        payload: dict[str, Any] = {"op": op, "limit": bounded_limit}
        if category is not None:
            payload["category"] = category
        if level is not None:
            payload["level"] = level
        try:
            data = await post_json(
                settings.viewer_base_url,
                "api/assets/spatial",
                payload,
            )
        except (UpstreamServiceError, ValueError):
            return {
                "op": op,
                "count": 0,
                "elements": [],
                "hint": "The viewer backend could not be reached.",
            }

        raw = data.get("elements", []) if isinstance(data, dict) else []
        elements: list[dict[str, Any]] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            ext = row.get("external_id")
            if not valid_external_id(ext):
                continue
            el: dict[str, Any] = {
                "external_id": ext,
                "asset_type": row.get("asset_type"),
                "description": row.get("description"),
                "level_name": row.get("level_name"),
            }
            # Size ops carry a real volume; compass ops carry only identity.
            if "volume_cuft" in row:
                el["volume_cuft"] = row.get("volume_cuft")
            elements.append(el)
        return {"op": op, "count": len(elements), "elements": elements}
