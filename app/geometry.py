"""Pure 3D geometry helpers for the spatial asset queries.

Databricks ST_* functions are 2D-only (they ignore Z), which is wrong for a 3D
building model: two elements stacked on different floors would read as distance
zero. So we pull raw coordinates from Databricks and do the real 3D math here.

Everything in this module is pure (no I/O, no DB, no network): it takes plain
numbers and returns plain numbers, so it can be unit-tested on its own.

Coordinate system: the model's local system, in feet, origin at the lower-left.
+Y is North, -X is West. All boxes are axis-aligned (AABBs) given as
(min_x, min_y, min_z, max_x, max_y, max_z).
"""
from __future__ import annotations

import math
import re

Box = tuple[float, float, float, float, float, float]


def aabb_distance(a: Box, b: Box) -> float:
    """True 3D nearest-point distance between two axis-aligned boxes.

    This is the gap between the two boxes' closest points: 0 if they overlap or
    touch, otherwise the straight-line distance across the empty space between
    them. For each axis we take how far apart the boxes are on that axis (0 if
    they overlap on it), then combine the three axis gaps with Pythagoras.
    """
    gap_x = max(0.0, b[0] - a[3], a[0] - b[3])
    gap_y = max(0.0, b[1] - a[4], a[1] - b[4])
    gap_z = max(0.0, b[2] - a[5], a[2] - b[5])
    return math.sqrt(gap_x * gap_x + gap_y * gap_y + gap_z * gap_z)


def aabb_volume(box: Box) -> float:
    """3D volume of an axis-aligned box (dx * dy * dz), never negative."""
    dx = max(0.0, box[3] - box[0])
    dy = max(0.0, box[4] - box[1])
    dz = max(0.0, box[5] - box[2])
    return dx * dy * dz


def aabb_extreme_value(box: Box, direction: str) -> float:
    """The box's edge coordinate in a compass direction, for ranking.

    'north' -> max_y, 'south' -> min_y, 'east' -> max_x, 'west' -> min_x.
    A larger returned value is "more extreme" in that direction, so callers
    sort DESC to get the most-northern/eastern element first (for south/west we
    negate so the same DESC sort still puts the most-extreme element first).
    """
    if direction == "north":
        return box[4]          # max_y
    if direction == "south":
        return -box[1]         # -min_y (most southern = smallest min_y)
    if direction == "east":
        return box[3]          # max_x
    if direction == "west":
        return -box[0]         # -min_x (most western = smallest min_x)
    raise ValueError(f"unknown direction: {direction}")


def aabb_overlap(a: Box, b: Box) -> bool:
    """True if two axis-aligned boxes overlap (share interior volume).

    Touching faces (zero-thickness contact) do NOT count as overlap, so a panel
    flush against its mounting wall is not reported as intruding into itself.
    """
    return (
        a[0] < b[3] and b[0] < a[3]
        and a[1] < b[4] and b[1] < a[4]
        and a[2] < b[5] and b[2] < a[5]
    )


def wall_to_box(
    start_x: float, start_y: float, start_z: float,
    end_x: float, end_y: float, end_z: float,
    thickness: float, base_offset: float, height: float,
) -> Box:
    """Approximate a wall as an axis-aligned box.

    A wall is stored as a center line (start->end) plus a thickness and height.
    We inflate the line's X/Y extent by half the thickness (the buffer) so the
    box spans the wall's real footprint, and extrude it vertically from its base
    to base+height. Good enough to test which surface an element is mounted on.
    """
    half = 0.5 * max(0.0, thickness)
    base = base_offset + min(start_z, end_z)
    h = height if height and height > 0 else 12.0
    return (
        min(start_x, end_x) - half,
        min(start_y, end_y) - half,
        base,
        max(start_x, end_x) + half,
        max(start_y, end_y) + half,
        base + h,
    )


def mounting_face(equipment: Box, surface: Box) -> str | None:
    """Which direction the equipment's working face points (away from a surface).

    An electrical panel is a thin box: its flat front/back is on its THINNER
    horizontal footprint axis, and the working clearance projects straight out
    from that front face. So we:

      1. pick the panel's thinner horizontal axis (X vs Y) as the front/back
         axis -- that is where the working face lies, and
      2. along that axis, put the mounting surface on one side and point the
         working face the OTHER way (out into the room, away from the wall or
         column the panel is fixed to).

    Returns "north"/"south"/"east"/"west", or None if the footprint is square
    (no clear front) or the surface is centered on the panel (degenerate), so
    the caller can fall back to an all-around check.
    """
    eq_cx = 0.5 * (equipment[0] + equipment[3])
    eq_cy = 0.5 * (equipment[1] + equipment[4])
    sf_cx = 0.5 * (surface[0] + surface[3])
    sf_cy = 0.5 * (surface[1] + surface[4])

    width_x = equipment[3] - equipment[0]
    width_y = equipment[4] - equipment[1]
    if abs(width_x - width_y) < 1e-9:
        return None  # square footprint: no distinguishable front face

    if width_x < width_y:
        # Thin in X -> front/back face along X. dx = panel_center - surface_center.
        # dx > 0 means the panel sits on the +X side of the surface, so the
        # surface is to the panel's west and the working face points east.
        dx = eq_cx - sf_cx
        if abs(dx) < 1e-9:
            return None
        return "east" if dx > 0 else "west"
    # Thin in Y -> front/back face along Y. dy > 0 means the panel sits on the
    # +Y side of the surface, so the surface is south and the face points north.
    dy = eq_cy - sf_cy
    if abs(dy) < 1e-9:
        return None
    return "north" if dy > 0 else "south"


def clearance_box(
    equipment: Box, face: str, depth: float, width: float, height: float,
    floor_z: float,
) -> Box:
    """Build the required clear-space box projected from a working face.

    `depth` extends outward from `face`; `width` is centered on the equipment
    (widened to at least the equipment's own width). The zone stands ON THE
    FLOOR: it rises from `floor_z` up to at least `floor_z + height`, and at
    least to the top of the equipment (NEC 110.26: 78 in high OR the height of
    the equipment, whichever is greater). All in feet.
    """
    eq_min_x, eq_min_y, eq_min_z, eq_max_x, eq_max_y, eq_max_z = equipment
    cx = 0.5 * (eq_min_x + eq_max_x)
    cy = 0.5 * (eq_min_y + eq_max_y)
    eq_w_x = eq_max_x - eq_min_x
    eq_w_y = eq_max_y - eq_min_y
    base_z = floor_z
    top_z = max(floor_z + height, eq_max_z)

    if face in ("north", "south"):
        half_w = 0.5 * max(width, eq_w_x)
        min_x, max_x = cx - half_w, cx + half_w
        if face == "north":
            min_y, max_y = eq_max_y, eq_max_y + depth
        else:
            min_y, max_y = eq_min_y - depth, eq_min_y
    else:  # east / west
        half_w = 0.5 * max(width, eq_w_y)
        min_y, max_y = cy - half_w, cy + half_w
        if face == "east":
            min_x, max_x = eq_max_x, eq_max_x + depth
        else:
            min_x, max_x = eq_min_x - depth, eq_min_x
    return (min_x, min_y, base_z, max_x, max_y, top_z)


def clearance_boxes_all_around(
    equipment: Box, depth: float, width: float, height: float, floor_z: float
) -> list[Box]:
    """The four horizontal clearance boxes (N/S/E/W) for a freestanding unit.

    Used when no mounting surface pins a single working face (floor-standing or
    strut-mounted gear serviced from multiple sides). This is a maintenance-
    access heuristic, not the literal NEC 110.26 front-only rule.
    """
    return [
        clearance_box(equipment, f, depth, width, height, floor_z)
        for f in ("north", "south", "east", "west")
    ]


# ------------------------------------------------------------- room containment
#
# Rooms arrive as WKT polygons: CURVEPOLYGON Z ( COMPOUNDCURVE Z ( LINESTRING Z
# (x y z, x y z, ...), LINESTRING Z (...), ... ) ). In this model every boundary
# is made of straight LINESTRING segments (no arcs), so the footprint is a
# simple polygon we can read off as an ordered ring of (x, y) vertices. We test
# room membership with centroid-in-prism: the element's bounding-box center must
# fall inside the footprint (point-in-polygon) AND within the room's vertical
# band [floor_z, floor_z + height].

_WKT_COORD = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_LINESTRING_RE = re.compile(r"LINESTRING\s*Z?\s*\(([^)]*)\)", re.IGNORECASE)


def room_ring_from_wkt(wkt: str) -> list[tuple[float, float]]:
    """Ordered (x, y) vertices of a room footprint, parsed from its WKT.

    Collects the start points of the boundary's LINESTRING segments in order.
    Because the segments form a closed loop (each one's end is the next one's
    start), the start points alone are the polygon ring. Returns [] if nothing
    parseable is found. Z is dropped here; the caller enforces the Z band.
    """
    ring: list[tuple[float, float]] = []
    for seg in _LINESTRING_RE.findall(wkt or ""):
        pts = [p.strip() for p in seg.split(",") if p.strip()]
        if not pts:
            continue
        first = pts[0].split()
        if len(first) >= 2:
            try:
                ring.append((float(first[0]), float(first[1])))
            except ValueError:
                continue
    return ring


def point_in_polygon(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    """True if point (x, y) is inside the polygon `ring` (ray-casting test).

    `ring` is an ordered list of (x, y) vertices; it is treated as closed
    (the last vertex connects back to the first). Points exactly on an edge may
    read either way -- acceptable for room membership. Fewer than 3 vertices is
    never inside.
    """
    n = len(ring)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        # Does a horizontal ray to the right from (x, y) cross edge i-j?
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def centroid_in_room(
    box: Box, ring: list[tuple[float, float]], floor_z: float, height: float
) -> bool:
    """True if the box's center is inside the room prism.

    The room prism is the footprint `ring` extruded from `floor_z` up by
    `height`. Membership = center point inside the footprint (point-in-polygon)
    AND center Z within [floor_z, floor_z + height]. A small tolerance absorbs
    rounding at the floor/ceiling.
    """
    cx = 0.5 * (box[0] + box[3])
    cy = 0.5 * (box[1] + box[4])
    cz = 0.5 * (box[2] + box[5])
    if not (floor_z - 1e-6 <= cz <= floor_z + height + 1e-6):
        return False
    return point_in_polygon(cx, cy, ring)

