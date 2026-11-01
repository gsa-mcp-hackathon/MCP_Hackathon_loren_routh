"""Tool: find_clearance — check NEC 110.26 working clearance for a panel.

Like find_by_category, this tool RETURNS data rather than performing a viewer
action: it answers "is this electrical panel's code-required working space
clear?" by asking the viewer backend, which builds the NEC 110.26 working-space
zone in true 3D and checks whether any other element intrudes into it.

NEC 110.26(A) requires, for 120V-250V-to-ground panels: at least 36 in deep in
front of the working face, at least max(30 in, the panel's own width) wide, and
at least 78 in (6.5 ft) tall measured FROM THE FLOOR. The backend owns those
numbers and all geometry — they define the code requirement, not an expected
answer. v1 covers electrical panels only; any other category returns
"rule not defined".

The panel is addressed by its durable Revit UniqueId (= the viewer's
externalId). The result includes a `draw` block the agent can hand to the
draw_clearance viewer command to show the zones (green = clear, red =
intruded). No coordinate or SQL is ever passed through; the backend enforces the
same allowlist. The viewer backend origin is fixed by server config, never a
tool argument.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP

from bim_viewer_mcp.config import settings
from bim_viewer_mcp.models import valid_external_id
from bim_viewer_mcp.utils import UpstreamServiceError, post_json


def register(mcp: FastMCP) -> None:
    """Register the find_clearance tool with the MCP server."""

    @mcp.tool(
        name="find_clearance",
        annotations={
            "title": "Check NEC working clearance for a panel",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def find_clearance(
        external_id: Annotated[
            str | None,
            "The externalId of the electrical panel to check. Provide this OR "
            "description. Get an id from find_by_category("
            "category='electrical_panels') when you have one.",
        ] = None,
        description: Annotated[
            str | None,
            "A substring of the panel's name (e.g. 'circuit breaker "
            "switchboard'). The backend resolves it to the single matching "
            "electrical panel. Provide this OR external_id; prefer it when the "
            "user names the panel so you need not look up an id first.",
        ] = None,
        category: Annotated[
            Literal["electrical_panels"],
            "The element category. v1 supports electrical_panels only; the "
            "clearance rule is NEC 110.26 working space.",
        ] = "electrical_panels",
    ) -> dict[str, Any]:
        """Check an electrical panel's NEC 110.26 working clearance in 3D.

        Use this to answer "is the working space in front of this panel clear?"
        The backend builds the code-required clearance zone(s) in true 3D and
        reports whether anything intrudes. Pass the returned `draw` block to the
        draw_clearance viewer command to show the zones (green clear, red
        intruded); this tool does NOT change the view by itself.

        Target the panel either by external_id or by a description substring
        that must resolve to exactly one panel. If the description matches none
        or several, the result says so — ask the user to clarify.

        The check is directional (projected off the panel's working face) when
        the panel is mounted on a wall or column, and an honest all-around
        maintenance-access check when the panel is freestanding — the `mode` and
        `rule` fields say which, and the all-around mode is labeled as
        NEC-derived guidance rather than the literal front-only rule.

        Example:
            - By id: external_id=<panel id>
            - By name: description="circuit breaker switchboard"

        Returns:
            {"external_id", "asset_type", "description", "mode", "rule",
             "mounting_surface", "clearance": {"zones": [...]},
             "intruders": [{external_id, asset_type, description}],
             "clear": bool|None, "draw": {...}}. `clear` is None when no rule is
            defined for the category.
        """
        if not external_id and not description:
            return {
                "external_id": None,
                "mode": "none",
                "rule": "external_id or description is required",
                "clear": None,
                "intruders": [],
            }
        if external_id and not valid_external_id(external_id):
            return {
                "external_id": external_id,
                "mode": "none",
                "rule": "external_id is not a valid element id",
                "clear": None,
                "intruders": [],
            }
        payload: dict[str, Any] = {"category": category}
        if external_id:
            payload["external_id"] = external_id
        if description:
            payload["description"] = description
        try:
            data = await post_json(
                settings.viewer_base_url,
                "api/assets/clearance",
                payload,
            )
        except (UpstreamServiceError, ValueError) as exc:
            # A 404/409 from the backend means the description matched no panel
            # or several — surface that as a usable note, not a dead end.
            detail = getattr(exc, "detail", "") or ""
            status = getattr(exc, "status_code", 0)
            if status in (404, 409):
                return {
                    "external_id": external_id,
                    "mode": "none",
                    "rule": "panel not resolved",
                    "clear": None,
                    "intruders": [],
                    "hint": str(detail) or "No single matching panel.",
                }
            return {
                "external_id": external_id,
                "mode": "none",
                "rule": "clearance check unavailable",
                "clear": None,
                "intruders": [],
                "hint": "The viewer backend could not be reached.",
            }

        if not isinstance(data, dict):
            return {
                "external_id": external_id,
                "mode": "none",
                "rule": "clearance check returned no result",
                "clear": None,
                "intruders": [],
            }

        # Pass the backend's verdict through unchanged (ids are re-validated
        # below). The backend owns the NEC numbers and all geometry.
        intruders: list[dict[str, Any]] = []
        for row in data.get("intruders", []) or []:
            if not isinstance(row, dict):
                continue
            ext = row.get("external_id")
            if not valid_external_id(ext):
                continue
            intruders.append(
                {
                    "external_id": ext,
                    "asset_type": row.get("asset_type"),
                    "description": row.get("description"),
                }
            )
        return {
            "external_id": external_id,
            "asset_type": data.get("asset_type"),
            "description": data.get("description"),
            "mode": data.get("mode"),
            "rule": data.get("rule"),
            "mounting_surface": data.get("mounting_surface"),
            "clearance": data.get("clearance"),
            "intruders": intruders,
            "clear": data.get("clear"),
            "draw": data.get("draw"),
        }
