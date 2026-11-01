"""Databricks App backend for the BIM viewer."""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from db import query_rows
from genie import answer as genie_answer
import geometry

HERE = Path(__file__).parent
app = FastAPI(title="BIM viewer")

# Structured logging with explicitly allowlisted fields only. Per SECURITY.md
# we never log upstream bodies, ids, URLs, query strings, stack traces, or any
# PII â€” only the small, non-sensitive markers below.
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("viewer")


# ----------------------------------------------- Role B: viewer command hub
#
# The browser viewer connects to /ws/viewer and listens for commands. The
# backend (and, in a later pass, an MCP server) broadcasts commands here to
# drive the 3D view. HACKATHON POSTURE: this endpoint is unauthenticated over
# public pilot data (see SECURITY-NOTES.md). All command *arguments* are still
# validated on the browser side before anything is executed.
#
# Only this fixed vocabulary is ever emitted. Nothing here accepts a
# tool-supplied URL, path, or arbitrary code.
ALLOWED_COMMANDS = {
    "highlight_elements",
    "highlight_rooms",
    "isolate",
    "zoom_to",
    "color_by",
    "draw_clearance",
    "reset",
}


class ViewerHub:
    """Tracks connected viewer sockets and broadcasts validated commands."""

    def __init__(self):
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        log.info("viewer_ws_connect clients=%d", len(self._clients))

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self._clients.discard(ws)
        log.info("viewer_ws_disconnect clients=%d", len(self._clients))

    async def broadcast(self, command: dict):
        """Send one allowlisted command to every connected viewer."""
        name = command.get("command") if isinstance(command, dict) else None
        if name not in ALLOWED_COMMANDS:
            # Fail safe; never emit an unknown command.
            log.warning("viewer_broadcast_rejected")
            return
        payload = json.dumps(command)
        async with self._lock:
            targets = list(self._clients)
        dead = []
        for ws in targets:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
        log.info(
            "viewer_broadcast command=%s clients=%d", name, len(targets)
        )


hub = ViewerHub()


@app.websocket("/ws/viewer")
async def viewer_ws(ws: WebSocket):
    await hub.connect(ws)
    try:
        # The viewer is a passive command sink; we simply keep the socket open.
        # Any inbound frames are ignored (the browser never needs to push here).
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await hub.disconnect(ws)
    except Exception:
        await hub.disconnect(ws)


class ChatIn(BaseModel):
    query: str


class ViewerCommandIn(BaseModel):
    command: str
    args: dict = {}


# ------------------------------------------------- Asset cost/data query
#
# The MCP `find_elements` tool calls this route to turn an allowlisted COST
# criterion into a set of durable externalIds (Revit UniqueId = source_unique_id
# = APS externalId) it can then hand to highlight_elements / isolate. The caller
# NEVER supplies SQL â€” only a metric + operator from the fixed allowlists below,
# plus a bounded numeric. The SQL is assembled here from constants and runs with
# parameterized values. HACKATHON POSTURE: unauthenticated over public pilot
# data (see SECURITY-NOTES.md); responses carry only ids + the selected metric.
ASSETS_TABLE = "users.loren_routh.maximo_assets"
WORKORDERS_TABLE = "users.loren_routh.maximo_workorders"

# metric -> how to express it in SQL. Only these are ever queryable.
ALLOWED_METRICS = {
    "replacement_cost": "a.replacement_cost",
    "maintenance_cost": "COALESCE(w.total_maintenance_cost, 0)",
}
ALLOWED_OPS = {"top", "bottom", "gt", "gte", "lt", "lte"}
MAX_LIMIT = 500


class AssetQueryIn(BaseModel):
    metric: str
    op: str
    value: float = 0.0
    limit: int = 50


@app.post("/api/assets/query")
async def assets_query(body: AssetQueryIn):
    """Resolve an allowlisted cost criterion to a set of externalIds.

    Body: {metric, op, value, limit}. `metric` and `op` must be in the fixed
    allowlists; `value`/`limit` are bounded numerics. Returns
    {elements:[{external_id, asset_type, description, metric_value}]}. No SQL,
    URL, or path is ever taken from the caller.
    """
    metric = body.metric
    op = body.op
    if metric not in ALLOWED_METRICS or op not in ALLOWED_OPS:
        log.warning("assets_query_rejected")
        raise HTTPException(status_code=400, detail="unsupported query")

    limit = max(1, min(int(body.limit or 50), MAX_LIMIT))
    metric_sql = ALLOWED_METRICS[metric]

    # Base relation: assets left-joined to per-asset summed maintenance cost.
    base = (
        f"FROM {ASSETS_TABLE} a "
        f"LEFT JOIN (SELECT asset_id, SUM(total_cost) AS total_maintenance_cost "
        f"           FROM {WORKORDERS_TABLE} GROUP BY asset_id) w "
        f"  ON a.asset_id = w.asset_id "
        f"WHERE a.source_unique_id IS NOT NULL"
    )

    params: dict = {}
    if op == "top":
        where_extra = ""
        order = f"ORDER BY {metric_sql} DESC"
    elif op == "bottom":
        where_extra = ""
        order = f"ORDER BY {metric_sql} ASC"
    else:
        sql_op = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
        where_extra = f" AND {metric_sql} {sql_op} :value"
        order = f"ORDER BY {metric_sql} DESC"
        params["value"] = float(body.value)

    query = (
        f"SELECT a.source_unique_id AS external_id, "
        f"       a.asset_type, a.description, "
        f"       {metric_sql} AS metric_value "
        f"{base}{where_extra} "
        f"{order} "
        f"LIMIT {limit}"
    )
    try:
        rows = await asyncio.to_thread(query_rows, query, params)
    except Exception as exc:
        log.warning("assets_query_failed")
        raise HTTPException(status_code=502, detail="query failed") from exc

    log.info("assets_query_served metric=%s op=%s n=%d", metric, op, len(rows))
    return {"metric": metric, "elements": rows}


# ------------------------------------------------- Single-asset details
#
# The viewer's properties panel calls this route to replace the (hollow) Revit
# Cost field with the authoritative Maximo values for the SELECTED element, and
# to overlay the nameplate fields (manufacturer / model / serial). It takes one
# durable externalId (= Revit UniqueId = source_unique_id) and returns that
# asset's replacement cost, summed maintenance cost, and nameplate data. Same
# hackathon posture and join as /api/assets/query: externalId is bound as a
# parameter, no SQL/column/path is ever taken from the caller, and a failure
# surfaces a 502 (never a fabricated success or a $0 placeholder).
class AssetDetailsIn(BaseModel):
    external_id: str


@app.post("/api/assets/details")
async def asset_details(body: AssetDetailsIn):
    """Return one asset's Maximo overlay fields by externalId.

    Body: {external_id}. Returns {external_id, found, replacement_cost,
    maintenance_cost, manufacturer, model_number, serial_number}. `found` is
    False when the id matches no Maximo asset, so the caller can show an honest
    "unavailable" instead of a faked value.
    """
    external_id = (body.external_id or "").strip()
    if not external_id:
        raise HTTPException(status_code=400, detail="external_id required")

    query = (
        "SELECT a.source_unique_id AS external_id, "
        "       a.replacement_cost AS replacement_cost, "
        "       a.manufacturer AS manufacturer, "
        "       a.model_number AS model_number, "
        "       a.serial_number AS serial_number, "
        "       COALESCE(w.total_maintenance_cost, 0) AS maintenance_cost "
        f"FROM {ASSETS_TABLE} a "
        f"LEFT JOIN (SELECT asset_id, SUM(total_cost) AS total_maintenance_cost "
        f"           FROM {WORKORDERS_TABLE} GROUP BY asset_id) w "
        "  ON a.asset_id = w.asset_id "
        "WHERE a.source_unique_id = :external_id "
        "LIMIT 1"
    )
    try:
        rows = await asyncio.to_thread(query_rows, query, {"external_id": external_id})
    except Exception as exc:
        log.warning("asset_details_failed")
        raise HTTPException(status_code=502, detail="query failed") from exc

    if not rows:
        log.info("asset_details_served found=0")
        return {
            "external_id": external_id,
            "found": False,
            "replacement_cost": None,
            "maintenance_cost": None,
            "manufacturer": None,
            "model_number": None,
            "serial_number": None,
        }

    row = rows[0]
    log.info("asset_details_served found=1")
    return {
        "external_id": external_id,
        "found": True,
        "replacement_cost": row.get("replacement_cost"),
        "maintenance_cost": row.get("maintenance_cost"),
        "manufacturer": row.get("manufacturer"),
        "model_number": row.get("model_number"),
        "serial_number": row.get("serial_number"),
    }


# ------------------------------------------------- Asset category query
#
# The MCP `find_by_category` tool calls this route to turn an allowlisted
# CATEGORY (plus optional level / room name) into a set of durable externalIds.
# Like /api/assets/query, the caller NEVER supplies SQL, a column name, or a raw
# asset_type: the category maps to a fixed set of asset_type values held here on
# the server. The level allowlist and room-name filter are passed as bound
# parameters. Same hackathon posture as /api/assets/query.
#
# category -> the asset_type values it covers. Only these are ever queryable.
ALLOWED_CATEGORIES = {
    "air_terminals": ["SUPPLY_DIFFUSER", "RETURN_GRILLE", "EXHAUST_GRILLE"],
    "supply_diffusers": ["SUPPLY_DIFFUSER"],
    "return_grilles": ["RETURN_GRILLE"],
    "exhaust_grilles": ["EXHAUST_GRILLE"],
    "heat_pumps": ["WSHP"],
    "cooling_tower": ["COOLING_TOWER"],
    "air_handlers": ["AHU"],
    "fans": ["FAN"],
    "transformers": ["TRANSFORMER"],
    "electrical_panels": ["ELECTRICAL_PANEL"],
    "water_heaters": ["WATER_HEATER"],
    "sprinklers": ["SPRINKLER"],
    "lighting": ["LIGHTING_FIXTURE"],
    "receptacles": [
        "DUPLEX_RECEPTACLE",
        "GFCI_RECEPTACLE",
        "QUADRUPLEX_RECEPTACLE",
    ],
    "plumbing_fixtures": ["WATER_CLOSET", "LAVATORY", "URINAL"],
}
ALLOWED_LEVELS = {"Level 1", "Level 2", "Level 3", "Roof Level"}
MAX_CATEGORY_LIMIT = 5000

# --- Work-order allow-lists (used by the find_workorders agent tool) ----------
# The work-order table (WORKORDERS_TABLE) is queried only through these fixed
# filters; the model fills enum values, never SQL. Values mirror the real
# distinct values in users.loren_routh.maximo_workorders.
WO_ALLOWED_STATUSES = {
    "OPEN", "IN_PROGRESS", "WAITING_PARTS", "CLOSED", "CANCELED",
}
# Friendly word -> stored numeric priority. Lower number = more urgent
# (2 = high, 3 = normal, 4 = low), per the data generator. No priority 1
# exists in the data (the emergency tier was removed).
WO_PRIORITY_WORDS = {"high": 2, "normal": 3, "low": 4}
WO_ALLOWED_FAILURE_CLASSES = {
    "LIGHTING", "ELECTRICAL", "HVAC", "PLUMBING", "FIRE_PROTECTION",
}
WO_ALLOWED_REPORTED_BY = {
    "TENANT", "FACILITIES_TECH", "INSPECTOR", "VENDOR",
}
WO_ALLOWED_MEASURES = {"count", "cost"}

# A few categories share an asset_type with items that don't belong. The
# "lighting" bucket is baked as LIGHTING_FIXTURE in the Revit model but also
# contains lighting CONTROLS (switches, occupancy sensors) which aren't light
# fixtures. These description substrings (lowercased) are excluded so a
# category means what a user expects. Server-side constant, never caller input.
CATEGORY_DESCRIPTION_EXCLUDES = {
    "lighting": ["switch", "sensor"],
}


class CategoryQueryIn(BaseModel):
    category: str
    level: str | None = None
    room_name: str | None = None
    limit: int = 500


@app.post("/api/assets/by-category")
async def assets_by_category(body: CategoryQueryIn):
    """Resolve an allowlisted category (+ optional level/room) to externalIds.

    Body: {category, level?, room_name?, limit}. `category` must be in the fixed
    ALLOWED_CATEGORIES map and `level` (if given) in ALLOWED_LEVELS; the
    asset_type values are taken from the server-side map, never the caller.
    `room_name` is an optional case-insensitive substring match, passed as a
    bound parameter. Returns {category, elements:[{external_id, asset_type,
    description, level_name, room_name}]}. No SQL, URL, or path is ever taken
    from the caller.
    """
    category = body.category
    if category not in ALLOWED_CATEGORIES:
        log.warning("category_query_rejected")
        raise HTTPException(status_code=400, detail="unsupported category")
    if body.level is not None and body.level not in ALLOWED_LEVELS:
        log.warning("category_query_rejected")
        raise HTTPException(status_code=400, detail="unsupported level")

    limit = max(1, min(int(body.limit or 500), MAX_CATEGORY_LIMIT))
    asset_types = ALLOWED_CATEGORIES[category]

    # asset_type IN (...) is built from the server-side constant, so each value
    # is a fixed literal here (never caller input). Bind level/room as params.
    type_list = ", ".join(f"'{t}'" for t in asset_types)
    where_extra = ""
    params: dict = {}
    if body.level is not None:
        where_extra += " AND level_name = :level"
        params["level"] = body.level
    if body.room_name:
        where_extra += " AND lower(room_name) LIKE :room"
        params["room"] = f"%{body.room_name.strip().lower()}%"
    for i, term in enumerate(CATEGORY_DESCRIPTION_EXCLUDES.get(category, [])):
        where_extra += f" AND lower(description) NOT LIKE :excl{i}"
        params[f"excl{i}"] = f"%{term}%"

    query = (
        "SELECT source_unique_id AS external_id, asset_type, description, "
        "       level_name, room_name "
        "FROM users.loren_routh.maximo_assets "
        "WHERE source_unique_id IS NOT NULL "
        f"  AND asset_type IN ({type_list})"
        f"{where_extra} "
        "ORDER BY level_name, asset_type "
        f"LIMIT {limit}"
    )
    try:
        rows = await asyncio.to_thread(query_rows, query, params)
    except Exception as exc:
        log.warning("category_query_failed")
        raise HTTPException(status_code=502, detail="query failed") from exc

    log.info(
        "category_query_served category=%s level=%s n=%d",
        category,
        body.level or "-",
        len(rows),
    )
    return {"category": category, "elements": rows}


# ------------------------------------------------- Work-order / maintenance query
#
# The find_workorders MCP tool and the Fast agent's find_workorders tool both
# reach the work-order table through this one route. Every filter is validated
# against a fixed allowlist (or mapped from a friendly word) before it touches
# SQL; the caller never supplies a column, value, or query. It answers "how
# many work orders ..." (measure=count) and "how much maintenance cost ..."
# (measure=cost). This is a data question: it returns numbers, not externalIds,
# and performs no viewer action.
class WorkOrderQueryIn(BaseModel):
    level: str | None = None
    status: str | None = None
    priority: str | None = None
    failure_class: str | None = None
    asset_category: str | None = None
    reported_by_role: str | None = None
    measure: str = "count"


@app.post("/api/workorders/find")
async def workorders_find(body: WorkOrderQueryIn):
    """Count or cost-aggregate work orders under allowlisted filters only.

    Body: {level?, status?, priority?, failure_class?, asset_category?,
    reported_by_role?, measure}. Each filter is validated against the fixed
    WO_* allowlists (priority is the friendly word high/normal/low mapped to the
    stored 2/3/4; asset_category maps through ALLOWED_CATEGORIES to asset_type).
    measure is 'count' (default -> {count, breakdown_by_status}) or 'cost'
    (-> {count, total_cost, total_labor_hours, avg_cost}). No SQL, column, or
    value is ever taken from the caller.
    """
    measure = (body.measure or "count").lower()
    if measure not in WO_ALLOWED_MEASURES:
        raise HTTPException(status_code=400, detail="unsupported measure")

    filters: dict = {}
    where: list[str] = []
    params: dict = {}

    if body.level is not None:
        if body.level not in ALLOWED_LEVELS:
            raise HTTPException(status_code=400, detail="unsupported level")
        where.append("level_name = :level")
        params["level"] = body.level
        filters["level"] = body.level
    if body.status is not None:
        s = body.status.upper()
        if s not in WO_ALLOWED_STATUSES:
            raise HTTPException(status_code=400, detail="unsupported status")
        where.append("status = :status")
        params["status"] = s
        filters["status"] = s
    if body.priority is not None:
        p = WO_PRIORITY_WORDS.get(str(body.priority).lower())
        if p is None:
            raise HTTPException(status_code=400, detail="unsupported priority")
        where.append("priority = :priority")
        params["priority"] = p
        filters["priority"] = str(body.priority).lower()
    if body.failure_class is not None:
        f = body.failure_class.upper()
        if f not in WO_ALLOWED_FAILURE_CLASSES:
            raise HTTPException(
                status_code=400, detail="unsupported failure_class"
            )
        where.append("failure_class = :failure_class")
        params["failure_class"] = f
        filters["failure_class"] = f
    if body.asset_category is not None:
        if body.asset_category not in ALLOWED_CATEGORIES:
            raise HTTPException(
                status_code=400, detail="unsupported asset_category"
            )
        asset_types = ALLOWED_CATEGORIES[body.asset_category]
        type_list = ", ".join(f"'{t}'" for t in asset_types)
        where.append(f"asset_type IN ({type_list})")
        filters["asset_category"] = body.asset_category
    if body.reported_by_role is not None:
        r = body.reported_by_role.upper()
        if r not in WO_ALLOWED_REPORTED_BY:
            raise HTTPException(
                status_code=400, detail="unsupported reported_by_role"
            )
        where.append("reported_by_role = :reported_by_role")
        params["reported_by_role"] = r
        filters["reported_by_role"] = r

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    try:
        if measure == "cost":
            query = (
                "SELECT COUNT(*) AS n, "
                "ROUND(SUM(total_cost), 2) AS total_cost, "
                "ROUND(SUM(labor_hours), 1) AS total_labor_hours, "
                "ROUND(AVG(total_cost), 2) AS avg_cost "
                f"FROM {WORKORDERS_TABLE}{where_sql}"
            )
            row = (await asyncio.to_thread(query_rows, query, params)
                   or [{}])[0]
            result = {
                "measure": "cost",
                "filters": filters,
                "count": int(row.get("n") or 0),
                "total_cost": row.get("total_cost"),
                "total_labor_hours": row.get("total_labor_hours"),
                "avg_cost": row.get("avg_cost"),
            }
        else:
            total_q = (
                f"SELECT COUNT(*) AS n FROM {WORKORDERS_TABLE}{where_sql}"
            )
            total = int((await asyncio.to_thread(query_rows, total_q, params)
                         or [{}])[0].get("n") or 0)
            bd_q = (
                "SELECT status, COUNT(*) AS n "
                f"FROM {WORKORDERS_TABLE}{where_sql} "
                "GROUP BY status ORDER BY n DESC"
            )
            bd = await asyncio.to_thread(query_rows, bd_q, params)
            result = {
                "measure": "count",
                "filters": filters,
                "count": total,
                "breakdown_by_status": {r["status"]: int(r["n"]) for r in bd},
            }
    except Exception as exc:
        log.warning("workorder_query_failed")
        raise HTTPException(status_code=502, detail="query failed") from exc

    log.info("workorder_query_served measure=%s filters=%s n=%d",
             measure, filters, result.get("count", 0))
    return result


# ------------------------------------------------- Asset spatial query
#
# The MCP spatial tools (find_nearest, find_extreme) call this route to rank
# elements by TRUE 3D geometry. Databricks' ST_* functions are 2D only (they
# ignore Z), which is wrong for a 3D model, so SQL here only *filters* the
# candidate set (by allowlisted category/level); the 3D distance/volume/extent
# math is done in Python from each element's bounding box (see geometry.py).
#
# The caller NEVER supplies SQL, a column name, or coordinates: only an
# allowlisted op (+ optional allowlisted category/level, a reference external_id
# for "nearest", and a bounded limit). Same hackathon posture as the other
# /api/assets routes; responses carry only ids + the computed metric.
GEOM_TABLE = "users.loren_routh.mech_family_instances"

ALLOWED_SPATIAL_OPS = {
    "nearest",      # closest to a reference element (needs reference_external_id)
    "northmost", "southmost", "eastmost", "westmost",  # directional extreme
    "largest", "smallest",                             # by 3D bbox volume
}
_DIRECTION_FOR_OP = {
    "northmost": "north",
    "southmost": "south",
    "eastmost": "east",
    "westmost": "west",
}
MAX_SPATIAL_LIMIT = 50


class SpatialQueryIn(BaseModel):
    op: str
    category: str | None = None
    level: str | None = None
    reference_external_id: str | None = None
    limit: int = 5


def _fetch_boxes(
    category: str | None, level: str | None, assets_only: bool = False
) -> list[dict]:
    """Return candidate assets with their bounding box, filtered in SQL only.

    Joins maximo_assets to the geometry table so only real, tracked assets come
    back. category/level are validated against the fixed allowlists by the
    caller; level is bound as a parameter, the asset_type IN-list is built from
    the server-side ALLOWED_CATEGORIES constant (never caller input).

    assets_only restricts to the asset_types named in ALLOWED_CATEGORIES, so
    size/direction queries rank real equipment (an AHU, a transformer) rather
    than duct fittings or pipe reducers that happen to have a bounding box.
    """
    where_extra = ""
    params: dict = {}
    if category is not None:
        type_list = ", ".join(f"'{t}'" for t in ALLOWED_CATEGORIES[category])
        where_extra += f" AND a.asset_type IN ({type_list})"
    elif assets_only:
        all_types = sorted(
            {t for types in ALLOWED_CATEGORIES.values() for t in types}
        )
        type_list = ", ".join(f"'{t}'" for t in all_types)
        where_extra += f" AND a.asset_type IN ({type_list})"
    if level is not None:
        where_extra += " AND f.level_name = :level"
        params["level"] = level

    query = (
        "SELECT a.source_unique_id AS external_id, a.asset_type, a.description, "
        "       f.level_name, "
        "       f.bounding_box.min_x AS min_x, f.bounding_box.min_y AS min_y, "
        "       f.bounding_box.min_z AS min_z, f.bounding_box.max_x AS max_x, "
        "       f.bounding_box.max_y AS max_y, f.bounding_box.max_z AS max_z "
        f"FROM {ASSETS_TABLE} a "
        f"JOIN {GEOM_TABLE} f ON a.source_unique_id = f.unique_id "
        "WHERE a.source_unique_id IS NOT NULL "
        "  AND f.bounding_box.min_x IS NOT NULL"
        f"{where_extra}"
    )
    return query_rows(query, params)


def _box_of(row: dict) -> tuple:
    return (
        row["min_x"], row["min_y"], row["min_z"],
        row["max_x"], row["max_y"], row["max_z"],
    )


def _as_element(row: dict, metric_name: str | None = None, metric_value: float = 0.0) -> dict:
    el = {
        "external_id": row["external_id"],
        "asset_type": row["asset_type"],
        "description": row["description"],
        "level_name": row["level_name"],
    }
    if metric_name is not None:
        el[metric_name] = round(metric_value, 3)
    return el


@app.post("/api/assets/spatial")
async def assets_spatial(body: SpatialQueryIn):
    """Rank elements by true 3D geometry; return their externalIds.

    Body: {op, category?, level?, reference_external_id?, limit}. `op` must be
    in ALLOWED_SPATIAL_OPS; category/level (if given) in the fixed allowlists.
    "nearest" requires reference_external_id (the element to measure from).
    Returns {op, elements:[{external_id, asset_type, description, level_name,
    <metric>}]} where <metric> is distance_ft, extent_ft, or volume_cuft. No
    SQL, URL, path, or coordinate is ever taken from the caller.
    """
    op = body.op
    if op not in ALLOWED_SPATIAL_OPS:
        log.warning("spatial_query_rejected")
        raise HTTPException(status_code=400, detail="unsupported op")
    if body.category is not None and body.category not in ALLOWED_CATEGORIES:
        log.warning("spatial_query_rejected")
        raise HTTPException(status_code=400, detail="unsupported category")
    if body.level is not None and body.level not in ALLOWED_LEVELS:
        log.warning("spatial_query_rejected")
        raise HTTPException(status_code=400, detail="unsupported level")
    if op == "nearest" and not body.reference_external_id:
        raise HTTPException(
            status_code=400, detail="reference_external_id required for nearest"
        )

    limit = max(1, min(int(body.limit or 5), MAX_SPATIAL_LIMIT))

    # Size/direction ops rank real equipment only; "nearest" measures against
    # whatever set the caller asked for (plus the reference element).
    assets_only = op != "nearest"
    try:
        rows = await asyncio.to_thread(
            _fetch_boxes, body.category, body.level, assets_only
        )
    except Exception as exc:
        log.warning("spatial_query_failed")
        raise HTTPException(status_code=502, detail="query failed") from exc

    if op == "nearest":
        ref_id = body.reference_external_id
        ref = next((r for r in rows if r["external_id"] == ref_id), None)
        if ref is None:
            # The reference may be excluded by the category/level filter; fetch
            # it on its own so "nearest X to Y" works when Y isn't an X.
            try:
                extra = await asyncio.to_thread(_fetch_boxes, None, None)
            except Exception as exc:
                log.warning("spatial_query_failed")
                raise HTTPException(
                    status_code=502, detail="query failed"
                ) from exc
            ref = next(
                (r for r in extra if r["external_id"] == ref_id), None
            )
        if ref is None:
            raise HTTPException(
                status_code=404, detail="reference element not found"
            )
        ref_box = _box_of(ref)
        ranked = sorted(
            (r for r in rows if r["external_id"] != ref_id),
            key=lambda r: geometry.aabb_distance(ref_box, _box_of(r)),
        )
        elements = [
            _as_element(r, "distance_ft", geometry.aabb_distance(ref_box, _box_of(r)))
            for r in ranked[:limit]
        ]
    elif op in _DIRECTION_FOR_OP:
        # The directional "extent" is a raw axis COORDINATE (can be negative,
        # e.g. south/west of the origin). It is only a sort key, never shown to
        # the caller as if it were a length. The result is simply *which*
        # element is most extreme, so we return the standard element shape.
        direction = _DIRECTION_FOR_OP[op]
        ranked = sorted(
            rows,
            key=lambda r: geometry.aabb_extreme_value(_box_of(r), direction),
            reverse=True,
        )
        elements = [_as_element(r) for r in ranked[:limit]]
    else:  # largest / smallest
        ranked = sorted(
            rows,
            key=lambda r: geometry.aabb_volume(_box_of(r)),
            reverse=(op == "largest"),
        )
        elements = [
            _as_element(r, "volume_cuft", geometry.aabb_volume(_box_of(r)))
            for r in ranked[:limit]
        ]

    log.info("spatial_query_served op=%s n=%d", op, len(elements))
    return {"op": op, "elements": elements}


# ---------------------------------------------------------- clearance checks
#
# NEC 110.26(A) working-space rule for electrical panels, 120V-250V to ground:
#   depth  >= 36 in  (3 ft) in front of the working face
#   width  >= max(30 in, panel width)
#   height >= 78 in  (6.5 ft) from the floor
# These are the only hard numbers; they define the code requirement, not an
# expected answer. v1 covers electrical panels only; any other category returns
# "rule not defined". A lookup table of codes is a future improvement.
NEC_110_26 = {
    "depth_ft": 36.0 / 12.0,       # 3.0
    "width_ft": 30.0 / 12.0,       # 2.5 (floor; widened to the panel's own width)
    "height_ft": 78.0 / 12.0,      # 6.5
}
CLEARANCE_CATEGORIES = {"electrical_panels"}

# A mounting surface counts as "in contact" when its box is within this gap of
# the panel's box. Walls/columns are approximated, so a hair of slack is sane.
MOUNT_CONTACT_FT = 0.25

# Categories excluded from the intrusion check because their stored bounding box
# overstates the element's real physical footprint, producing false positives.
# Lighting fixtures are the known case: Revit reports a loose envelope (mounting
# hardware / aiming geometry) far larger than the visible lamp, so a box that
# "overlaps" a clearance zone can be empty space in practice. Dropping them
# until we can test against true (Model Derivative) mesh geometry is the honest,
# conservative choice; it is a known limitation, not a hidden tuning constant.
CLEARANCE_INTRUDER_EXCLUDE_CATEGORIES = {"Lighting Fixtures"}

# Tables whose elements can be a mounting surface a panel is fixed to. Walls are
# stored as a center line + thickness; columns already carry a bounding box.
_WALL_TABLE = "users.loren_routh.mech_walls"
_COLUMN_TABLES = (
    "users.loren_routh.mech_architectural_columns",
    "users.loren_routh.mech_structural_columns",
)
_MULLION_TABLE = "users.loren_routh.mech_curtain_wall_mullions"
_LEVELS_TABLE = "users.loren_routh.mech_levels"

# A panel can sit a little below its floor's nominal elevation (mounting slop);
# allow this much tolerance when assigning a panel to the level it stands on.
_FLOOR_TOLERANCE_FT = 0.5


class ClearanceQueryIn(BaseModel):
    category: str
    external_id: str | None = None
    # Optional: resolve the panel by a description substring instead of an id,
    # so the agent can ask for "the circuit breaker switchboard" without having
    # to carry the exact externalId. Matched against the panel's description,
    # restricted to the clearance category. Must resolve to exactly one panel.
    description: str | None = None


class InRoomQueryIn(BaseModel):
    room_external_id: str | None = None
    room_number: str | None = None
    category: str | None = None
    limit: int = 50


_ROOMS_TABLE = "users.loren_routh.mech_rooms"
MAX_INROOM_LIMIT = 500


def _num(v, default: float = 0.0) -> float:
    return float(v) if v is not None else default


def _fetch_panel(external_id: str) -> dict | None:
    """The one panel asset + its bounding box, or None if not a tracked asset."""
    rows = query_rows(
        "SELECT a.source_unique_id AS external_id, a.asset_type, a.description, "
        "       f.level_name, "
        "       f.bounding_box.min_x AS min_x, f.bounding_box.min_y AS min_y, "
        "       f.bounding_box.min_z AS min_z, f.bounding_box.max_x AS max_x, "
        "       f.bounding_box.max_y AS max_y, f.bounding_box.max_z AS max_z "
        f"FROM {ASSETS_TABLE} a "
        f"JOIN {GEOM_TABLE} f ON a.source_unique_id = f.unique_id "
        "WHERE a.source_unique_id = :eid "
        "  AND f.bounding_box.min_x IS NOT NULL",
        {"eid": external_id},
    )
    return rows[0] if rows else None


def _resolve_panel_ids_by_description(
    category: str, description: str
) -> list[str]:
    """externalIds of panels in `category` whose description matches `description`.

    A case-insensitive substring match, restricted to the category's asset
    types, ordered for stable results. Used so the agent can target a panel by
    name ('circuit breaker switchboard') without carrying an externalId. The
    caller decides what to do when this is empty or ambiguous (not exactly one).
    """
    asset_types = ALLOWED_CATEGORIES.get(category, [])
    if not asset_types or not description.strip():
        return []
    type_list = ", ".join(f"'{t}'" for t in asset_types)
    rows = query_rows(
        "SELECT source_unique_id AS external_id "
        f"FROM {ASSETS_TABLE} "
        "WHERE source_unique_id IS NOT NULL "
        f"  AND asset_type IN ({type_list}) "
        "  AND lower(description) LIKE :descr "
        "ORDER BY description",
        {"descr": f"%{description.strip().lower()}%"},
    )
    return [r["external_id"] for r in rows if r.get("external_id")]


def _fetch_level_elevations() -> list[float]:
    """Building level elevations (feet), ascending. The 'floor' a panel stands
    on is the highest level at or below the panel's base."""
    rows = query_rows(
        f"SELECT elevation FROM {_LEVELS_TABLE} "
        "WHERE elevation IS NOT NULL ORDER BY elevation"
    )
    return [_num(r["elevation"]) for r in rows]


def _fetch_level_elevation_map() -> dict[str, float]:
    """Level name -> elevation (feet). Used to recover the true vertical extent
    of elements (columns) whose stored bounding box is in Family-local Z (always
    starting at 0.0) rather than project Z. The level fields carry the real
    placement; see _column_true_z."""
    rows = query_rows(
        f"SELECT level_name, elevation FROM {_LEVELS_TABLE} "
        "WHERE elevation IS NOT NULL AND level_name IS NOT NULL"
    )
    return {r["level_name"]: _num(r["elevation"]) for r in rows}


def _floor_z_for(min_z: float, elevations: list[float]) -> float:
    """The floor elevation the panel stands on: the highest level at or below
    the panel base (with a small tolerance), or the lowest level as a floor."""
    if not elevations:
        return min_z
    floor = elevations[0]
    for elev in elevations:
        if min_z >= elev - _FLOOR_TOLERANCE_FT:
            floor = elev
        else:
            break
    return floor


def _fetch_surface_boxes() -> list[dict]:
    """All candidate mounting surfaces (walls + columns) as boxes + metadata.

    Each entry: {kind, box}. Walls become boxes via wall_to_box (center line
    inflated by half-thickness, extruded by height); columns already have a
    bounding box. Read-only, no caller input reaches the query.
    """
    surfaces: list[dict] = []

    walls = query_rows(
        "SELECT thickness, base_offset, user_height, actual_height, "
        "       default_height, "
        "       location.start_x AS start_x, location.start_y AS start_y, "
        "       location.start_z AS start_z, location.end_x AS end_x, "
        "       location.end_y AS end_y, location.end_z AS end_z "
        f"FROM {_WALL_TABLE} "
        "WHERE location.start_x IS NOT NULL"
    )
    for w in walls:
        height = (
            _num(w["user_height"])
            or _num(w["actual_height"])
            or _num(w["default_height"])
        )
        box = geometry.wall_to_box(
            _num(w["start_x"]), _num(w["start_y"]), _num(w["start_z"]),
            _num(w["end_x"]), _num(w["end_y"]), _num(w["end_z"]),
            _num(w["thickness"]), _num(w["base_offset"]), height,
        )
        surfaces.append({"kind": "wall", "box": box})

    for table in _COLUMN_TABLES:
        kind = "architectural_column" if "architectural" in table else "structural_column"
        cols = query_rows(
            "SELECT bounding_box.min_x AS min_x, bounding_box.min_y AS min_y, "
            "       bounding_box.min_z AS min_z, bounding_box.max_x AS max_x, "
            "       bounding_box.max_y AS max_y, bounding_box.max_z AS max_z "
            f"FROM {table} "
            "WHERE bounding_box.min_x IS NOT NULL"
        )
        for c in cols:
            box = (
                _num(c["min_x"]), _num(c["min_y"]), _num(c["min_z"]),
                _num(c["max_x"]), _num(c["max_y"]), _num(c["max_z"]),
            )
            surfaces.append({"kind": kind, "box": box})

    return surfaces


def _column_true_z(
    base_level: str | None, base_off, top_level: str | None, top_off,
    fallback_min_z: float, fallback_max_z: float, level_map: dict[str, float],
) -> tuple[float, float]:
    """A column's TRUE vertical extent in project Z.

    Column bounding boxes are stored in Family-local Z (every column's box
    starts at 0.0), so the box alone cannot tell a Level 1 segment from a
    Level 2/3 one. The real placement lives in the level fields: a segment runs
    from base_level + base_offset up to top_level + top_offset. We recover that
    here so an upper-floor segment no longer falsely overlaps a lower floor's
    clearance zone. Falls back to the stored box Z if levels are missing.
    """
    base_elev = level_map.get(base_level or "")
    top_elev = level_map.get(top_level or "")
    if base_elev is None or top_elev is None:
        return fallback_min_z, fallback_max_z
    true_min = base_elev + _num(base_off)
    true_max = top_elev + _num(top_off)
    if true_max < true_min:
        true_min, true_max = true_max, true_min
    return true_min, true_max


def _fetch_intrusion_candidates(exclude_id: str) -> list[dict]:
    """Every element that can physically block a clearance zone, minus self and
    any category whose box is known to overstate its footprint.

    Sources:
      - the family-instance geometry table (equipment, fixtures, etc.),
      - the structural + architectural COLUMN tables, and
      - the curtain-wall MULLION table.
    Columns and mullions live in their own tables (not the family-instance
    table), so without this they would be invisible to the intrusion check.

    Columns get a TRUE-Z transform (see _column_true_z): their stored bounding
    box is in Family-local Z (always starting at 0.0), so an upper-floor column
    segment would otherwise falsely overlap a lower floor's zone. We replace the
    box Z with the real extent derived from the level fields. Mullions and
    family instances already carry real project Z, so they are used as-is.

    Categories in CLEARANCE_INTRUDER_EXCLUDE_CATEGORIES are filtered out because
    their stored bounding box is a loose envelope (see that constant).
    """
    level_map = _fetch_level_elevation_map()

    rows = query_rows(
        "SELECT f.unique_id AS external_id, f.category_name AS category, "
        "       f.bounding_box.min_x AS min_x, f.bounding_box.min_y AS min_y, "
        "       f.bounding_box.min_z AS min_z, f.bounding_box.max_x AS max_x, "
        "       f.bounding_box.max_y AS max_y, f.bounding_box.max_z AS max_z "
        f"FROM {GEOM_TABLE} f "
        "WHERE f.bounding_box.min_x IS NOT NULL "
        "  AND f.unique_id <> :eid",
        {"eid": exclude_id},
    )
    candidates = [
        dict(r)
        for r in rows
        if r["category"] not in CLEARANCE_INTRUDER_EXCLUDE_CATEGORIES
    ]

    # Columns: fetch with level fields and rewrite box Z to the true extent.
    for table in _COLUMN_TABLES:
        kind = (
            "Architectural Columns"
            if "architectural" in table
            else "Structural Columns"
        )
        cols = query_rows(
            "SELECT unique_id, base_level, base_offset, top_level, top_offset, "
            "       bounding_box.min_x AS min_x, bounding_box.min_y AS min_y, "
            "       bounding_box.min_z AS min_z, bounding_box.max_x AS max_x, "
            "       bounding_box.max_y AS max_y, bounding_box.max_z AS max_z "
            f"FROM {table} "
            "WHERE bounding_box.min_x IS NOT NULL "
            "  AND unique_id <> :eid",
            {"eid": exclude_id},
        )
        for c in cols:
            true_min_z, true_max_z = _column_true_z(
                c["base_level"], c["base_offset"],
                c["top_level"], c["top_offset"],
                _num(c["min_z"]), _num(c["max_z"]), level_map,
            )
            candidates.append({
                "external_id": c["unique_id"],
                "category": kind,
                "min_x": c["min_x"], "min_y": c["min_y"], "min_z": true_min_z,
                "max_x": c["max_x"], "max_y": c["max_y"], "max_z": true_max_z,
            })

    # Curtain-wall mullions: their box Z is already real project Z.
    mullions = query_rows(
        "SELECT unique_id, "
        "       bounding_box.min_x AS min_x, bounding_box.min_y AS min_y, "
        "       bounding_box.min_z AS min_z, bounding_box.max_x AS max_x, "
        "       bounding_box.max_y AS max_y, bounding_box.max_z AS max_z "
        f"FROM {_MULLION_TABLE} "
        "WHERE bounding_box.min_x IS NOT NULL "
        "  AND unique_id <> :eid",
        {"eid": exclude_id},
    )
    for m in mullions:
        candidates.append({
            "external_id": m["unique_id"],
            "category": "Curtain Wall Mullions",
            "min_x": m["min_x"], "min_y": m["min_y"], "min_z": m["min_z"],
            "max_x": m["max_x"], "max_y": m["max_y"], "max_z": m["max_z"],
        })

    return candidates


def _intruders_per_zone(
    zones: list[tuple], candidates: list[dict]
) -> list[list[dict]]:
    """For each zone, the elements whose box overlaps it (touching is not
    intruding). Returns one list per zone, in zone order."""
    per_zone: list[list[dict]] = []
    for z in zones:
        hits: list[dict] = []
        for c in candidates:
            cbox = (
                _num(c["min_x"]), _num(c["min_y"]), _num(c["min_z"]),
                _num(c["max_x"]), _num(c["max_y"]), _num(c["max_z"]),
            )
            if geometry.aabb_overlap(z, cbox):
                hits.append(
                    {"external_id": c["external_id"], "category": c["category"]}
                )
        per_zone.append(hits)
    return per_zone


@app.post("/api/assets/clearance")
async def assets_clearance(body: ClearanceQueryIn):
    """Check NEC 110.26 working clearance for one electrical panel.

    Body: {category, external_id}. category must be in CLEARANCE_CATEGORIES
    (electrical_panels only in v1); external_id is the panel to check.

    Finds the panel's mounting surface (wall or column) by box adjacency:
      - a surface within MOUNT_CONTACT_FT -> DIRECTIONAL check: the clearance
        zone projects out from the face opposite the surface (a real NEC 110.26
        front-clearance check).
      - no surface in contact -> ALL-AROUND check: a maintenance-access
        heuristic on all four horizontal sides (NOT the literal front-only
        code rule), honestly labeled as such.

    Returns {external_id, mode, rule, mounting_surface, clearance, intruders,
    clear}. `clear` is True iff nothing intrudes. No SQL/coordinate is ever
    taken from the caller.
    """
    if body.category not in ALLOWED_CATEGORIES:
        raise HTTPException(status_code=400, detail="unsupported category")

    # Resolve the target panel id. Either an explicit external_id, or a
    # description substring that must match exactly one panel in the category.
    panel_id = body.external_id
    if not panel_id:
        if not body.description:
            raise HTTPException(
                status_code=400,
                detail="external_id or description is required",
            )
        try:
            matches = await asyncio.to_thread(
                _resolve_panel_ids_by_description,
                body.category, body.description,
            )
        except Exception as exc:
            log.warning("clearance_resolve_failed")
            raise HTTPException(status_code=502, detail="query failed") from exc
        if not matches:
            raise HTTPException(
                status_code=404,
                detail="no panel matches that description",
            )
        if len(matches) > 1:
            # Ambiguous: let the caller disambiguate rather than guessing one.
            raise HTTPException(
                status_code=409,
                detail=f"{len(matches)} panels match that description",
            )
        panel_id = matches[0]

    if body.category not in CLEARANCE_CATEGORIES:
        return {
            "external_id": panel_id,
            "mode": "none",
            "rule": "clearance rule not defined for this category",
            "mounting_surface": None,
            "clearance": None,
            "intruders": [],
            "clear": None,
        }

    try:
        panel = await asyncio.to_thread(_fetch_panel, panel_id)
    except Exception as exc:
        log.warning("clearance_query_failed")
        raise HTTPException(status_code=502, detail="query failed") from exc
    if panel is None:
        raise HTTPException(status_code=404, detail="panel not found")

    pbox = _box_of(panel)
    depth = NEC_110_26["depth_ft"]
    width = NEC_110_26["width_ft"]
    height = NEC_110_26["height_ft"]

    try:
        surfaces = await asyncio.to_thread(_fetch_surface_boxes)
        candidates = await asyncio.to_thread(
            _fetch_intrusion_candidates, panel_id
        )
        elevations = await asyncio.to_thread(_fetch_level_elevations)
    except Exception as exc:
        log.warning("clearance_query_failed")
        raise HTTPException(status_code=502, detail="query failed") from exc

    # The clearance stands on the floor the panel is mounted above, not on the
    # panel's own (elevated) base. NEC 110.26 measures height from the floor.
    floor_z = _floor_z_for(pbox[2], elevations)

    # Nearest mounting surface by 3D gap between boxes.
    nearest = None
    best_gap = float("inf")
    for s in surfaces:
        gap = geometry.aabb_distance(pbox, s["box"])
        if gap < best_gap:
            best_gap, nearest = gap, s

    face = None
    if nearest is not None and best_gap <= MOUNT_CONTACT_FT:
        face = geometry.mounting_face(pbox, nearest["box"])

    if face is not None:
        # Directional: the real NEC 110.26 front-clearance check.
        zone = geometry.clearance_box(pbox, face, depth, width, height, floor_z)
        zones = [zone]
        mounting_surface = {
            "kind": nearest["kind"],
            "distance_ft": round(best_gap, 3),
        }
        mode = "directional"
        rule = (
            "NEC 110.26(A) working clearance "
            f"({depth:.2f} ft deep x >= {width:.2f} ft wide x {height:.2f} ft tall), "
            f"projected from the {face} face (opposite the mounting {nearest['kind']})"
        )
    else:
        # All-around: freestanding/floor-mounted, or a surface was in contact
        # but the face was degenerate. Maintenance-access heuristic, NOT the
        # literal NEC front-only rule.
        zones = geometry.clearance_boxes_all_around(
            pbox, depth, width, height, floor_z
        )
        mounting_surface = None
        if nearest is not None:
            mounting_surface = {
                "kind": nearest["kind"],
                "distance_ft": round(best_gap, 3),
            }
        mode = "all_around"
        rule = (
            "No mounting surface in contact; all-around maintenance-access "
            f"check ({depth:.2f} ft on each horizontal side, {height:.2f} ft "
            "tall). This is NEC-derived guidance, not the literal 110.26 "
            "front-only rule."
        )

    per_zone = _intruders_per_zone(zones, candidates)

    # Overall intruder set (union across zones), de-duplicated for the panel's
    # text answer and the viewer's red tint.
    all_intruders: dict[str, dict] = {}
    for hits in per_zone:
        for h in hits:
            all_intruders[h["external_id"]] = h
    intruders = list(all_intruders.values())

    def _z(z: tuple, clear: bool) -> dict:
        return {
            "min": [round(z[0], 3), round(z[1], 3), round(z[2], 3)],
            "max": [round(z[3], 3), round(z[4], 3), round(z[5], 3)],
            "clear": clear,
        }

    zone_dicts = [
        _z(z, len(per_zone[i]) == 0) for i, z in enumerate(zones)
    ]

    log.info(
        "clearance_served mode=%s intruders=%d", mode, len(intruders)
    )
    return {
        "external_id": panel_id,
        "asset_type": panel["asset_type"],
        "description": panel["description"],
        "mode": mode,
        "rule": rule,
        "mounting_surface": mounting_surface,
        "clearance": {"zones": zone_dicts},
        "intruders": intruders,
        "clear": len(intruders) == 0,
        # Flat shape the viewer's draw_clearance command consumes directly:
        # a panel id, the zone boxes (each with its own clear flag so the
        # viewer can color zones independently), and the intruding element ids.
        "draw": {
            "panel_external_id": panel_id,
            "zones": zone_dicts,
            "intruder_ids": [i["external_id"] for i in intruders],
            "clear": len(intruders) == 0,
        },
    }


def _fetch_room(
    room_external_id: str | None, room_number: str | None
) -> dict | None:
    """One room's footprint (WKT) + height + floor elevation, or None.

    Looks the room up by durable unique_id first, then by room_number. Returns
    the raw WKT string, the room height, and the floor Z (location_point z, the
    room's level elevation). Read-only; both lookups are bound parameters.
    """
    if room_external_id:
        rows = query_rows(
            "SELECT unique_id, room_name, room_number, level_name, "
            "       height, unbounded_height, "
            "       location_point.z AS floor_z, wkt "
            f"FROM {_ROOMS_TABLE} "
            "WHERE unique_id = :rid AND wkt IS NOT NULL",
            {"rid": room_external_id},
        )
        if rows:
            return rows[0]
    if room_number:
        rows = query_rows(
            "SELECT unique_id, room_name, room_number, level_name, "
            "       height, unbounded_height, "
            "       location_point.z AS floor_z, wkt "
            f"FROM {_ROOMS_TABLE} "
            "WHERE room_number = :rnum AND wkt IS NOT NULL",
            {"rnum": room_number},
        )
        if rows:
            return rows[0]
    return None


@app.post("/api/assets/in-room")
async def assets_in_room(body: InRoomQueryIn):
    """Which tracked assets fall inside a room's 3D volume.

    Body: {room_external_id? , room_number?, category?, limit}. One of
    room_external_id / room_number identifies the room; category (if given) is
    validated against ALLOWED_CATEGORIES. Returns {room, count, elements:[...]}.

    Membership is centroid-in-prism: the room's WKT footprint is extruded from
    its floor elevation up by its height, and an asset is "in" the room when its
    bounding-box center lies inside that prism (see geometry.centroid_in_room).
    All geometry math is done here in Python; no SQL ST_* (those are 2D-only).
    """
    if body.category is not None and body.category not in ALLOWED_CATEGORIES:
        raise HTTPException(status_code=400, detail="unsupported category")
    if not body.room_external_id and not body.room_number:
        raise HTTPException(
            status_code=400, detail="room_external_id or room_number required"
        )

    limit = max(1, min(int(body.limit or 50), MAX_INROOM_LIMIT))

    try:
        room = await asyncio.to_thread(
            _fetch_room, body.room_external_id, body.room_number
        )
    except Exception as exc:
        log.warning("in_room_query_failed")
        raise HTTPException(status_code=502, detail="query failed") from exc
    if room is None:
        raise HTTPException(status_code=404, detail="room not found")

    ring = geometry.room_ring_from_wkt(room["wkt"])
    if len(ring) < 3:
        raise HTTPException(
            status_code=422, detail="room footprint could not be parsed"
        )
    floor_z = _num(room["floor_z"])
    height = _num(room["height"]) or _num(room["unbounded_height"])

    try:
        rows = await asyncio.to_thread(_fetch_boxes, body.category, None, True)
    except Exception as exc:
        log.warning("in_room_query_failed")
        raise HTTPException(status_code=502, detail="query failed") from exc

    elements = [
        _as_element(r)
        for r in rows
        if geometry.centroid_in_room(_box_of(r), ring, floor_z, height)
    ]
    elements = elements[:limit]

    log.info("in_room_served count=%d", len(elements))
    return {
        "room": {
            "external_id": room["unique_id"],
            "room_name": room["room_name"],
            "room_number": room["room_number"],
            "level_name": room["level_name"],
        },
        "count": len(elements),
        "elements": elements,
    }


@app.post("/api/viewer/command")
async def viewer_command(body: ViewerCommandIn):
    """Inject one viewer command, to be broadcast to every connected viewer.

    This is the single HTTP entry point the MCP server uses to drive the 3D
    view. HACKATHON POSTURE: unauthenticated over public pilot data (see
    SECURITY-NOTES.md). Defense in depth: the command name must be in the fixed
    ALLOWED_COMMANDS allowlist here, and every argument is re-validated on the
    browser side before anything executes. No tool-supplied URL, path, or code
    is ever accepted.
    """
    name = body.command
    if name not in ALLOWED_COMMANDS:
        # Fail safe; never forward an unknown command. Do not echo the raw name.
        log.warning("viewer_command_rejected")
        raise HTTPException(status_code=400, detail="unknown command")
    await hub.broadcast({"command": name, "args": body.args or {}})
    return {"accepted": True}


@app.post("/api/chat")
async def chat(body: ChatIn):
    """Role A + B: ask the Genie Agent, then drive the viewer.

    Returns {reply, field_order, elements, rooms}. The requesting page applies
    the highlight directly from this response; we also broadcast the same
    highlight over the command channel so any other connected viewer (or a
    future MCP-driven session) stays in sync. Honors the zoom rule: a Genie
    answer only highlights in place, never moves the camera.
    """
    question = (body.query or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="query is required")
    try:
        result = await asyncio.to_thread(genie_answer, question)
    except Exception as exc:
        # Sanitized: do not leak the upstream failure detail to the client.
        log.warning("genie_request_failed")
        raise HTTPException(
            status_code=502, detail="Genie request failed"
        ) from exc

    await _broadcast_highlight(result)
    return result


async def _broadcast_highlight(result: dict):
    """Derive highlight commands from a Genie result and broadcast them.

    Local, deterministic mapping only â€” no extra model call. Emits highlight
    commands (no camera move) so other connected viewers mirror the answer.
    """
    elements = result.get("elements") or []
    external_ids = [
        e.get("external_id") for e in elements if e.get("external_id")
    ]
    if external_ids:
        await hub.broadcast({
            "command": "highlight_elements",
            "args": {"external_ids": external_ids},
        })


# ------------------------------------------------------------ levels data
#
# The viewer's Levels menu assigns every element to a building level. Most
# elements carry their level in the SVF (Constraints -> Level / Reference
# Level), but some categories (curtain-wall mullions/panels, walls, columns,
# doors, etc.) only expose it in Databricks. This endpoint returns, read-only:
#   - element_levels: unique_id -> level_name, from the mech_* category tables
#     (authoritative; used as a fallback before any Z-inference), and
#   - levels: the building levels with their elevations (for Z-inference of the
#     remaining stragglers the SVF and DB both leave unassigned).
# All columns are allowlisted; no user input reaches the query.

# Category tables that carry a per-element level_name keyed by unique_id.
_LEVEL_SOURCE_TABLES = (
    "mech_family_instances",
    "mech_walls",
    "mech_curtain_walls",
    "mech_curtain_wall_panels",
    "mech_curtain_wall_mullions",
    "mech_doors",
    "mech_floors",
    "mech_ceilings",
    "mech_structural_columns",
    "mech_architectural_columns",
)

_LEVELS_CACHE = {}
CACHE_SECONDS = 60


@app.get("/api/levels")
def levels_data():
    """Return per-element levels (from DB) + building-level elevations."""
    cached = _LEVELS_CACHE.get("payload")
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]

    element_levels = {}
    for table in _LEVEL_SOURCE_TABLES:
        try:
            rows = query_rows(
                f"SELECT unique_id, level_name "
                f"FROM users.loren_routh.{table} "
                f"WHERE level_name IS NOT NULL"
            )
        except Exception:
            # A missing/variant table must not break the whole response.
            log.warning("levels_source_skipped")
            continue
        for r in rows:
            uid = r.get("unique_id")
            name = r.get("level_name")
            if uid and name:
                element_levels[uid] = name

    try:
        level_rows = query_rows(
            "SELECT level_name, elevation "
            "FROM users.loren_routh.mech_levels "
            "WHERE elevation IS NOT NULL "
            "ORDER BY elevation"
        )
    except Exception as exc:
        log.warning("levels_elevations_failed")
        raise HTTPException(
            status_code=502, detail="Levels query failed"
        ) from exc

    levels = [
        {"level_name": r.get("level_name"), "elevation": r.get("elevation")}
        for r in level_rows
        if r.get("level_name") is not None
    ]

    payload = {"levels": levels, "element_levels": element_levels}
    _LEVELS_CACHE["payload"] = (time.time(), payload)
    log.info(
        "levels_served levels=%d element_levels=%d",
        len(levels), len(element_levels),
    )
    return payload


# Fast agent path (USAi LLM calls the asset tools). Additive: imported after the
# allowlists/hub it reuses are defined; does not touch the Genie path.
from agent import router as agent_router  # noqa: E402

app.include_router(agent_router)

# Serve APS packages hosted in a UC Volume (additive; parallel to /models and
# the Genie path). The viewer can load from /dbx-models/... instead of local
# files. See volume_models.py.
from volume_models import router as volume_models_router  # noqa: E402

app.include_router(volume_models_router)


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "viewer.html")


@app.get("/health")
def health():
    return {"status": "ok"}


app.mount(
    "/static",
    StaticFiles(directory=HERE / "static"),
    name="static",
)

app.mount(
    "/models",
    StaticFiles(directory=HERE / "ModelDerivatives"),
    name="models",
)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("DATABRICKS_APP_PORT", "8000")),
    )
