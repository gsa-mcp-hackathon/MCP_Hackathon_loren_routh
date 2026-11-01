"""Tool: find_elements — resolve a cost criterion to element externalIds.

Unlike the other tools (which perform a viewer ACTION), this one RETURNS data:
it answers "which elements match this cost criterion?" by asking the viewer
backend, which queries the facility/asset (Maximo) data and joins it to the
durable Revit UniqueId (= the viewer's externalId). The agent then feeds those
ids to highlight_elements or isolate.

Only an allowlisted metric + operator are accepted — never free-form SQL, a
field name, or a URL. The backend enforces the same allowlists (defense in
depth). The viewer backend origin is fixed by server config, never a tool
argument.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP

from bim_viewer_mcp.config import settings
from bim_viewer_mcp.models import (
    ALLOWED_METRICS,
    ALLOWED_OPS,
    MAX_QUERY_LIMIT,
    valid_external_id,
)
from bim_viewer_mcp.utils import UpstreamServiceError, post_json


def register(mcp: FastMCP) -> None:
    """Register the find_elements tool with the MCP server."""

    @mcp.tool(
        name="find_elements",
        annotations={
            "title": "Find elements by cost",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def find_elements(
        metric: Annotated[
            Literal["replacement_cost", "maintenance_cost"],
            "Which cost to rank/filter by: an asset's replacement_cost, or its "
            "summed maintenance_cost from work orders.",
        ],
        op: Annotated[
            Literal["top", "gt", "gte", "lt", "lte"],
            "How to select: 'top' for the highest-cost N; gt/gte/lt/lte to "
            "compare against a dollar threshold.",
        ],
        value: Annotated[
            float,
            "Dollar threshold for gt/gte/lt/lte. Ignored when op='top'.",
        ] = 0.0,
        limit: Annotated[
            int,
            "Max elements to return (1-500). For op='top' this is N.",
        ] = 50,
    ) -> dict[str, Any]:
        """Find building elements by a cost criterion; return their externalIds.

        Use this to answer cost questions, then pass the returned external_ids
        to highlight_elements (to show them in place) or isolate. This tool does
        NOT change the view by itself.

        Examples:
            - Ten most expensive: metric='replacement_cost', op='top', limit=10
            - Over $50,000:       metric='replacement_cost', op='gt', value=50000
            - Highest maintenance: metric='maintenance_cost', op='top', limit=5

        Returns:
            {"metric": str, "count": int,
             "elements": [{external_id, asset_type, description, metric_value}]}.
        """
        if metric not in ALLOWED_METRICS or op not in ALLOWED_OPS:
            return {
                "metric": metric,
                "count": 0,
                "elements": [],
                "hint": "Unsupported metric or operator.",
            }
        bounded_limit = max(1, min(int(limit or 50), MAX_QUERY_LIMIT))
        try:
            data = await post_json(
                settings.viewer_base_url,
                "api/assets/query",
                {
                    "metric": metric,
                    "op": op,
                    "value": float(value or 0.0),
                    "limit": bounded_limit,
                },
            )
        except (UpstreamServiceError, ValueError):
            return {
                "metric": metric,
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
            elements.append(
                {
                    "external_id": ext,
                    "asset_type": row.get("asset_type"),
                    "description": row.get("description"),
                    "metric_value": row.get("metric_value"),
                }
            )
        return {"metric": metric, "count": len(elements), "elements": elements}
