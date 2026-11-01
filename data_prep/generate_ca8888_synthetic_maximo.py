"""
Generate synthetic Maximo-style facilities data from MECH Revit model JSON.

MECH-ONLY VERSION.
Single model => single coordinate system, single set of level names.
No ARCH/STRUC loading, no cross-model level crosswalk, no cross-model geometry.

Fixes applied vs. prior version:
  1. Duct Fittings / Pipe Fittings no longer hard-excluded.
  2. Air Terminals now classified (were silently falling through).
  3. Mechanical Equipment / M_WSHP now classified (were silently falling through).
  4. Dead normalize_level_name() mapping (1FL/2F/RF/PHF) removed - belonged to another building.
  5. Records with no level_name (e.g. Electrical Fixtures) get level derived from z elevation.
  6. Classifier restructured to category-first dispatch with per-category fallback,
     so nothing is ever silently dropped.
  7. symbol_* dimensions now read from type parameters, with bounding_box fallback.
  8. classification_audit.json emitted so future silent drops announce themselves.
"""

import json
import math
import hashlib
import re
from pathlib import Path
from datetime import date, timedelta

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

BUILDING_ID = "CA8888"

ROOT = Path("JSON_Model_Data")
OUT = Path("CA8888_SYNTHETIC_MAXIMO")
OUT.mkdir(exist_ok=True)

MECH = "CA8888_MECH_JSON"
MODEL_KEY = "MECH"

# --- TUNABLE DEFAULTS (flip these as needed) -------------------------

# Max XY distance (model units, ft) an asset may be from a space centroid
# and still be assigned that space. Beyond this -> level-only, LOW confidence.
# Prevents confidently-wrong room assignment.
MAX_SPACE_MATCH_DIST = 60.0

# Asset types that are NOT routinely maintained as discrete CMMS assets.
# They still get asset records (needed for model location Q&A) but are
# excluded from PM schedules and work orders, and flagged maintainable=False.
NON_MAINTAINABLE_TYPES = {
    "DUCT_FITTING", "DUCT_FITTING_ELBOW", "DUCT_FITTING_TEE",
    "DUCT_FITTING_TRANSITION", "DUCT_FITTING_TAKEOFF", "DUCT_FITTING_CAP",
    "DUCT_FITTING_UNION", "DUCT_FITTING_CROSS", "DUCT_FITTING_OFFSET",
    "PIPE_FITTING", "PIPE_FITTING_ELBOW", "PIPE_FITTING_TEE",
    "PIPE_FITTING_COUPLING", "PIPE_FITTING_CAP", "PIPE_FITTING_UNION",
    "PIPE_FITTING_FLANGE", "PIPE_FITTING_REDUCER", "PIPE_FITTING_CROSS",
}

# Generate work orders / PMs only for maintainable assets.
SUPPRESS_WO_FOR_NON_MAINTAINABLE = True
SUPPRESS_PM_FOR_NON_MAINTAINABLE = True

# Panel -> receptacle POWERS relationships are NOT generated:
# no Panel / Circuit Number parameters are present in the extract, so any
# such relationship would be a nearest-neighbour guess. Set True only if
# real circuit data becomes available.
GENERATE_PANEL_TO_RECEPTACLE_RELATIONSHIPS = False

# Asset ID sequence width. 4 digits because duct fittings can exceed 999
# for one type on one level.
SEQ_WIDTH = 4

# ---------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------

def stable_int(s, mod=10_000):
    h = hashlib.sha256(str(s).encode("utf-8")).hexdigest()
    return int(h[:12], 16) % mod

def parse_json_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def find_files(folder, stem_prefix):
    d = ROOT / folder
    if not d.exists():
        print(f"WARNING: folder not found: {d}")
        return []
    files = []
    for p in d.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in [".json", ".txt"]:
            continue
        if p.stem == stem_prefix or p.stem.startswith(stem_prefix + "_part"):
            files.append(p)
    return sorted(files, key=lambda p: p.name)

def load_records(folder, stem_prefix, required=False):
    paths = find_files(folder, stem_prefix)
    if not paths:
        msg = f"{'ERROR' if required else 'NOTE'}: no files matched {folder}/{stem_prefix}*.json"
        print(msg)
        return []
    rows = []
    for p in paths:
        try:
            data = parse_json_file(p)
            if isinstance(data, list):
                rows.extend(data)
            elif isinstance(data, dict):
                rows.append(data)
        except Exception as e:
            print(f"WARNING: could not read {p}: {e}")
    return rows

def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

def dedupe_by_unique_id(rows, label="records"):
    out = {}
    canon = {}
    for r in rows:
        uid = r.get("unique_id")
        if not uid:
            continue
        c = canonical_json(r)
        if uid not in out:
            out[uid] = r
            canon[uid] = c
        elif canon[uid] != c:
            print(f"WARNING: duplicate unique_id, differing content in {label}: {uid}; keeping first")
    return list(out.values())

def sanitize_id(value):
    if value is None or str(value).strip() == "":
        return "UNKNOWN"
    s = str(value).strip().upper()
    s = s.replace("/", "-").replace("\\", "-")
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^A-Z0-9\-_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "UNKNOWN"

def get_nested(d, *keys):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur

def param_value(rec, name, default=None):
    params = rec.get("parameters")
    if not isinstance(params, list):
        return default
    for p in params:
        if isinstance(p, dict) and p.get("name") == name:
            v = p.get("value", default)
            if v == "":
                return default
            return v
    return default

def distance_xy(a, b):
    ax, ay = a
    bx, by = b
    if ax is None or ay is None or bx is None or by is None:
        return float("inf")
    return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)

def has_word(text, *words):
    """Word-boundary match. Avoids 'fan' matching inside unrelated words,
    'heu' matching mid-string, 'panel' matching 'Panel-Mounted', etc."""
    for w in words:
        if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text):
            return True
    return False

def has_sub(text, *subs):
    """Plain substring match, for multiword phrases where boundaries are safe."""
    for s in subs:
        if s in text:
            return True
    return False

# ---------------------------------------------------------------------
# Level handling (MECH-only: names are used as-is, no crosswalk)
# ---------------------------------------------------------------------

def normalize_level_name(level):
    """MECH-only: there is exactly one naming convention, so this is a
    pass-through normaliser (trim only). The old 1FL/2F/RF/PHF mapping
    belonged to a different building model and has been removed."""
    if level is None:
        return None
    s = str(level).strip()
    return s or None

def loc_level_id(level_name):
    return f"{BUILDING_ID}-LEVEL-{sanitize_id(level_name)}"

def room_location_id(level_name, room_number, seq=None):
    lvl = sanitize_id(level_name)
    rn = sanitize_id(room_number if room_number not in [None, ""] else "UNNUMBERED")
    base = f"{BUILDING_ID}-{lvl}-ROOM-{rn}"
    if seq and seq > 1:
        base += f"-{seq:03d}"
    return base

# ---------------------------------------------------------------------
# Record field accessors
# ---------------------------------------------------------------------

def effective_level_name(rec):
    """Order matters. Duct/pipe fittings commonly carry Reference Level;
    some families carry Schedule Level. Electrical Fixtures in this extract
    carry no level at all - those fall through to z-elevation derivation."""
    return (
        rec.get("level_name")
        or param_value(rec, "Level")
        or param_value(rec, "Reference Level")
        or param_value(rec, "Schedule Level")
        or param_value(rec, "Base Level")
    )

def record_location_x(rec):
    return get_nested(rec, "location", "x")

def record_location_y(rec):
    return get_nested(rec, "location", "y")

def record_location_z(rec):
    return get_nested(rec, "location", "z")

def record_room_name(rec):
    """Some records (e.g. M_Duplex Receptacle) carry a direct room reference.
    This is far better than nearest-centroid guessing when it is present."""
    return (
        rec.get("room_name")
        or param_value(rec, "Room Name")
        or param_value(rec, "Room")
    )

def record_room_number(rec):
    return (
        rec.get("room_number")
        or param_value(rec, "Room Number")
    )

def space_number(sp):
    return (
        sp.get("space_number")
        or sp.get("room_number")
        or param_value(sp, "Room Number")
        or param_value(sp, "Number")
    )

def space_name(sp):
    return (
        sp.get("space_name")
        or sp.get("room_name")
        or param_value(sp, "Room Name")
        or param_value(sp, "Name")
    )

def space_level_name(sp):
    return sp.get("level_name") or param_value(sp, "Level")

def space_xy(sp):
    x = get_nested(sp, "location_point", "x")
    y = get_nested(sp, "location_point", "y")
    if x is None or y is None:
        x = get_nested(sp, "location", "x")
        y = get_nested(sp, "location", "y")
    return x, y

# ---------------------------------------------------------------------
# Load source data (MECH only)
# ---------------------------------------------------------------------

print("Loading MECH source data...")

project_info = load_records(MECH, "project_info")
levels = load_records(MECH, "levels", required=True)

mech_spaces = dedupe_by_unique_id(load_records(MECH, "spaces"), "MECH spaces")
mech_rooms = dedupe_by_unique_id(load_records(MECH, "rooms"), "MECH rooms")

family_instances = dedupe_by_unique_id(
    load_records(MECH, "family_instances", required=True),
    "MECH family_instances",
)

# Type-level parameter data. The extract may expose this as family_symbols.json
# or as a separate per-type parameter file; try several stems and merge.
symbol_rows = []
for stem in ["family_symbols", "family_types", "type_parameters", "types"]:
    rows = load_records(MECH, stem)
    if rows:
        print(f"  loaded {len(rows)} records from {stem}*.json")
        symbol_rows.extend(rows)
symbols = dedupe_by_unique_id(symbol_rows, "MECH type/symbol records")

# Index symbols by every identifier they expose. The extract mixes GUID
# unique_ids with integer element ids, so a single-key join is unreliable.
symbols_by_key = {}
for s in symbols:
    for k in [s.get("unique_id"), s.get("type_id"), s.get("id"), s.get("element_id")]:
        if k not in [None, ""]:
            symbols_by_key.setdefault(str(k), s)

print(f"  project_info: {len(project_info)}")
print(f"  levels: {len(levels)}")
print(f"  spaces: {len(mech_spaces)}")
print(f"  rooms: {len(mech_rooms)}")
print(f"  family_instances: {len(family_instances)}")
print(f"  type/symbol records: {len(symbols)} (indexed under {len(symbols_by_key)} keys)")

# ---------------------------------------------------------------------
# Generate locations.json
# ---------------------------------------------------------------------

locations = []
project = project_info[0] if project_info else {}

locations.append({
    "location_id": BUILDING_ID,
    "parent_location_id": None,
    "location_type": "BUILDING",
    "building_id": BUILDING_ID,

    "model_folder": MECH,
    "model_key": MODEL_KEY,
    "source_file": "project_info.json",
    "source_unique_id": project.get("unique_id"),

    "level_name": None,
    "room_number": None,
    "description": f"Synthetic facilities location for building {BUILDING_ID}",
    "status": "OPERATING",

    "x": None, "y": None, "z": None,
    "area": None, "volume": None,
})

# Level records, sorted by elevation so the hierarchy reads bottom-up.
level_rows = []
for lvl in levels:
    ln = lvl.get("level_name")
    if not ln:
        continue
    level_rows.append(lvl)
level_rows.sort(key=lambda l: (l.get("elevation") if l.get("elevation") is not None else 0.0))

level_elevations = []  # [(elevation, level_name)] for z-based derivation
for lvl in level_rows:
    ln = lvl.get("level_name")
    elev = lvl.get("elevation")
    if elev is not None:
        level_elevations.append((float(elev), ln))

    locations.append({
        "location_id": loc_level_id(ln),
        "parent_location_id": BUILDING_ID,
        "location_type": "LEVEL",
        "building_id": BUILDING_ID,

        "model_folder": MECH,
        "model_key": MODEL_KEY,
        "source_file": "levels.json",
        "source_unique_id": lvl.get("unique_id"),

        "level_name": ln,
        "room_number": None,
        "description": f"Level {ln}",
        "status": "OPERATING",

        "x": None, "y": None, "z": elev,
        "area": None, "volume": None,
    })

level_elevations.sort()
known_level_names = {l.get("level_name") for l in level_rows if l.get("level_name")}

def level_from_elevation(z):
    """Derive a level for records with no level parameter (Electrical Fixtures).
    Returns the highest level whose elevation is at or below z, with a small
    tolerance so items mounted slightly below a slab still read correctly."""
    if z is None or not level_elevations:
        return None
    tol = 1.0
    chosen = None
    for elev, name in level_elevations:
        if z >= elev - tol:
            chosen = name
        else:
            break
    return chosen if chosen is not None else level_elevations[0][1]

# Spatial records become ROOM locations. Prefer rooms.json if present;
# otherwise spaces.json is the spatial source of truth.
if mech_rooms:
    spatial_records = mech_rooms
    spatial_source_file = "rooms.json"
    print("  using MECH rooms.json as spatial source")
else:
    spatial_records = mech_spaces
    spatial_source_file = "spaces.json"
    print("  using MECH spaces.json as spatial source (no rooms.json found)")

room_locations = []
room_key_counts = {}

for r in spatial_records:
    ln = normalize_level_name(space_level_name(r))
    rn = space_number(r)
    rname = space_name(r)

    key = (ln, rn)
    room_key_counts[key] = room_key_counts.get(key, 0) + 1
    seq = room_key_counts[key]

    rx, ry = space_xy(r)
    rz = get_nested(r, "location_point", "z") or get_nested(r, "location", "z")

    lid = room_location_id(ln, rn, seq if seq > 1 else None)
    parent = loc_level_id(ln) if ln in known_level_names else BUILDING_ID

    row = {
        "location_id": lid,
        "parent_location_id": parent,
        "location_type": "ROOM",
        "building_id": BUILDING_ID,

        "model_folder": MECH,
        "model_key": MODEL_KEY,
        "source_file": spatial_source_file,
        "source_unique_id": r.get("unique_id"),

        "level_name": ln,
        "room_number": rn,
        "description": rname,
        "status": "OPERATING",

        "x": rx, "y": ry, "z": rz,
        "area": r.get("area"),
        "volume": r.get("volume"),
    }
    locations.append(row)
    room_locations.append(row)

location_ids = {l["location_id"] for l in locations}

# Lookup indexes for room assignment
rooms_by_level = {}
rooms_by_name = {}
rooms_by_level_name = {}

for rr in room_locations:
    ln = rr.get("level_name")
    if ln:
        rooms_by_level.setdefault(ln, []).append(rr)

    desc = rr.get("description")
    if desc not in [None, ""]:
        k = str(desc).strip().upper()
        rooms_by_name.setdefault(k, []).append(rr)
        if ln:
            rooms_by_level_name.setdefault((ln, k), []).append(rr)

print(f"  locations built: {len(locations)} "
      f"({len(room_locations)} rooms across {len(rooms_by_level)} levels)")

# ---------------------------------------------------------------------
# Classification: category-first dispatch
#
# Every handler ends in a fallback return, so a record whose category is
# recognised can never be silently dropped. Only categories in
# EXCLUDED_CATEGORIES are intentionally discarded, and non-MEP/annotation
# categories fall through to an explicit "unhandled" tally in the audit.
# ---------------------------------------------------------------------

EXCLUDED_CATEGORIES = {
    "title blocks", "view titles", "callout heads", "grid heads",
    "level heads", "generic annotations", "text notes", "dimensions",
    "levels", "rooms", "spaces", "areas",
    "walls", "floors", "ceilings", "roofs", "doors", "windows",
    "curtain panels", "curtain wall mullions",
    "structural columns", "architectural columns", "structural framing",
    "ducts", "duct placeholders", "flex ducts",
    "pipes", "pipe placeholders", "flex pipes",
    "cable trays", "cable tray fittings", "conduits", "conduit fittings",
    "wires", "electrical circuits",
    "duct insulations", "duct linings", "pipe insulations",
    "lines", "detail items",
}

# Context objects that are modelled geometry but not assets.
CONTEXT_PHRASES = (
    "service space", "equipment base", "equipment foundation",
    "sleeve", "housekeeping pad", "access panel opening",
)


def classify_lighting(text):
    if has_sub(text, "exit sign"):
        return ("EXIT_SIGN_LIGHT", "Life Safety", "LIFE_SAFETY.EXIT_SIGN")
    if has_sub(text, "emergency"):
        return ("EMERGENCY_LIGHT", "Life Safety", "LIFE_SAFETY.EMERGENCY_LIGHT")
    if has_sub(text, "stairway", "stairwell") or has_word(text, "stair"):
        return ("STAIRWAY_LIGHT", "Life Safety", "LIFE_SAFETY.STAIRWAY_LIGHT")
    if has_sub(text, "wall light", "bracket type", "sconce"):
        return ("LIGHTING_FIXTURE", "Lighting", "LIGHTING.FIXTURE.WALL")
    if has_sub(text, "downlight"):
        return ("LIGHTING_FIXTURE", "Lighting", "LIGHTING.FIXTURE.DOWNLIGHT")
    if has_sub(text, "troffer"):
        return ("LIGHTING_FIXTURE", "Lighting", "LIGHTING.FIXTURE.TROFFER")
    if has_sub(text, "pendant"):
        return ("LIGHTING_FIXTURE", "Lighting", "LIGHTING.FIXTURE.PENDANT")
    if has_sub(text, "recessed"):
        return ("LIGHTING_FIXTURE", "Lighting", "LIGHTING.FIXTURE.RECESSED")
    if has_sub(text, "surface-mounted", "surface mount", "surface mounted"):
        return ("LIGHTING_FIXTURE", "Lighting", "LIGHTING.FIXTURE.SURFACE_MOUNT")
    return ("LIGHTING_FIXTURE", "Lighting", "LIGHTING.FIXTURE")


def classify_electrical_fixture(text):
    """Electrical Fixtures category. Handled BEFORE any generic 'light' or
    'panel' text test, which is what previously stole records like
    'Lighting Control Switch' into LIGHTING_FIXTURE."""
    if has_sub(text, "gfci", "gfi", "ground fault"):
        return ("GFCI_RECEPTACLE", "Electrical", "ELECTRICAL.RECEPTACLE.GFCI")
    if has_sub(text, "floor box", "floor receptacle", "floor outlet"):
        return ("FLOOR_BOX", "Electrical", "ELECTRICAL.FLOOR_BOX")
    if has_sub(text, "quadruplex", "quad receptacle"):
        return ("QUADRUPLEX_RECEPTACLE", "Electrical", "ELECTRICAL.RECEPTACLE.QUADRUPLEX")
    if has_sub(text, "duplex"):
        return ("DUPLEX_RECEPTACLE", "Electrical", "ELECTRICAL.RECEPTACLE.DUPLEX")
    if has_sub(text, "receptacle", "outlet", "socket"):
        return ("RECEPTACLE", "Electrical", "ELECTRICAL.RECEPTACLE")
    if has_sub(text, "dimmer"):
        return ("DIMMER_SWITCH", "Electrical", "ELECTRICAL.SWITCH.DIMMER")
    if has_sub(text, "occupancy sensor", "vacancy sensor"):
        return ("OCCUPANCY_SENSOR", "Electrical", "ELECTRICAL.SENSOR.OCCUPANCY")
    if has_sub(text, "switch"):
        return ("LIGHT_SWITCH", "Electrical", "ELECTRICAL.SWITCH")
    if has_sub(text, "junction box", "j-box", "jbox"):
        return ("JUNCTION_BOX", "Electrical", "ELECTRICAL.JUNCTION_BOX")
    if has_sub(text, "data", "telephone", "voice", "comm outlet", "rj45"):
        return ("DATA_OUTLET", "Electrical", "ELECTRICAL.DATA_OUTLET")
    if has_sub(text, "disconnect"):
        return ("DISCONNECT_SWITCH", "Electrical", "ELECTRICAL.DISCONNECT")
    if has_sub(text, "contactor", "relay", "starter"):
        return ("MOTOR_STARTER", "Electrical", "ELECTRICAL.MOTOR_STARTER")
    if has_sub(text, "thermostat"):
        return ("THERMOSTAT", "Electrical", "ELECTRICAL.THERMOSTAT")
    # Category-level fallback: an Electrical Fixture we don't recognise is
    # still an electrical fixture. Never dropped.
    return ("ELECTRICAL_FIXTURE", "Electrical", "ELECTRICAL.FIXTURE")


def classify_air_terminal(text):
    """Air Terminals category.
    VAV / fan-powered boxes keep their equipment classification per your
    instruction, and are tested first."""
    if has_word(text, "vav") or has_sub(text, "variable air volume"):
        return ("VAV", "HVAC", "HVAC.VAV")
    if has_sub(text, "fan powered", "fan-powered", "parallel fan", "series fan"):
        return ("VAV_FAN_POWERED", "HVAC", "HVAC.VAV.FAN_POWERED")
    if has_sub(text, "cav", "constant air volume"):
        return ("CAV_TERMINAL", "HVAC", "HVAC.CAV")

    if has_sub(text, "linear slot", "slot diffuser", "linear diffuser"):
        return ("LINEAR_SLOT_DIFFUSER", "HVAC", "HVAC.AIR_TERMINAL.LINEAR_SLOT")
    if has_sub(text, "exhaust"):
        return ("EXHAUST_GRILLE", "HVAC", "HVAC.AIR_TERMINAL.EXHAUST")
    if has_sub(text, "return"):
        return ("RETURN_GRILLE", "HVAC", "HVAC.AIR_TERMINAL.RETURN")
    if has_sub(text, "transfer"):
        return ("TRANSFER_GRILLE", "HVAC", "HVAC.AIR_TERMINAL.TRANSFER")
    if has_sub(text, "outside air", "outdoor air", "intake", "louver"):
        return ("AIR_INTAKE_LOUVER", "HVAC", "HVAC.AIR_TERMINAL.INTAKE")
    if has_sub(text, "supply"):
        return ("SUPPLY_DIFFUSER", "HVAC", "HVAC.AIR_TERMINAL.SUPPLY")
    if has_sub(text, "diffuser"):
        return ("SUPPLY_DIFFUSER", "HVAC", "HVAC.AIR_TERMINAL.SUPPLY")
    if has_sub(text, "grille", "register"):
        return ("RETURN_GRILLE", "HVAC", "HVAC.AIR_TERMINAL.RETURN")
    return ("AIR_TERMINAL", "HVAC", "HVAC.AIR_TERMINAL")


def classify_mechanical_equipment(text):
    """Mechanical Equipment category.
    WSHP is tested before any 'pump' test. A water source heat pump is a
    packaged refrigerant unit, not a plumbing pump, and the abbreviation
    'M_WSHP' contains no searchable word, which is why these previously
    fell through every branch and produced zero records."""
    if has_word(text, "wshp") or has_sub(text, "water source heat pump", "water-source heat pump"):
        return ("WSHP", "HVAC", "HVAC.WSHP")
    if has_word(text, "gshp") or has_sub(text, "ground source heat pump", "geothermal heat pump"):
        return ("GSHP", "HVAC", "HVAC.GSHP")
    if has_sub(text, "vrf outdoor", "outdoor unit", "condensing unit", "condenser unit"):
        return ("VRF_OUTDOOR_UNIT", "HVAC", "HVAC.VRF.OUTDOOR_UNIT")
    if has_sub(text, "vrf indoor", "indoor unit", "cassette", "ceiling concealed", "ductless"):
        return ("HVAC_INDOOR_UNIT", "HVAC", "HVAC.INDOOR_UNIT")
    if has_sub(text, "heat pump"):
        return ("HEAT_PUMP", "HVAC", "HVAC.HEAT_PUMP")
    if has_sub(text, "air handling", "air-handling") or has_word(text, "ahu"):
        return ("AHU", "HVAC", "HVAC.AHU")
    if has_sub(text, "rooftop unit") or has_word(text, "rtu"):
        return ("RTU", "HVAC", "HVAC.RTU")
    if has_sub(text, "energy recovery", "heat recovery") or has_word(text, "erv", "hrv"):
        return ("ENERGY_RECOVERY_UNIT", "HVAC", "HVAC.ENERGY_RECOVERY")
    if has_sub(text, "heat exchanger") or has_word(text, "heu", "hx"):
        return ("HEAT_EXCHANGER_UNIT", "HVAC", "HVAC.HEAT_EXCHANGER")
    if has_sub(text, "fan coil") or has_word(text, "fcu"):
        return ("FCU", "HVAC", "HVAC.FCU")
    if has_sub(text, "unit heater", "cabinet heater", "baseboard heater"):
        return ("UNIT_HEATER", "HVAC", "HVAC.UNIT_HEATER")
    if has_sub(text, "chiller"):
        return ("CHILLER", "HVAC", "HVAC.CHILLER")
    if has_sub(text, "cooling tower"):
        return ("COOLING_TOWER", "HVAC", "HVAC.COOLING_TOWER")
    if has_sub(text, "boiler"):
        return ("BOILER", "HVAC", "HVAC.BOILER")
    if has_sub(text, "humidifier"):
        return ("HUMIDIFIER", "HVAC", "HVAC.HUMIDIFIER")
    if has_sub(text, "dehumidifier"):
        return ("DEHUMIDIFIER", "HVAC", "HVAC.DEHUMIDIFIER")
    if has_sub(text, "water heater"):
        return ("WATER_HEATER", "Plumbing", "PLUMBING.WATER_HEATER")
    if has_sub(text, "exhaust") and has_word(text, "fan"):
        return ("EXHAUST_FAN", "HVAC", "HVAC.FAN.EXHAUST")
    if has_sub(text, "fire pump") or (has_sub(text, "fire") and has_word(text, "pump")):
        return ("FIRE_PUMP", "Fire Protection", "FIRE_PROTECTION.PUMP")
    if has_sub(text, "sump pump"):
        return ("SUMP_PUMP", "Plumbing", "PLUMBING.PUMP.SUMP")
    if has_sub(text, "circulator", "circulating pump"):
        return ("CIRCULATOR_PUMP", "HVAC", "MECHANICAL.PUMP.CIRCULATOR")
    if has_word(text, "pump"):
        return ("PUMP", "Plumbing", "MECHANICAL.PUMP")
    if has_word(text, "fan") or has_sub(text, "blower"):
        return ("FAN", "HVAC", "HVAC.FAN")
    if has_sub(text, "compressor"):
        return ("AIR_COMPRESSOR", "Plumbing", "MECHANICAL.AIR_COMPRESSOR")
    if has_sub(text, "tank"):
        return ("TANK", "Plumbing", "PLUMBING.TANK")
    return ("MECHANICAL_EQUIPMENT", "HVAC", "HVAC.EQUIPMENT")


def classify_duct_fitting(text):
    """Duct Fittings category. Previously hard-excluded.
    No System Classification parameter is present in this extract, so
    fittings are classified by geometry only - not by the service they carry."""
    if has_sub(text, "endcap", "end cap") or has_word(text, "cap"):
        return ("DUCT_FITTING_CAP", "HVAC", "HVAC.DUCT_FITTING.CAP")
    if has_sub(text, "elbow", "bend", "radius"):
        return ("DUCT_FITTING_ELBOW", "HVAC", "HVAC.DUCT_FITTING.ELBOW")
    if has_sub(text, "takeoff", "take-off", "tap", "spin-in"):
        return ("DUCT_FITTING_TAKEOFF", "HVAC", "HVAC.DUCT_FITTING.TAKEOFF")
    if has_sub(text, "transition", "reducer", "reduction"):
        return ("DUCT_FITTING_TRANSITION", "HVAC", "HVAC.DUCT_FITTING.TRANSITION")
    if has_sub(text, "wye", "y-fitting"):
        return ("DUCT_FITTING_TEE", "HVAC", "HVAC.DUCT_FITTING.WYE")
    if has_word(text, "tee") or has_sub(text, "tee "):
        return ("DUCT_FITTING_TEE", "HVAC", "HVAC.DUCT_FITTING.TEE")
    if has_sub(text, "cross"):
        return ("DUCT_FITTING_CROSS", "HVAC", "HVAC.DUCT_FITTING.CROSS")
    if has_sub(text, "offset"):
        return ("DUCT_FITTING_OFFSET", "HVAC", "HVAC.DUCT_FITTING.OFFSET")
    if has_sub(text, "union", "coupling", "connector"):
        return ("DUCT_FITTING_UNION", "HVAC", "HVAC.DUCT_FITTING.UNION")
    return ("DUCT_FITTING", "HVAC", "HVAC.DUCT_FITTING")


def classify_pipe_fitting(text):
    """Pipe Fittings category. Previously hard-excluded.
    Geometry-based only, same parameter limitation as duct fittings."""
    if has_sub(text, "elbow", "bend"):
        return ("PIPE_FITTING_ELBOW", "Plumbing", "PLUMBING.PIPE_FITTING.ELBOW")
    if has_word(text, "cap") or has_sub(text, "end cap"):
        return ("PIPE_FITTING_CAP", "Plumbing", "PLUMBING.PIPE_FITTING.CAP")
    if has_sub(text, "flange"):
        return ("PIPE_FITTING_FLANGE", "Plumbing", "PLUMBING.PIPE_FITTING.FLANGE")
    if has_sub(text, "reducer", "transition"):
        return ("PIPE_FITTING_REDUCER", "Plumbing", "PLUMBING.PIPE_FITTING.REDUCER")
    if has_sub(text, "wye", "y-fitting"):
        return ("PIPE_FITTING_TEE", "Plumbing", "PLUMBING.PIPE_FITTING.WYE")
    if has_word(text, "tee") or has_sub(text, "tee "):
        return ("PIPE_FITTING_TEE", "Plumbing", "PLUMBING.PIPE_FITTING.TEE")
    if has_sub(text, "cross"):
        return ("PIPE_FITTING_CROSS", "Plumbing", "PLUMBING.PIPE_FITTING.CROSS")
    if has_sub(text, "union"):
        return ("PIPE_FITTING_UNION", "Plumbing", "PLUMBING.PIPE_FITTING.UNION")
    if has_sub(text, "coupling", "connector", "adapter", "sweep"):
        return ("PIPE_FITTING_COUPLING", "Plumbing", "PLUMBING.PIPE_FITTING.COUPLING")
    return ("PIPE_FITTING", "Plumbing", "PLUMBING.PIPE_FITTING")


def classify_duct_accessory(text):
    if has_sub(text, "fire smoke damper", "fire/smoke damper", "combination fire"):
        return ("FIRE_SMOKE_DAMPER", "Fire Protection", "FIRE_PROTECTION.DAMPER.FIRE_SMOKE")
    if has_sub(text, "fire damper"):
        return ("FIRE_DAMPER", "Fire Protection", "FIRE_PROTECTION.DAMPER")
    if has_sub(text, "smoke damper"):
        return ("SMOKE_DAMPER", "Life Safety", "LIFE_SAFETY.DAMPER.SMOKE")
    if has_sub(text, "backdraft damper", "back draft"):
        return ("BACKDRAFT_DAMPER", "HVAC", "HVAC.DAMPER.BACKDRAFT")
    if has_sub(text, "volume control damper", "volume damper") or has_word(text, "vcd"):
        return ("DAMPER", "HVAC", "HVAC.DAMPER.VOLUME_CONTROL")
    if has_sub(text, "motor damper", "motorized damper", "motorised damper"):
        return ("DAMPER", "HVAC", "HVAC.DAMPER.MOTOR")
    if has_sub(text, "sound attenuator", "silencer"):
        return ("SOUND_ATTENUATOR", "HVAC", "HVAC.SOUND_ATTENUATOR")
    if has_sub(text, "access door", "access panel"):
        return ("DUCT_ACCESS_DOOR", "HVAC", "HVAC.DUCT_ACCESS_DOOR")
    if has_sub(text, "damper"):
        return ("DAMPER", "HVAC", "HVAC.DAMPER")
    return ("DUCT_ACCESSORY", "HVAC", "HVAC.DUCT_ACCESSORY")


def classify_pipe_accessory(text):
    if has_sub(text, "gate valve"):
        return ("VALVE", "Plumbing", "MECHANICAL.VALVE.GATE")
    if has_sub(text, "check valve"):
        return ("VALVE", "Plumbing", "MECHANICAL.VALVE.CHECK")
    if has_sub(text, "ball valve"):
        return ("VALVE", "Plumbing", "MECHANICAL.VALVE.BALL")
    if has_sub(text, "butterfly valve"):
        return ("VALVE", "Plumbing", "MECHANICAL.VALVE.BUTTERFLY")
    if has_sub(text, "balancing valve", "balance valve"):
        return ("VALVE", "Plumbing", "MECHANICAL.VALVE.BALANCING")
    if has_sub(text, "pressure reducing", "prv"):
        return ("VALVE", "Plumbing", "MECHANICAL.VALVE.PRESSURE_REDUCING")
    if has_sub(text, "backflow preventer", "backflow prevention"):
        return ("BACKFLOW_PREVENTER", "Plumbing", "PLUMBING.BACKFLOW_PREVENTER")
    if has_sub(text, "strainer"):
        return ("STRAINER", "Plumbing", "MECHANICAL.STRAINER")
    if has_sub(text, "expansion joint", "flexible connector", "ball joint"):
        return ("PIPE_ACCESSORY", "Plumbing", "PLUMBING.PIPE_ACCESSORY.EXPANSION")
    if has_sub(text, "valve"):
        return ("VALVE", "Plumbing", "MECHANICAL.VALVE")
    if has_sub(text, "meter", "gauge", "gage"):
        return ("PIPE_ACCESSORY", "Plumbing", "PLUMBING.PIPE_ACCESSORY.INSTRUMENT")
    return ("PIPE_ACCESSORY", "Plumbing", "PLUMBING.PIPE_ACCESSORY")


def classify_plumbing_fixture(text):
    if has_sub(text, "water closet", "toilet"):
        return ("WATER_CLOSET", "Plumbing", "PLUMBING.FIXTURE.WATER_CLOSET")
    if has_sub(text, "urinal"):
        return ("URINAL", "Plumbing", "PLUMBING.FIXTURE.URINAL")
    if has_sub(text, "lavatory", "sink", "basin"):
        return ("LAVATORY", "Plumbing", "PLUMBING.FIXTURE.LAVATORY")
    if has_sub(text, "drinking fountain", "water cooler", "bottle filler"):
        return ("DRINKING_FOUNTAIN", "Plumbing", "PLUMBING.FIXTURE.DRINKING_FOUNTAIN")
    if has_sub(text, "shower"):
        return ("SHOWER", "Plumbing", "PLUMBING.FIXTURE.SHOWER")
    if has_sub(text, "floor drain"):
        return ("FLOOR_DRAIN", "Plumbing", "PLUMBING.FLOOR_DRAIN")
    if has_sub(text, "roof drain"):
        return ("ROOF_DRAIN", "Plumbing", "PLUMBING.ROOF_DRAIN")
    if has_sub(text, "cleanout"):
        return ("CLEANOUT", "Plumbing", "PLUMBING.CLEANOUT")
    if has_sub(text, "mop sink", "service sink"):
        return ("SERVICE_SINK", "Plumbing", "PLUMBING.FIXTURE.SERVICE_SINK")
    if has_sub(text, "eye wash", "eyewash", "emergency shower"):
        return ("EMERGENCY_FIXTURE", "Life Safety", "LIFE_SAFETY.EMERGENCY_FIXTURE")
    if has_sub(text, "vent fitting", "vent cap", "vent through roof"):
        return ("VENT_FITTING", "Plumbing", "PLUMBING.VENT_FITTING")
    if has_sub(text, "trap primer"):
        return ("TRAP_PRIMER", "Plumbing", "PLUMBING.TRAP_PRIMER")
    return ("PLUMBING_FIXTURE", "Plumbing", "PLUMBING.FIXTURE")


def classify_electrical_equipment(text):
    if has_sub(text, "distribution board", "distribution panel", "switchboard", "switchgear"):
        return ("ELECTRICAL_PANEL", "Electrical", "ELECTRICAL.PANEL")
    if has_sub(text, "terminal board", "terminal cabinet"):
        return ("TERMINAL_BOARD", "Electrical", "ELECTRICAL.TERMINAL_BOARD")
    if has_sub(text, "control panel", "controller"):
        return ("CONTROL_PANEL", "Electrical", "ELECTRICAL.PANEL.CONTROL")
    if has_sub(text, "transformer"):
        return ("TRANSFORMER", "Electrical", "ELECTRICAL.TRANSFORMER")
    if has_sub(text, "generator"):
        return ("GENERATOR", "Electrical", "ELECTRICAL.GENERATOR")
    if has_sub(text, "automatic transfer switch") or has_word(text, "ats"):
        return ("TRANSFER_SWITCH", "Electrical", "ELECTRICAL.TRANSFER_SWITCH")
    if has_sub(text, "uninterruptible", "battery") or has_word(text, "ups"):
        return ("UPS", "Electrical", "ELECTRICAL.UPS")
    if has_sub(text, "motor control center") or has_word(text, "mcc"):
        return ("MOTOR_CONTROL_CENTER", "Electrical", "ELECTRICAL.MCC")
    if has_sub(text, "variable frequency drive") or has_word(text, "vfd"):
        return ("VFD", "Electrical", "ELECTRICAL.VFD")
    if has_sub(text, "handhole", "pull box", "manhole"):
        return ("ELECTRICAL_HANDHOLE", "Electrical", "ELECTRICAL.HANDHOLE")
    if has_sub(text, "meter"):
        return ("ELECTRICAL_METER", "Electrical", "ELECTRICAL.METER")
    if has_sub(text, "panel", "board"):
        return ("ELECTRICAL_PANEL", "Electrical", "ELECTRICAL.PANEL")
    return ("ELECTRICAL_EQUIPMENT", "Electrical", "ELECTRICAL.EQUIPMENT")


def classify_fire_protection(text):
    if has_sub(text, "fire hydrant", "hydrant"):
        return ("FIRE_HYDRANT", "Fire Protection", "FIRE_PROTECTION.HYDRANT")
    if has_sub(text, "standpipe", "fire department connection") or has_word(text, "fdc"):
        return ("STANDPIPE", "Fire Protection", "FIRE_PROTECTION.STANDPIPE")
    if has_sub(text, "fire extinguisher"):
        return ("FIRE_EXTINGUISHER", "Fire Protection", "FIRE_PROTECTION.EXTINGUISHER")
    if has_sub(text, "sprinkler"):
        return ("SPRINKLER", "Fire Protection", "FIRE_PROTECTION.SPRINKLER")
    if has_sub(text, "fire alarm", "pull station", "horn", "strobe"):
        return ("FIRE_ALARM_DEVICE", "Life Safety", "LIFE_SAFETY.FIRE_ALARM")
    if has_sub(text, "smoke detector", "heat detector", "detector"):
        return ("DETECTOR", "Life Safety", "LIFE_SAFETY.DETECTOR")
    return ("FIRE_PROTECTION_DEVICE", "Fire Protection", "FIRE_PROTECTION.DEVICE")


def classify_generic_or_specialty(text):
    """Generic Models, Specialty Equipment, Mechanical Control Devices, etc.
    These categories hold a grab bag, so keyword routing is the only option."""
    if has_sub(text, "sump pit", "sump basin"):
        return ("SUMP_PIT", "Plumbing", "PLUMBING.SUMP_PIT")
    if has_sub(text, "fire fighting water fill tank", "fire suppression water", "fire water tank"):
        return ("TANK", "Fire Protection", "PLUMBING.TANK")
    if has_sub(text, "expansion tank"):
        return ("TANK", "HVAC", "PLUMBING.TANK.EXPANSION")
    if has_sub(text, "tank"):
        return ("TANK", "Plumbing", "PLUMBING.TANK")
    if has_sub(text, "thermostat", "sensor"):
        return ("CONTROL_DEVICE", "HVAC", "HVAC.CONTROL_DEVICE")
    if has_sub(text, "actuator"):
        return ("CONTROL_DEVICE", "HVAC", "HVAC.CONTROL_DEVICE.ACTUATOR")
    return None  # unrecognised -> tallied as unhandled in the audit


# Category -> handler. Keys are lowercase category_name values.
CATEGORY_HANDLERS = {
    "electrical fixtures": classify_electrical_fixture,
    "lighting fixtures": classify_lighting,
    "lighting devices": classify_lighting,
    "air terminals": classify_air_terminal,
    "mechanical equipment": classify_mechanical_equipment,
    "duct fittings": classify_duct_fitting,
    "pipe fittings": classify_pipe_fitting,
    "duct accessories": classify_duct_accessory,
    "pipe accessories": classify_pipe_accessory,
    "plumbing fixtures": classify_plumbing_fixture,
    "plumbing equipment": classify_plumbing_fixture,
    "electrical equipment": classify_electrical_equipment,
    "sprinklers": classify_fire_protection,
    "fire alarm devices": classify_fire_protection,
    "fire protection": classify_fire_protection,
    "security devices": classify_fire_protection,
    "communication devices": classify_electrical_fixture,
    "data devices": classify_electrical_fixture,
    "telephone devices": classify_electrical_fixture,
    "nurse call devices": classify_electrical_fixture,
    "generic models": classify_generic_or_specialty,
    "specialty equipment": classify_generic_or_specialty,
    "mechanical control devices": classify_generic_or_specialty,
    "food service equipment": classify_generic_or_specialty,
}

# Audit counters
audit = {}

def audit_bump(category, outcome, asset_type=None, family=None):
    c = audit.setdefault(category or "(no category)", {
        "seen": 0,
        "classified": 0,
        "excluded_category": 0,
        "context_object": 0,
        "unhandled_category": 0,
        "unrecognised_in_category": 0,
        "no_unique_id": 0,
        "duplicate_uid": 0,
        "asset_types": {},
        "unresolved_families": {},
        "min_x": None, "max_x": None,
        "min_y": None, "max_y": None,
    })
    c[outcome] = c.get(outcome, 0) + 1
    if asset_type:
        c["asset_types"][asset_type] = c["asset_types"].get(asset_type, 0) + 1
    if family:
        c["unresolved_families"][family] = c["unresolved_families"].get(family, 0) + 1
    return c

def audit_extent(counter, x, y):
    if x is not None:
        counter["min_x"] = x if counter["min_x"] is None else min(counter["min_x"], x)
        counter["max_x"] = x if counter["max_x"] is None else max(counter["max_x"], x)
    if y is not None:
        counter["min_y"] = y if counter["min_y"] is None else min(counter["min_y"], y)
        counter["max_y"] = y if counter["max_y"] is None else max(counter["max_y"], y)


def classify_asset(rec):
    """Returns (asset_type, system, maximo_classification) or None.

    Category-first dispatch. The previous implementation ran ~40 sequential
    loose substring tests, which caused three distinct silent failures:
    Air Terminals and M_WSHP matched nothing, and 'pump' / 'light' / 'panel'
    tests stole records from the wrong categories."""
    cat = (rec.get("category_name") or "").strip()
    cat_l = cat.lower()
    fam = rec.get("family_name") or ""
    typ = rec.get("type_name") or ""
    text = f"{cat} {fam} {typ}".lower()

    counter = audit_bump(cat, "seen")
    audit_extent(counter, record_location_x(rec), record_location_y(rec))

    if cat_l in EXCLUDED_CATEGORIES:
        audit_bump(cat, "excluded_category")
        return None

    if has_sub(text, *CONTEXT_PHRASES):
        audit_bump(cat, "context_object")
        return None

    handler = CATEGORY_HANDLERS.get(cat_l)
    if handler is None:
        audit_bump(cat, "unhandled_category", family=fam)
        return None

    result = handler(text)
    if result is None:
        audit_bump(cat, "unrecognised_in_category", family=fam)
        return None

    audit_bump(cat, "classified", asset_type=result[0])
    return result

# ---------------------------------------------------------------------
# Room assignment (single model -> no cross-model geometry anywhere)
# ---------------------------------------------------------------------

def nearest_room_on_level(level_name, x, y):
    """Returns (room_row, distance) or (None, inf)."""
    candidates = rooms_by_level.get(level_name, [])
    if not candidates or x is None or y is None:
        return None, float("inf")
    best, best_d = None, float("inf")
    for r in candidates:
        d = distance_xy((x, y), (r.get("x"), r.get("y")))
        if d < best_d:
            best, best_d = r, d
    return best, best_d


def assign_location(rec):
    """Three-tier assignment, highest confidence first.

    Tier 1  direct room_name on the record  -> HIGH
    Tier 2  nearest spatial record on same level, within distance cap -> HIGH/MEDIUM
    Tier 3  level only (from level_name or z elevation) -> LOW
    Tier 4  building -> LOW

    No step compares coordinates from one model against another, so a
    coordinate origin mismatch cannot produce a wrong room here."""

    x = record_location_x(rec)
    y = record_location_y(rec)
    z = record_location_z(rec)

    raw_level = effective_level_name(rec)
    level_name = normalize_level_name(raw_level)
    level_source = "PARAMETER" if level_name else None

    # Records with no level parameter at all (e.g. M_Duplex Receptacle in
    # this extract) get a level derived from z against levels.json elevations.
    if not level_name:
        level_name = level_from_elevation(z)
        level_source = "Z_ELEVATION" if level_name else None

    # Tier 1: the record names its own room.
    rname = record_room_name(rec)
    if rname not in [None, ""]:
        k = str(rname).strip().upper()

        # Prefer a match on the same level, then fall back to name-only.
        cands = []
        if level_name:
            cands = rooms_by_level_name.get((level_name, k), [])
        if not cands:
            cands = rooms_by_name.get(k, [])

        if cands:
            room = cands[0]
            if len(cands) > 1 and x is not None and y is not None:
                room = min(cands, key=lambda r: distance_xy((x, y), (r.get("x"), r.get("y"))))
            return {
                "location_id": room["location_id"],
                "room_number": room.get("room_number"),
                "room_name": room.get("description"),
                "level_name": level_name or room.get("level_name"),
                "level_source": level_source,
                "method": "ROOM_NAME_MATCH",
                "confidence": "HIGH",
                "match_distance": None,
            }

    # Tier 2: nearest spatial record on the same level, with a distance cap
    # so a far-away room is never asserted as a match.
    if level_name:
        room, dist = nearest_room_on_level(level_name, x, y)
        if room is not None and dist <= MAX_SPACE_MATCH_DIST:
            conf = "HIGH" if dist <= MAX_SPACE_MATCH_DIST / 3.0 else "MEDIUM"
            return {
                "location_id": room["location_id"],
                "room_number": room.get("room_number"),
                "room_name": room.get("description"),
                "level_name": level_name,
                "level_source": level_source,
                "method": "NEAREST_ROOM_ON_LEVEL",
                "confidence": conf,
                "match_distance": round(dist, 3),
            }

        # Tier 3: level only. Honest "Level 2, room unknown" rather than a guess.
        lid = loc_level_id(level_name)
        if lid in location_ids:
            return {
                "location_id": lid,
                "room_number": None,
                "room_name": None,
                "level_name": level_name,
                "level_source": level_source,
                "method": "LEVEL_ONLY" if room is None else "LEVEL_ONLY_ROOM_TOO_FAR",
                "confidence": "LOW",
                "match_distance": round(dist, 3) if room is not None else None,
            }

    # Tier 4: building.
    return {
        "location_id": BUILDING_ID,
        "room_number": None,
        "room_name": None,
        "level_name": level_name,
        "level_source": level_source,
        "method": "BUILDING_FALLBACK",
        "confidence": "LOW",
        "match_distance": None,
    }

# ---------------------------------------------------------------------
# Dimensions
#
# The previous version read sym.get("width") / ("depth") / ("diameter") /
# ("thickness"), but the type records expose dimensions inside their
# parameters array, not as top-level keys. Those four fields were therefore
# null on every asset ever generated. Read parameters first, then fall back
# to the instance bounding box, which is always present in this extract.
# ---------------------------------------------------------------------

WIDTH_PARAMS = ["Width", "Duct Width", "Supply Air Width", "Return Air Width",
                "Nominal Width", "Overall Width", "Pump Width"]
DEPTH_PARAMS = ["Depth", "Length", "Duct Length", "Nominal Depth",
                "Overall Depth", "Pump Length"]
HEIGHT_PARAMS = ["Height", "Duct Height", "Supply Air Height",
                 "Return Air Height", "Overall Height", "Pump Height"]
DIAMETER_PARAMS = ["Diameter", "Nominal Diameter", "Duct Diameter",
                   "Water Supply Diameter", "Water Return Diameter",
                   "Condensate Diameter", "Hot Water Supply Diameter"]
THICKNESS_PARAMS = ["Thickness", "Wall Thickness", "Insulation Thickness"]


def first_param(sym, names):
    for n in names:
        v = param_value(sym, n)
        if isinstance(v, (int, float)) and v != 0:
            return v
    return None


def bbox_dims(rec):
    """(width_x, depth_y, height_z) from the instance bounding box."""
    mnx = get_nested(rec, "bounding_box", "min_x")
    mny = get_nested(rec, "bounding_box", "min_y")
    mnz = get_nested(rec, "bounding_box", "min_z")
    mxx = get_nested(rec, "bounding_box", "max_x")
    mxy = get_nested(rec, "bounding_box", "max_y")
    mxz = get_nested(rec, "bounding_box", "max_z")
    if None in [mnx, mny, mnz, mxx, mxy, mxz]:
        return None, None, None
    return (
        round(abs(mxx - mnx), 4),
        round(abs(mxy - mny), 4),
        round(abs(mxz - mnz), 4),
    )


def resolve_dimensions(rec, sym):
    """Returns a dict of symbol_* fields plus a provenance flag."""
    w = first_param(sym, WIDTH_PARAMS)
    d = first_param(sym, DEPTH_PARAMS)
    h = first_param(sym, HEIGHT_PARAMS)
    dia = first_param(sym, DIAMETER_PARAMS)
    th = first_param(sym, THICKNESS_PARAMS)

    source = "TYPE_PARAMETERS" if any(v is not None for v in [w, d, h, dia, th]) else None

    bw, bd, bh = bbox_dims(rec)
    if w is None and d is None and h is None:
        w, d, h = bw, bd, bh
        source = "BOUNDING_BOX" if w is not None else source

    return {
        "symbol_width": w,
        "symbol_depth": d,
        "symbol_height": h,
        "symbol_diameter": dia,
        "symbol_thickness": th,
        "bbox_width": bw,
        "bbox_depth": bd,
        "bbox_height": bh,
        "dimension_source": source or "NONE",
    }

# ---------------------------------------------------------------------
# Attribute extraction for selected types
# ---------------------------------------------------------------------

def parse_capacity_kw(family_name, type_name):
    """WSHP types are named '7 kW', '14 kW' and the family carries a range.
    The type value is the actual unit capacity, so prefer it."""
    for candidate in [type_name, family_name]:
        if not candidate:
            continue
        m = re.search(r"(\d+(?:\.\d+)?)\s*kw\b", str(candidate).lower())
        if m:
            # Skip family-name ranges like '7-18 kW' when a type value exists.
            if re.search(r"\d+\s*-\s*\d+\s*kw", str(candidate).lower()) and candidate == family_name:
                continue
            return float(m.group(1))
    return None


def extract_attributes(asset_type, rec, sym):
    """Type-specific descriptive fields. These are the attributes an agent
    is most likely to be asked to filter on."""
    fam = (rec.get("family_name") or "")
    typ = (rec.get("type_name") or "")
    fl = fam.lower()
    tl = typ.lower()
    attrs = {}

    if asset_type in ["WSHP", "GSHP", "HEAT_PUMP"]:
        cap = parse_capacity_kw(fam, typ)
        if cap is not None:
            attrs["capacity_kw"] = cap
        if "horizontal" in fl:
            attrs["configuration"] = "HORIZONTAL"
        elif "vertical" in fl:
            attrs["configuration"] = "VERTICAL"
        if "high efficiency" in fl:
            attrs["efficiency_class"] = "HIGH_EFFICIENCY"
        ret = "LEFT" if "left return" in fl else ("RIGHT" if "right return" in fl else None)
        dis = "RIGHT" if "right discharge" in fl else ("LEFT" if "left discharge" in fl else None)
        if ret:
            attrs["return_side"] = ret
        if dis:
            attrs["discharge_side"] = dis
        v = param_value(sym, "Voltage")
        if isinstance(v, (int, float)) and v:
            attrs["voltage"] = round(float(v), 1)
        p = param_value(sym, "Number of Poles")
        if isinstance(p, (int, float)) and p:
            attrs["number_of_poles"] = int(p)
        al = param_value(sym, "Apparent Load")
        if isinstance(al, (int, float)) and al:
            attrs["apparent_load_va"] = round(float(al), 1)

    if asset_type in ["SUPPLY_DIFFUSER", "RETURN_GRILLE", "EXHAUST_GRILLE",
                      "TRANSFER_GRILLE", "LINEAR_SLOT_DIFFUSER", "AIR_TERMINAL",
                      "AIR_INTAKE_LOUVER"]:
        if "sidewall" in fl:
            attrs["mounting"] = "SIDEWALL"
        elif "ceiling" in fl:
            attrs["mounting"] = "CEILING"
        elif "floor" in fl:
            attrs["mounting"] = "FLOOR"
        # Type names like '450 x 200' are the face size in mm.
        m = re.search(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)", tl)
        if m:
            attrs["face_width_mm"] = float(m.group(1))
            attrs["face_height_mm"] = float(m.group(2))
        elif re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", tl):
            attrs["face_diameter_mm"] = float(tl.strip())

    if asset_type.startswith("DUCT_FITTING"):
        if "rectangular" in fl:
            attrs["duct_shape"] = "RECTANGULAR"
        elif "round" in fl:
            attrs["duct_shape"] = "ROUND"
        elif "oval" in fl:
            attrs["duct_shape"] = "OVAL"

    if asset_type.startswith("PIPE_FITTING"):
        dia = first_param(sym, DIAMETER_PARAMS)
        if dia is not None:
            attrs["nominal_diameter"] = dia

    if asset_type in ["DUPLEX_RECEPTACLE", "QUADRUPLEX_RECEPTACLE", "RECEPTACLE",
                      "GFCI_RECEPTACLE", "FLOOR_BOX"]:
        z = record_location_z(rec)
        lvl = level_from_elevation(z)
        if lvl is not None and z is not None:
            for elev, name in level_elevations:
                if name == lvl:
                    attrs["mounting_height_ft"] = round(z - elev, 3)
                    break

    rot = get_nested(rec, "location", "rotation")
    if isinstance(rot, (int, float)):
        attrs["rotation_rad"] = rot
        attrs["rotation_deg"] = round(math.degrees(rot) % 360.0, 2)

    return attrs

# ---------------------------------------------------------------------
# Synthetic operational defaults
#
# (criticality, service_life_years, replacement_cost, pm_frequency, manufacturer)
# Fittings are deliberately low-cost, low-criticality and ANNUAL; they are
# flagged maintainable=False and are excluded from PM/WO generation, so their
# PM frequency is descriptive only.
# ---------------------------------------------------------------------

TYPE_DEFAULTS = {
    # --- HVAC equipment ---
    "WSHP":                  (5, 15, 14000, "QUARTERLY",  "Synthetic HVAC Co."),
    "GSHP":                  (5, 20, 18000, "QUARTERLY",  "Synthetic HVAC Co."),
    "HEAT_PUMP":             (5, 15, 13000, "QUARTERLY",  "Synthetic HVAC Co."),
    "AHU":                   (5, 25, 85000, "MONTHLY",    "Synthetic Mechanical Co."),
    "RTU":                   (5, 20, 62000, "MONTHLY",    "Synthetic Mechanical Co."),
    "ENERGY_RECOVERY_UNIT":  (4, 20, 38000, "QUARTERLY",  "Synthetic Mechanical Co."),
    "VRF_OUTDOOR_UNIT":      (5, 18, 42000, "MONTHLY",    "Synthetic HVAC Co."),
    "HVAC_INDOOR_UNIT":      (4, 15, 12000, "QUARTERLY",  "Synthetic HVAC Co."),
    "HEAT_EXCHANGER_UNIT":   (4, 20, 28000, "QUARTERLY",  "Synthetic Mechanical Co."),
    "FCU":                   (4, 15, 9000,  "QUARTERLY",  "Synthetic HVAC Co."),
    "UNIT_HEATER":           (3, 15, 4200,  "ANNUAL",     "Synthetic HVAC Co."),
    "CHILLER":               (5, 25, 240000,"MONTHLY",    "Synthetic Mechanical Co."),
    "COOLING_TOWER":         (5, 20, 95000, "MONTHLY",    "Synthetic Mechanical Co."),
    "BOILER":                (5, 25, 110000,"MONTHLY",    "Synthetic Mechanical Co."),
    "HUMIDIFIER":            (3, 12, 7500,  "QUARTERLY",  "Synthetic HVAC Co."),
    "DEHUMIDIFIER":          (3, 12, 8200,  "QUARTERLY",  "Synthetic HVAC Co."),
    "AIR_COMPRESSOR":        (4, 20, 22000, "QUARTERLY",  "Synthetic Mechanical Co."),
    "MECHANICAL_EQUIPMENT":  (3, 20, 15000, "ANNUAL",     "Synthetic Mechanical Co."),
    "FAN":                   (3, 15, 8500,  "QUARTERLY",  "Synthetic Mechanical Co."),
    "EXHAUST_FAN":           (4, 15, 9500,  "QUARTERLY",  "Synthetic Mechanical Co."),

    # --- Air terminals ---
    "VAV":                   (4, 20, 3800,  "SEMIANNUAL", "Synthetic HVAC Co."),
    "VAV_FAN_POWERED":       (4, 18, 5200,  "SEMIANNUAL", "Synthetic HVAC Co."),
    "CAV_TERMINAL":          (3, 20, 2600,  "ANNUAL",     "Synthetic HVAC Co."),
    "SUPPLY_DIFFUSER":       (2, 25, 320,   "ANNUAL",     "Synthetic Air Devices Co."),
    "RETURN_GRILLE":         (2, 25, 280,   "ANNUAL",     "Synthetic Air Devices Co."),
    "EXHAUST_GRILLE":        (2, 25, 280,   "ANNUAL",     "Synthetic Air Devices Co."),
    "TRANSFER_GRILLE":       (1, 25, 220,   "ANNUAL",     "Synthetic Air Devices Co."),
    "LINEAR_SLOT_DIFFUSER":  (2, 25, 540,   "ANNUAL",     "Synthetic Air Devices Co."),
    "AIR_INTAKE_LOUVER":     (3, 30, 1800,  "ANNUAL",     "Synthetic Air Devices Co."),
    "AIR_TERMINAL":          (2, 25, 300,   "ANNUAL",     "Synthetic Air Devices Co."),

    # --- Electrical fixtures / devices ---
    "DUPLEX_RECEPTACLE":     (2, 25, 95,    "ANNUAL",     "Synthetic Electrical Co."),
    "QUADRUPLEX_RECEPTACLE": (2, 25, 140,   "ANNUAL",     "Synthetic Electrical Co."),
    "GFCI_RECEPTACLE":       (3, 20, 165,   "ANNUAL",     "Synthetic Electrical Co."),
    "RECEPTACLE":            (2, 25, 95,    "ANNUAL",     "Synthetic Electrical Co."),
    "FLOOR_BOX":             (2, 25, 420,   "ANNUAL",     "Synthetic Electrical Co."),
    "JUNCTION_BOX":          (2, 30, 85,    "ANNUAL",     "Synthetic Electrical Co."),
    "LIGHT_SWITCH":          (2, 25, 75,    "ANNUAL",     "Synthetic Electrical Co."),
    "DIMMER_SWITCH":         (2, 20, 145,   "ANNUAL",     "Synthetic Electrical Co."),
    "OCCUPANCY_SENSOR":      (3, 15, 210,   "ANNUAL",     "Synthetic Controls Co."),
    "DATA_OUTLET":           (2, 20, 120,   "ANNUAL",     "Synthetic Electrical Co."),
    "DISCONNECT_SWITCH":     (4, 25, 850,   "ANNUAL",     "Synthetic Electrical Co."),
    "MOTOR_STARTER":         (4, 20, 1650,  "ANNUAL",     "Synthetic Electrical Co."),
    "THERMOSTAT":            (3, 12, 260,   "ANNUAL",     "Synthetic Controls Co."),
    "ELECTRICAL_FIXTURE":    (2, 25, 150,   "ANNUAL",     "Synthetic Electrical Co."),

    # --- Electrical equipment ---
    "ELECTRICAL_PANEL":      (5, 30, 22000, "ANNUAL",     "Synthetic Electrical Co."),
    "TERMINAL_BOARD":        (4, 25, 12000, "ANNUAL",     "Synthetic Electrical Co."),
    "CONTROL_PANEL":         (5, 20, 18000, "SEMIANNUAL", "Synthetic Controls Co."),
    "TRANSFORMER":           (5, 30, 48000, "ANNUAL",     "Synthetic Electrical Co."),
    "GENERATOR":             (5, 25, 185000,"MONTHLY",    "Synthetic Power Systems Co."),
    "TRANSFER_SWITCH":       (5, 25, 32000, "SEMIANNUAL", "Synthetic Power Systems Co."),
    "UPS":                   (5, 12, 56000, "QUARTERLY",  "Synthetic Power Systems Co."),
    "MOTOR_CONTROL_CENTER":  (5, 30, 64000, "ANNUAL",     "Synthetic Electrical Co."),
    "VFD":                   (4, 15, 7800,  "SEMIANNUAL", "Synthetic Electrical Co."),
    "ELECTRICAL_HANDHOLE":   (3, 30, 2500,  "ANNUAL",     "Synthetic Electrical Co."),
    "ELECTRICAL_METER":      (4, 20, 3200,  "ANNUAL",     "Synthetic Electrical Co."),
    "ELECTRICAL_EQUIPMENT":  (3, 25, 10000, "ANNUAL",     "Synthetic Electrical Co."),

    # --- Lighting ---
    "LIGHTING_FIXTURE":      (2, 15, 650,   "ANNUAL",     "Synthetic Lighting Co."),
    "EMERGENCY_LIGHT":       (4, 12, 950,   "SEMIANNUAL", "Synthetic Life Safety Lighting Co."),
    "EXIT_SIGN_LIGHT":       (4, 12, 850,   "SEMIANNUAL", "Synthetic Life Safety Lighting Co."),
    "STAIRWAY_LIGHT":        (4, 12, 900,   "SEMIANNUAL", "Synthetic Life Safety Lighting Co."),

    # --- Fire protection / life safety ---
    "FIRE_PUMP":             (5, 25, 78000, "MONTHLY",    "Synthetic Fire Protection Co."),
    "FIRE_HYDRANT":          (5, 30, 9000,  "ANNUAL",     "Synthetic Fire Protection Co."),
    "STANDPIPE":             (5, 30, 6500,  "ANNUAL",     "Synthetic Fire Protection Co."),
    "FIRE_EXTINGUISHER":     (4, 12, 180,   "ANNUAL",     "Synthetic Fire Protection Co."),
    "SPRINKLER":             (4, 25, 350,   "ANNUAL",     "Synthetic Fire Protection Co."),
    "FIRE_ALARM_DEVICE":     (5, 15, 420,   "SEMIANNUAL", "Synthetic Life Safety Co."),
    "DETECTOR":              (5, 10, 280,   "SEMIANNUAL", "Synthetic Life Safety Co."),
    "FIRE_SMOKE_DAMPER":     (5, 20, 3000,  "ANNUAL",     "Synthetic Fire Protection Co."),
    "FIRE_DAMPER":           (5, 20, 2500,  "ANNUAL",     "Synthetic Fire Protection Co."),
    "SMOKE_DAMPER":          (5, 20, 2800,  "ANNUAL",     "Synthetic Life Safety Co."),
    "EMERGENCY_FIXTURE":     (5, 15, 1850,  "SEMIANNUAL", "Synthetic Life Safety Co."),
    "FIRE_PROTECTION_DEVICE":(4, 20, 900,   "ANNUAL",     "Synthetic Fire Protection Co."),

    # --- Duct accessories ---
    "BACKDRAFT_DAMPER":      (3, 20, 1500,  "ANNUAL",     "Synthetic HVAC Co."),
    "DAMPER":                (3, 20, 1200,  "ANNUAL",     "Synthetic HVAC Co."),
    "SOUND_ATTENUATOR":      (2, 25, 2200,  "ANNUAL",     "Synthetic HVAC Co."),
    "DUCT_ACCESS_DOOR":      (2, 25, 240,   "ANNUAL",     "Synthetic HVAC Co."),
    "DUCT_ACCESSORY":        (3, 20, 900,   "ANNUAL",     "Synthetic HVAC Co."),

    # --- Plumbing ---
    "WATER_HEATER":          (4, 15, 8600,  "QUARTERLY",  "Synthetic Plumbing Co."),
    "PUMP":                  (5, 20, 25000, "MONTHLY",    "Synthetic Pump Co."),
    "SUMP_PUMP":             (4, 15, 6400,  "QUARTERLY",  "Synthetic Pump Co."),
    "CIRCULATOR_PUMP":       (4, 15, 5200,  "QUARTERLY",  "Synthetic Pump Co."),
    "VALVE":                 (3, 25, 1500,  "ANNUAL",     "Synthetic Valve Co."),
    "BACKFLOW_PREVENTER":    (5, 20, 3400,  "ANNUAL",     "Synthetic Plumbing Co."),
    "STRAINER":              (3, 25, 820,   "ANNUAL",     "Synthetic Plumbing Co."),
    "PIPE_ACCESSORY":        (3, 25, 1200,  "ANNUAL",     "Synthetic Plumbing Co."),
    "TANK":                  (5, 30, 65000, "ANNUAL",     "Synthetic Plumbing Co."),
    "SUMP_PIT":              (4, 35, 18000, "QUARTERLY",  "Synthetic Civil Systems Co."),
    "WATER_CLOSET":          (2, 25, 820,   "ANNUAL",     "Synthetic Plumbing Co."),
    "URINAL":                (2, 25, 740,   "ANNUAL",     "Synthetic Plumbing Co."),
    "LAVATORY":              (2, 25, 680,   "ANNUAL",     "Synthetic Plumbing Co."),
    "SHOWER":                (2, 25, 1100,  "ANNUAL",     "Synthetic Plumbing Co."),
    "SERVICE_SINK":          (2, 25, 760,   "ANNUAL",     "Synthetic Plumbing Co."),
    "DRINKING_FOUNTAIN":     (3, 20, 1450,  "ANNUAL",     "Synthetic Plumbing Co."),
    "FLOOR_DRAIN":           (2, 30, 390,   "ANNUAL",     "Synthetic Plumbing Co."),
    "ROOF_DRAIN":            (3, 30, 620,   "ANNUAL",     "Synthetic Plumbing Co."),
    "CLEANOUT":              (2, 25, 750,   "ANNUAL",     "Synthetic Plumbing Co."),
    "VENT_FITTING":          (2, 25, 900,   "ANNUAL",     "Synthetic Plumbing Co."),
    "TRAP_PRIMER":           (2, 20, 480,   "ANNUAL",     "Synthetic Plumbing Co."),
    "PLUMBING_FIXTURE":      (2, 25, 700,   "ANNUAL",     "Synthetic Plumbing Co."),

    # --- Controls ---
    "CONTROL_DEVICE":        (3, 12, 420,   "ANNUAL",     "Synthetic Controls Co."),

    # --- Fittings (non-maintainable; present for model location Q&A) ---
    "DUCT_FITTING_ELBOW":      (1, 30, 85,  "ANNUAL", "Synthetic Sheet Metal Co."),
    "DUCT_FITTING_TEE":        (1, 30, 110, "ANNUAL", "Synthetic Sheet Metal Co."),
    "DUCT_FITTING_TRANSITION": (1, 30, 95,  "ANNUAL", "Synthetic Sheet Metal Co."),
    "DUCT_FITTING_TAKEOFF":    (1, 30, 70,  "ANNUAL", "Synthetic Sheet Metal Co."),
    "DUCT_FITTING_CAP":        (1, 30, 45,  "ANNUAL", "Synthetic Sheet Metal Co."),
    "DUCT_FITTING_UNION":      (1, 30, 55,  "ANNUAL", "Synthetic Sheet Metal Co."),
    "DUCT_FITTING_CROSS":      (1, 30, 130, "ANNUAL", "Synthetic Sheet Metal Co."),
    "DUCT_FITTING_OFFSET":     (1, 30, 105, "ANNUAL", "Synthetic Sheet Metal Co."),
    "DUCT_FITTING":            (1, 30, 90,  "ANNUAL", "Synthetic Sheet Metal Co."),

    "PIPE_FITTING_ELBOW":      (1, 35, 40,  "ANNUAL", "Synthetic Pipe Fittings Co."),
    "PIPE_FITTING_TEE":        (1, 35, 55,  "ANNUAL", "Synthetic Pipe Fittings Co."),
    "PIPE_FITTING_COUPLING":    (1, 35, 35,  "ANNUAL", "Synthetic Pipe Fittings Co."),
    "PIPE_FITTING_CAP":        (1, 35, 25,  "ANNUAL", "Synthetic Pipe Fittings Co."),
    "PIPE_FITTING_UNION":      (1, 35, 45,  "ANNUAL", "Synthetic Pipe Fittings Co."),
    "PIPE_FITTING_FLANGE":     (1, 35, 95,  "ANNUAL", "Synthetic Pipe Fittings Co."),
    "PIPE_FITTING_REDUCER":    (1, 35, 50,  "ANNUAL", "Synthetic Pipe Fittings Co."),
    "PIPE_FITTING_CROSS":      (1, 35, 75,  "ANNUAL", "Synthetic Pipe Fittings Co."),
    "PIPE_FITTING":            (1, 35, 40,  "ANNUAL", "Synthetic Pipe Fittings Co."),
}


def is_maintainable(asset_type):
    return asset_type not in NON_MAINTAINABLE_TYPES


def synthetic_defaults(asset_type, uid):
    base = stable_int(uid, 100)
    criticality, life, cost, pm_freq, mfr = TYPE_DEFAULTS.get(
        asset_type, (3, 20, 5000, "ANNUAL", "Synthetic Facilities Co.")
    )

    # Fittings are long-lived passive components; keep their synthetic
    # condition high so they do not distort condition-based reporting.
    if not is_maintainable(asset_type):
        condition = 80 + (base % 16)
        failures = 0
    else:
        condition = 55 + (base % 41)
        failures = base % 4 if condition < 75 else base % 2

    install_year = 2014 + (base % 8)
    install_month = 1 + (base % 12)
    install_day = 1 + (base % 27)

    return {
        "criticality": criticality,
        "condition_score": condition,
        "install_date": f"{install_year:04d}-{install_month:02d}-{install_day:02d}",
        "expected_service_life_years": life,
        "replacement_cost": cost,
        "manufacturer": mfr,
        "pm_frequency": pm_freq,
        "last_pm_date": "2026-09-15",
        "next_pm_date": "2026-10-15",
        "failure_count_12mo": failures,
        "open_workorder_count": 0,   # recomputed after work orders exist
        "risk_score": 0,             # recomputed after work orders exist
    }


def asset_id_for(asset_type, level_name, seq):
    lvl = sanitize_id(level_name) if level_name else "BLDG"
    return f"{BUILDING_ID}-{sanitize_id(asset_type)}-{lvl}-{seq:0{SEQ_WIDTH}d}"

# ---------------------------------------------------------------------
# Generate assets.json
# ---------------------------------------------------------------------

asset_source_rows = []
seen_uids = {}

def add_asset_from_record(rec, source_file):
    cls = classify_asset(rec)
    if not cls:
        return

    asset_type, system, classification = cls
    cat = (rec.get("category_name") or "").strip()

    source_uid = rec.get("unique_id")
    if not source_uid:
        audit_bump(cat, "no_unique_id")
        return

    c = canonical_json(rec)
    if source_uid in seen_uids:
        if seen_uids[source_uid] != c:
            print(f"WARNING: asset candidate duplicate unique_id, differing content: {source_uid}; keeping first")
        audit_bump(cat, "duplicate_uid")
        return
    seen_uids[source_uid] = c

    sym = symbols_by_key.get(str(rec.get("type_id")), {})
    dims = resolve_dimensions(rec, sym)
    attrs = extract_attributes(asset_type, rec, sym)
    loc = assign_location(rec)
    defaults = synthetic_defaults(asset_type, source_uid)
    maintainable = is_maintainable(asset_type)

    fam = rec.get("family_name") or ""
    typ = rec.get("type_name") or ""
    desc_bits = [b for b in [fam, typ] if b]
    readable = " - ".join(desc_bits) if desc_bits else asset_type

    asset_source_rows.append({
        "_sort_asset_type": asset_type,
        "_sort_level": loc.get("level_name") or "",
        "_sort_uid": source_uid,

        "building_id": BUILDING_ID,
        "model_folder": MECH,
        "model_key": MODEL_KEY,
        "source_file": source_file,

        "source_unique_id": source_uid,
        "source_type_id": rec.get("type_id"),
        "source_family_id": rec.get("family_id"),
        "source_category_name": rec.get("category_name"),
        "source_family_name": fam,
        "source_type_name": typ,
        "source_phase": rec.get("phase"),

        "asset_type": asset_type,
        "system": system,
        "maximo_classification": classification,
        "maintainable": maintainable,
        "description": readable,

        "location_id": loc["location_id"],
        "level_name": loc.get("level_name"),
        "level_source": loc.get("level_source"),
        "room_number": loc.get("room_number"),
        "room_name": loc.get("room_name"),
        "room_assignment_method": loc["method"],
        "location_confidence": loc["confidence"],
        "room_match_distance": loc.get("match_distance"),

        "x": record_location_x(rec),
        "y": record_location_y(rec),
        "z": record_location_z(rec),

        "bbox_min_x": get_nested(rec, "bounding_box", "min_x"),
        "bbox_min_y": get_nested(rec, "bounding_box", "min_y"),
        "bbox_min_z": get_nested(rec, "bounding_box", "min_z"),
        "bbox_max_x": get_nested(rec, "bounding_box", "max_x"),
        "bbox_max_y": get_nested(rec, "bounding_box", "max_y"),
        "bbox_max_z": get_nested(rec, "bounding_box", "max_z"),

        **dims,
        "attributes": attrs,

        "status": "OPERATING",
        **defaults,
    })


print("Classifying family_instances...")
for rec in family_instances:
    add_asset_from_record(rec, "family_instances.json")

# Generic models, if the MECH extract has them. The previous version only
# ever read generic_models from STRUC, which meant any MECH generic models
# were never opened at all.
mech_generic = dedupe_by_unique_id(load_records(MECH, "generic_models"), "MECH generic_models")
if mech_generic:
    print(f"  classifying {len(mech_generic)} MECH generic_models records...")
    for rec in mech_generic:
        add_asset_from_record(rec, "generic_models.json")

asset_source_rows.sort(key=lambda r: (r["_sort_asset_type"], r["_sort_level"], r["_sort_uid"]))

seq_by_type_level = {}
assets = []
for r in asset_source_rows:
    lvl = r.get("level_name") or "BLDG"
    key = (r["asset_type"], lvl)
    seq_by_type_level[key] = seq_by_type_level.get(key, 0) + 1
    seq = seq_by_type_level[key]

    r["asset_id"] = asset_id_for(r["asset_type"], r.get("level_name"), seq)
    r["model_number"] = f"SYN-{sanitize_id(r['asset_type'])}-{1000 + stable_int(r['source_unique_id'], 9000)}"
    r["serial_number"] = f"SYN-{BUILDING_ID}-{sanitize_id(r['asset_type'])}-{seq:05d}"

    for k in ["_sort_asset_type", "_sort_level", "_sort_uid"]:
        r.pop(k, None)

    assets.append(r)

print(f"  assets generated: {len(assets)}")

maintainable_assets = [a for a in assets if a["maintainable"]]
print(f"  maintainable: {len(maintainable_assets)}  non-maintainable: {len(assets) - len(maintainable_assets)}")

# ---------------------------------------------------------------------
# preventive_maintenance.json
# ---------------------------------------------------------------------
JOB_PLANS = {
    # --- HVAC equipment ---
    "WSHP": "Water source heat pump quarterly inspection (filter, coil, condensate, refrigerant circuit)",
    "GSHP": "Ground source heat pump quarterly inspection",
    "HEAT_PUMP": "Heat pump quarterly inspection",
    "AHU": "AHU monthly inspection",
    "RTU": "Rooftop unit monthly inspection",
    "ENERGY_RECOVERY_UNIT": "Energy recovery unit quarterly inspection",
    "VRF_OUTDOOR_UNIT": "VRF outdoor unit inspection",
    "HVAC_INDOOR_UNIT": "Indoor HVAC unit inspection",
    "HEAT_EXCHANGER_UNIT": "Heat exchanger unit inspection",
    "FCU": "Fan coil unit quarterly inspection",
    "UNIT_HEATER": "Unit heater annual inspection",
    "CHILLER": "Chiller monthly inspection",
    "COOLING_TOWER": "Cooling tower monthly inspection and water treatment check",
    "BOILER": "Boiler monthly inspection",
    "HUMIDIFIER": "Humidifier quarterly inspection",
    "DEHUMIDIFIER": "Dehumidifier quarterly inspection",
    "AIR_COMPRESSOR": "Air compressor quarterly inspection",
    "MECHANICAL_EQUIPMENT": "Mechanical equipment annual inspection",
    "FAN": "Fan inspection",
    "EXHAUST_FAN": "Exhaust fan inspection",

    # --- Air terminals ---
    "VAV": "VAV box semiannual inspection (damper, actuator, controller)",
    "VAV_FAN_POWERED": "Fan-powered VAV semiannual inspection",
    "CAV_TERMINAL": "CAV terminal annual inspection",
    "SUPPLY_DIFFUSER": "Supply diffuser annual clean and airflow check",
    "RETURN_GRILLE": "Return grille annual clean and inspection",
    "EXHAUST_GRILLE": "Exhaust grille annual clean and inspection",
    "TRANSFER_GRILLE": "Transfer grille annual inspection",
    "LINEAR_SLOT_DIFFUSER": "Linear slot diffuser annual clean and airflow check",
    "AIR_INTAKE_LOUVER": "Air intake louver annual clean and screen inspection",
    "AIR_TERMINAL": "Air terminal annual clean and inspection",

    # --- Electrical fixtures / devices ---
    "DUPLEX_RECEPTACLE": "Receptacle annual inspection and device test",
    "QUADRUPLEX_RECEPTACLE": "Receptacle annual inspection and device test",
    "GFCI_RECEPTACLE": "GFCI receptacle annual trip test",
    "RECEPTACLE": "Receptacle annual inspection and device test",
    "FLOOR_BOX": "Floor box annual inspection and cover check",
    "JUNCTION_BOX": "Junction box annual inspection",
    "LIGHT_SWITCH": "Switch annual inspection and operation test",
    "DIMMER_SWITCH": "Dimmer annual inspection and operation test",
    "OCCUPANCY_SENSOR": "Occupancy sensor annual calibration check",
    "DATA_OUTLET": "Data outlet annual inspection",
    "DISCONNECT_SWITCH": "Disconnect switch annual inspection",
    "MOTOR_STARTER": "Motor starter annual inspection",
    "THERMOSTAT": "Thermostat annual calibration check",
    "ELECTRICAL_FIXTURE": "Electrical fixture annual inspection",

    # --- Electrical equipment ---
    "ELECTRICAL_PANEL": "Electrical panel annual inspection and thermographic scan",
    "TERMINAL_BOARD": "Terminal board annual inspection",
    "CONTROL_PANEL": "Control panel semiannual inspection",
    "TRANSFORMER": "Transformer annual inspection",
    "GENERATOR": "Generator monthly load test and inspection",
    "TRANSFER_SWITCH": "Transfer switch semiannual transfer test",
    "UPS": "UPS quarterly battery and load test",
    "MOTOR_CONTROL_CENTER": "Motor control center annual inspection",
    "VFD": "VFD semiannual inspection and cooling check",
    "ELECTRICAL_HANDHOLE": "Electrical handhole annual inspection",
    "ELECTRICAL_METER": "Electrical meter annual verification",
    "ELECTRICAL_EQUIPMENT": "Electrical equipment annual inspection",

    # --- Lighting ---
    "LIGHTING_FIXTURE": "Lighting fixture annual inspection",
    "EMERGENCY_LIGHT": "Emergency light semiannual functional test",
    "EXIT_SIGN_LIGHT": "Exit sign semiannual functional test",
    "STAIRWAY_LIGHT": "Stairway light semiannual inspection",

    # --- Fire protection / life safety ---
    "FIRE_PUMP": "Fire pump monthly churn test",
    "FIRE_HYDRANT": "Fire hydrant annual inspection and flow test",
    "STANDPIPE": "Standpipe annual inspection",
    "FIRE_EXTINGUISHER": "Fire extinguisher annual inspection",
    "SPRINKLER": "Sprinkler annual inspection",
    "FIRE_ALARM_DEVICE": "Fire alarm device semiannual functional test",
    "DETECTOR": "Detector semiannual sensitivity test",
    "FIRE_SMOKE_DAMPER": "Fire smoke damper annual drop test",
    "FIRE_DAMPER": "Fire damper annual drop test",
    "SMOKE_DAMPER": "Smoke damper annual operation test",
    "EMERGENCY_FIXTURE": "Emergency fixture semiannual flow test",
    "FIRE_PROTECTION_DEVICE": "Fire protection device annual inspection",

    # --- Duct accessories ---
    "BACKDRAFT_DAMPER": "Backdraft damper annual inspection",
    "DAMPER": "Damper annual inspection",
    "SOUND_ATTENUATOR": "Sound attenuator annual inspection",
    "DUCT_ACCESS_DOOR": "Duct access door annual inspection",
    "DUCT_ACCESSORY": "Duct accessory annual inspection",

    # --- Plumbing ---
    "WATER_HEATER": "Water heater quarterly inspection",
    "PUMP": "Pump monthly inspection",
    "SUMP_PUMP": "Sump pump quarterly operation test",
    "CIRCULATOR_PUMP": "Circulator pump quarterly inspection",
    "VALVE": "Valve annual exercise and inspection",
    "BACKFLOW_PREVENTER": "Backflow preventer annual certification test",
    "STRAINER": "Strainer annual clean and inspection",
    "PIPE_ACCESSORY": "Pipe accessory annual inspection",
    "TANK": "Tank annual inspection",
    "SUMP_PIT": "Sump pit quarterly inspection",
    "WATER_CLOSET": "Water closet annual inspection",
    "URINAL": "Urinal annual inspection",
    "LAVATORY": "Lavatory annual inspection",
    "SHOWER": "Shower annual inspection",
    "SERVICE_SINK": "Service sink annual inspection",
    "DRINKING_FOUNTAIN": "Drinking fountain annual inspection and filter change",
    "FLOOR_DRAIN": "Floor drain annual clean and trap check",
    "ROOF_DRAIN": "Roof drain annual clean and inspection",
    "CLEANOUT": "Cleanout annual inspection",
    "VENT_FITTING": "Vent fitting annual inspection",
    "TRAP_PRIMER": "Trap primer annual inspection",
    "PLUMBING_FIXTURE": "Plumbing fixture annual inspection",

    # --- Controls ---
    "CONTROL_DEVICE": "Control device annual calibration check",
}


def job_plan_for(asset_type):
    return JOB_PLANS.get(asset_type, f"{asset_type.replace('_', ' ').title()} preventive maintenance")


# PM estimated hours by rough equipment weight. Keeps a diffuser from
# claiming the same labour as a chiller.
PM_HOURS_BY_CRITICALITY = {1: 0.25, 2: 0.5, 3: 1.0, 4: 2.0, 5: 3.0}

preventive_maintenance = []

pm_source = maintainable_assets if SUPPRESS_PM_FOR_NON_MAINTAINABLE else assets

for a in pm_source:
    base_hours = PM_HOURS_BY_CRITICALITY.get(a["criticality"], 1.0)
    jitter = (stable_int(a["asset_id"], 5) * 0.25)
    est = round(base_hours + jitter, 2)

    preventive_maintenance.append({
        "pm_id": f"PM-{a['asset_id']}",
        "asset_id": a["asset_id"],
        "building_id": BUILDING_ID,
        "location_id": a["location_id"],
        "asset_type": a["asset_type"],
        "system": a["system"],
        "job_plan": job_plan_for(a["asset_type"]),
        "frequency": a["pm_frequency"],
        "estimated_hours": est,
        "last_completed_date": a["last_pm_date"],
        "next_due_date": a["next_pm_date"],
        "status": "ACTIVE",
    })

print(f"  PM schedules generated: {len(preventive_maintenance)}")

# ---------------------------------------------------------------------
# workorders.json
# ---------------------------------------------------------------------

PROBLEM_BY_SYSTEM = {
    "HVAC": ["TEMP_COMPLAINT", "NO_COOLING", "NO_HEATING", "FILTER_ALARM",
             "VIBRATION", "CONTROL_FAILURE", "HIGH_TEMP", "CONDENSATE_LEAK",
             "AIRFLOW_LOW", "NOISE_COMPLAINT"],
    "Electrical": ["BREAKER_TRIP", "CONTROL_FAILURE", "INSPECTION_DEFICIENCY",
                   "NO_POWER", "LOOSE_DEVICE", "DAMAGED_COVER", "GFCI_TRIP"],
    "Plumbing": ["SEAL_LEAK", "LOW_PRESSURE", "INSPECTION_DEFICIENCY",
                 "CLOG", "DRIP_LEAK", "NOISE_COMPLAINT"],
    "Fire Protection": ["INSPECTION_DEFICIENCY", "LOW_PRESSURE", "ACCESS_ISSUE",
                        "TEST_FAILURE", "OBSTRUCTION"],
    "Life Safety": ["INSPECTION_DEFICIENCY", "CONTROL_FAILURE", "TEST_FAILURE",
                    "BATTERY_FAILURE"],
    "Lighting": ["LAMP_OUT", "INSPECTION_DEFICIENCY", "CONTROL_FAILURE",
                 "PM_OVERDUE", "FLICKER"],
}

FAILURE_BY_SYSTEM = {
    "HVAC": "HVAC",
    "Electrical": "ELECTRICAL",
    "Plumbing": "PLUMBING",
    "Fire Protection": "FIRE_PROTECTION",
    "Life Safety": "LIFE_SAFETY",
    "Lighting": "LIGHTING",
}

# Weighted so most work orders are historical/closed rather than open.
STATUSES = ["OPEN", "IN_PROGRESS", "WAITING_PARTS",
            "CLOSED", "CLOSED", "CLOSED", "CLOSED", "CANCELED"]

# Types that should rarely generate work orders even though they are
# nominally maintainable. Keeps 400+ receptacles from dominating the WO file.
LOW_WO_RATE_TYPES = {
    "DUPLEX_RECEPTACLE", "QUADRUPLEX_RECEPTACLE", "RECEPTACLE",
    "JUNCTION_BOX", "DATA_OUTLET", "LIGHT_SWITCH", "FLOOR_BOX",
    "SUPPLY_DIFFUSER", "RETURN_GRILLE", "EXHAUST_GRILLE", "TRANSFER_GRILLE",
    "LINEAR_SLOT_DIFFUSER", "AIR_TERMINAL",
}


def wo_count_for_asset(a):
    """Work order volume driven by condition, criticality and asset class.
    Non-maintainable assets get none when suppression is on."""
    if SUPPRESS_WO_FOR_NON_MAINTAINABLE and not a["maintainable"]:
        return 0

    score = a.get("condition_score", 80)
    crit = a.get("criticality", 3)
    h = stable_int(a["asset_id"], 100)

    if a["asset_type"] in LOW_WO_RATE_TYPES:
        # ~12% of these get a single work order.
        return 1 if h % 100 < 12 else 0

    if score < 65:
        return 2
    if score < 75 or crit >= 5:
        return 1 if h % 100 < 70 else 2
    return 1 if h % 100 < 25 else 0


workorders = []
wo_seq = 1

for a in assets:
    count = wo_count_for_asset(a)
    if count <= 0:
        continue

    problems = PROBLEM_BY_SYSTEM.get(
        a["system"], ["INSPECTION_DEFICIENCY", "ACCESS_ISSUE", "PM_OVERDUE"]
    )
    failure_class = FAILURE_BY_SYSTEM.get(a["system"], "GENERAL")

    for i in range(count):
        h = stable_int(f"{a['asset_id']}-{i}", 1000)
        status = STATUSES[h % len(STATUSES)]

        if status in ["OPEN", "IN_PROGRESS", "WAITING_PARTS"]:
            reported = date(2026, 10, 1) + timedelta(days=h % 20)
            completed = None
        elif status == "CANCELED":
            reported = date(2026, 8, 1) + timedelta(days=h % 60)
            completed = None
        else:
            reported = date(2025, 11, 1) + timedelta(days=h % 330)
            completed = reported + timedelta(days=1 + h % 10)

        priority = 3
        if a.get("criticality", 3) >= 5 and a.get("condition_score", 80) < 75:
            priority = 2
        if status == "OPEN" and a.get("condition_score", 80) < 65:
            priority = 2
        if a["asset_type"] in ["FIRE_PUMP", "FIRE_HYDRANT", "FIRE_DAMPER",
                               "FIRE_SMOKE_DAMPER", "GENERATOR", "PUMP"] \
                and a.get("condition_score", 80) < 60:
            priority = 1
        if a["asset_type"] in LOW_WO_RATE_TYPES:
            priority = 4

        # Labour scaled by criticality so a diffuser cleaning is not
        # charged like a chiller teardown.
        base_labor = PM_HOURS_BY_CRITICALITY.get(a["criticality"], 1.0)
        labor = round(base_labor + (h % 8) * 0.5, 1)
        material = round(a["replacement_cost"] * 0.02 + (h % 12) * 15, 2)
        total = round(material + labor * 145, 2)

        problem = problems[h % len(problems)]

        workorders.append({
            "workorder_id": f"WO-{BUILDING_ID}-{wo_seq:06d}",
            "asset_id": a["asset_id"],
            "asset_type": a["asset_type"],

            "building_id": BUILDING_ID,
            "location_id": a["location_id"],
            "level_name": a.get("level_name"),
            "room_number": a.get("room_number"),
            "room_name": a.get("room_name"),

            "status": status,
            "priority": priority,
            "problem_code": problem,
            "failure_class": failure_class,
            "description": (
                f"Synthetic work order for {problem.lower().replace('_', ' ')} "
                f"on {a['asset_type'].replace('_', ' ').lower()}"
            ),

            "reported_date": reported.isoformat(),
            "target_start_date": reported.isoformat(),
            "target_finish_date": (reported + timedelta(days=3 + h % 5)).isoformat(),
            "completed_date": completed.isoformat() if completed else None,

            "labor_hours": labor,
            "material_cost": material,
            "total_cost": total,

            "reported_by_role": "FACILITIES_TECH",
            "is_emergency": priority == 1,
        })
        wo_seq += 1

print(f"  work orders generated: {len(workorders)}")

# Recompute open counts and risk now that work orders exist.
open_counts = {}
for w in workorders:
    if w["status"] in ["OPEN", "IN_PROGRESS", "WAITING_PARTS"]:
        open_counts[w["asset_id"]] = open_counts.get(w["asset_id"], 0) + 1

for a in assets:
    a["open_workorder_count"] = open_counts.get(a["asset_id"], 0)
    a["risk_score"] = (
        a["criticality"] * (100 - a["condition_score"] + 1)
        + a["failure_count_12mo"] * 10
        + a["open_workorder_count"] * 15
    )

# ---------------------------------------------------------------------
# asset_relationships.json
#
# All geometry here is MECH-to-MECH, so there is no cross-model coordinate
# risk. Relationships are additionally restricted to the same level and
# capped per parent so the file cannot balloon.
#
# Panel -> receptacle POWERS relationships are NOT generated: the extract
# has no Panel or Circuit Number parameter, so any such link would be a
# nearest-neighbour guess. An agent answering "what is on panel LP-3A?"
# would be confidently wrong. Enable only with real circuit data.
# ---------------------------------------------------------------------

relationships = []
rel_keys = set()


def same_level(a, b):
    la, lb = a.get("level_name"), b.get("level_name")
    return la is not None and la == lb


def dist_assets(a, b):
    return distance_xy((a.get("x"), a.get("y")), (b.get("x"), b.get("y")))


def add_rel(parent, child, typ, desc, distance=None):
    if not parent or not child:
        return
    if parent["asset_id"] == child["asset_id"]:
        return
    key = (parent["asset_id"], child["asset_id"], typ)
    if key in rel_keys:
        return
    rel_keys.add(key)
    relationships.append({
        "parent_asset_id": parent["asset_id"],
        "child_asset_id": child["asset_id"],
        "parent_asset_type": parent["asset_type"],
        "child_asset_type": child["asset_type"],
        "relationship_type": typ,
        "description": desc,
        "same_level": same_level(parent, child),
        "distance_xy": round(distance, 3) if distance is not None else None,
        "basis": "SYNTHETIC_PROXIMITY",
    })


by_type = {}
for a in assets:
    by_type.setdefault(a["asset_type"], []).append(a)


def nearest_on_level(source, candidates, limit, max_dist=None):
    pool = [c for c in candidates if same_level(source, c)]
    scored = []
    for c in pool:
        d = dist_assets(source, c)
        if d == float("inf"):
            continue
        if max_dist is not None and d > max_dist:
            continue
        scored.append((d, c))
    scored.sort(key=lambda t: t[0])
    return scored[:limit]


# Panels / boards power nearby signific```python
# Panels / boards power nearby significant equipment on the same level.
# Receptacles and other devices are deliberately excluded here - see note above.
panels = (
    by_type.get("ELECTRICAL_PANEL", [])
    + by_type.get("TERMINAL_BOARD", [])
    + by_type.get("CONTROL_PANEL", [])
    + by_type.get("MOTOR_CONTROL_CENTER", [])
)

POWERED_EQUIPMENT_TYPES = {
    "WSHP", "GSHP", "HEAT_PUMP",
    "AHU", "RTU", "ENERGY_RECOVERY_UNIT",
    "VRF_OUTDOOR_UNIT", "HVAC_INDOOR_UNIT", "FCU", "UNIT_HEATER",
    "CHILLER", "COOLING_TOWER", "BOILER",
    "HUMIDIFIER", "DEHUMIDIFIER", "AIR_COMPRESSOR",
    "FAN", "EXHAUST_FAN",
    "PUMP", "SUMP_PUMP", "CIRCULATOR_PUMP", "FIRE_PUMP",
    "WATER_HEATER",
    "VAV_FAN_POWERED",
    "CONTROL_PANEL", "VFD",
    "FIRE_DAMPER", "FIRE_SMOKE_DAMPER", "SMOKE_DAMPER",
}

powered_pool = [a for a in assets if a["asset_type"] in POWERED_EQUIPMENT_TYPES]

if GENERATE_PANEL_TO_RECEPTACLE_RELATIONSHIPS:
    powered_pool += [
        a for a in assets
        if a["asset_type"] in {
            "DUPLEX_RECEPTACLE", "QUADRUPLEX_RECEPTACLE", "RECEPTACLE",
            "GFCI_RECEPTACLE", "FLOOR_BOX", "JUNCTION_BOX",
            "LIGHT_SWITCH", "DIMMER_SWITCH",
        }
    ]

for p in panels:
    for d, c in nearest_on_level(p, powered_pool, limit=8, max_dist=120.0):
        add_rel(
            p, c, "POWERS",
            "Synthetic proximity relationship: panel powers nearby equipment on same level. "
            "Not derived from circuit data.",
            d,
        )

# VRF outdoor units serve indoor units.
for outdoor in by_type.get("VRF_OUTDOOR_UNIT", []):
    for d, c in nearest_on_level(outdoor, by_type.get("HVAC_INDOOR_UNIT", []),
                                 limit=10, max_dist=200.0):
        add_rel(outdoor, c, "SERVES",
                "Synthetic proximity relationship: VRF outdoor unit serves indoor unit", d)

# Air handling equipment serves air terminals on the same level.
AIR_SOURCE_TYPES = ["AHU", "RTU", "ENERGY_RECOVERY_UNIT", "FCU",
                    "WSHP", "GSHP", "HEAT_PUMP", "HVAC_INDOOR_UNIT"]
AIR_TERMINAL_TYPES = ["SUPPLY_DIFFUSER", "RETURN_GRILLE", "EXHAUST_GRILLE",
                      "LINEAR_SLOT_DIFFUSER", "TRANSFER_GRILLE", "AIR_TERMINAL",
                      "VAV", "VAV_FAN_POWERED", "CAV_TERMINAL"]

air_terminal_pool = []
for t in AIR_TERMINAL_TYPES:
    air_terminal_pool += by_type.get(t, [])

for t in AIR_SOURCE_TYPES:
    for src in by_type.get(t, []):
        for d, c in nearest_on_level(src, air_terminal_pool, limit=12, max_dist=100.0):
            add_rel(src, c, "SERVES",
                    "Synthetic proximity relationship: air source equipment serves nearby air terminal", d)

# VAV boxes serve downstream diffusers.
downstream_terminals = (
    by_type.get("SUPPLY_DIFFUSER", [])
    + by_type.get("LINEAR_SLOT_DIFFUSER", [])
)
for vav in by_type.get("VAV", []) + by_type.get("VAV_FAN_POWERED", []):
    for d, c in nearest_on_level(vav, downstream_terminals, limit=6, max_dist=60.0):
        add_rel(vav, c, "SERVES",
                "Synthetic proximity relationship: VAV terminal serves nearby supply diffuser", d)

# Pumps support tanks, pits and fire-protection assets.
for pump_type in ["PUMP", "SUMP_PUMP", "CIRCULATOR_PUMP", "FIRE_PUMP"]:
    targets = (
        by_type.get("TANK", [])
        + by_type.get("SUMP_PIT", [])
        + by_type.get("FIRE_HYDRANT", [])
        + by_type.get("STANDPIPE", [])
    )
    for pump in by_type.get(pump_type, []):
        for d, c in nearest_on_level(pump, targets, limit=4, max_dist=150.0):
            add_rel(pump, c, "SUPPORTS",
                    "Synthetic proximity relationship: pump supports nearby plumbing/fire-protection asset", d)

# Valves support water and fire-protection assets.
for valve in by_type.get("VALVE", []) + by_type.get("BACKFLOW_PREVENTER", []):
    if valve.get("system") in ["Plumbing", "Fire Protection", "HVAC"]:
        targets = (
            by_type.get("FIRE_HYDRANT", [])
            + by_type.get("STANDPIPE", [])
            + by_type.get("SPRINKLER", [])
            + by_type.get("TANK", [])
            + by_type.get("WATER_HEATER", [])
        )
        for d, c in nearest_on_level(valve, targets, limit=2, max_dist=80.0):
            add_rel(valve, c, "SUPPORTS",
                    "Synthetic proximity relationship: valve supports nearby water/fire-protection asset", d)

# Fire and smoke dampers support fan systems.
for damper_type in ["FIRE_DAMPER", "FIRE_SMOKE_DAMPER", "SMOKE_DAMPER"]:
    targets = by_type.get("FAN", []) + by_type.get("EXHAUST_FAN", []) + by_type.get("AHU", [])
    for fd in by_type.get(damper_type, []):
        for d, c in nearest_on_level(fd, targets, limit=3, max_dist=120.0):
            add_rel(fd, c, "SUPPORTS",
                    "Synthetic proximity relationship: damper provides isolation for nearby fan/air system", d)

# NOTE: duct and pipe fittings are intentionally excluded from all
# relationship generation. With ~1,400 of them, proximity-based links would
# dominate the file without adding usable meaning.

print(f"  relationships generated: {len(relationships)}")

# ---------------------------------------------------------------------
# classification_audit.json
#
# This is the file that would have caught every bug found in the previous
# version: the fitting exclusions, the Air Terminal fall-through, the WSHP
# fall-through, and the null symbol dimensions. Check it after every run.
# ---------------------------------------------------------------------

audit_categories = {}
for cat, c in sorted(audit.items(), key=lambda kv: -kv[1]["seen"]):
    entry = {
        "records_seen": c["seen"],
        "classified_as_assets": c["classified"],
        "skipped_excluded_category": c["excluded_category"],
        "skipped_context_object": c["context_object"],
        "skipped_unhandled_category": c["unhandled_category"],
        "skipped_unrecognised_in_category": c["unrecognised_in_category"],
        "skipped_no_unique_id": c["no_unique_id"],
        "skipped_duplicate_unique_id": c["duplicate_uid"],
        "asset_types": dict(sorted(c["asset_types"].items(), key=lambda kv: -kv[1])),
        "xy_extent": {
            "min_x": c["min_x"], "max_x": c["max_x"],
            "min_y": c["min_y"], "max_y": c["max_y"],
        },
    }
    unresolved = dict(sorted(c["unresolved_families"].items(), key=lambda kv: -kv[1])[:25])
    if unresolved:
        entry["unresolved_family_names_top25"] = unresolved
    audit_categories[cat] = entry

# Location assignment quality - the single most important metric for an
# agent whose job is locating assets.
method_counts = {}
confidence_counts = {}
level_source_counts = {}
dimension_source_counts = {}
for a in assets:
    m = a["room_assignment_method"]
    method_counts[m] = method_counts.get(m, 0) + 1
    cf = a["location_confidence"]
    confidence_counts[cf] = confidence_counts.get(cf, 0) + 1
    ls = a.get("level_source") or "NONE"
    level_source_counts[ls] = level_source_counts.get(ls, 0) + 1
    ds = a.get("dimension_source") or "NONE"
    dimension_source_counts[ds] = dimension_source_counts.get(ds, 0) + 1

match_distances = [a["room_match_distance"] for a in assets
                   if isinstance(a.get("room_match_distance"), (int, float))]
distance_stats = None
if match_distances:
    match_distances.sort()
    n = len(match_distances)
    distance_stats = {
        "count": n,
        "min": round(match_distances[0], 3),
        "median": round(match_distances[n // 2], 3),
        "p90": round(match_distances[int(n * 0.90)], 3),
        "max": round(match_distances[-1], 3),
    }

# Per-asset-type location quality, so a systematically mislocated category
# is visible rather than buried in the overall average.
type_location_quality = {}
for a in assets:
    t = type_location_quality.setdefault(a["asset_type"], {
        "count": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "with_room": 0,
    })
    t["count"] += 1
    t[a["location_confidence"]] += 1
    if a.get("room_number") not in [None, ""]:
        t["with_room"] += 1

# Symbol/type join effectiveness.
resolved_syms = sum(1 for a in assets
                    if str(a.get("source_type_id")) in symbols_by_key)

classification_audit = {
    "generated_for": BUILDING_ID,
    "model_folder": MECH,
    "purpose": (
        "Per-category tally of what was classified, what was skipped, and why. "
        "Review after every run. A category showing records_seen > 0 with "
        "classified_as_assets == 0 is a silent drop and should be investigated."
    ),
    "source_record_counts": {
        "family_instances": len(family_instances),
        "generic_models": len(mech_generic),
        "spaces": len(mech_spaces),
        "rooms": len(mech_rooms),
        "levels": len(levels),
        "type_symbol_records": len(symbols),
    },
    "categories": audit_categories,
    "location_assignment": {
        "by_method": dict(sorted(method_counts.items(), key=lambda kv: -kv[1])),
        "by_confidence": dict(sorted(confidence_counts.items(), key=lambda kv: -kv[1])),
        "by_level_source": dict(sorted(level_source_counts.items(), key=lambda kv: -kv[1])),
        "match_distance_stats": distance_stats,
        "max_space_match_dist_setting": MAX_SPACE_MATCH_DIST,
        "note": (
            "All geometry comparisons are MECH-to-MECH within one coordinate "
            "system. ROOM_NAME_MATCH is a direct textual room reference and is "
            "the most reliable. LEVEL_ONLY_ROOM_TOO_FAR means the nearest "
            "spatial record exceeded max_space_match_dist_setting and was "
            "deliberately not asserted as a match."
        ),
    },
    "location_quality_by_asset_type": dict(
        sorted(type_location_quality.items(), key=lambda kv: -kv[1]["count"])
    ),
    "dimension_resolution": {
        "by_source": dict(sorted(dimension_source_counts.items(), key=lambda kv: -kv[1])),
        "type_id_join_resolved": resolved_syms,
        "type_id_join_unresolved": len(assets) - resolved_syms,
        "note": (
            "TYPE_PARAMETERS means dimensions came from the type record's "
            "parameters array. BOUNDING_BOX means the type join missed or "
            "carried no dimension parameters, so instance bounding box extents "
            "were used instead. A high unresolved count means type_id values in "
            "family_instances do not match the identifiers exposed by the type "
            "or symbol files."
        ),
    },
    "suppression_settings": {
        "suppress_pm_for_non_maintainable": SUPPRESS_PM_FOR_NON_MAINTAINABLE,
        "suppress_wo_for_non_maintainable": SUPPRESS_WO_FOR_NON_MAINTAINABLE,
        "generate_panel_to_receptacle_relationships": GENERATE_PANEL_TO_RECEPTACLE_RELATIONSHIPS,
        "non_maintainable_types": sorted(NON_MAINTAINABLE_TYPES),
        "low_wo_rate_types": sorted(LOW_WO_RATE_TYPES),
    },
}

# ---------------------------------------------------------------------
# data_dictionary.json
# ---------------------------------------------------------------------

asset_type_counts = {}
for a in assets:
    asset_type_counts[a["asset_type"]] = asset_type_counts.get(a["asset_type"], 0) + 1

data_dictionary = {
    "dataset_name": "CA8888 Synthetic Maximo-Style Facilities Dataset (MECH-only)",
    "notice": (
        "All records in CA8888_SYNTHETIC_MAXIMO are synthetic and generated for an MCP server "
        "and model-viewer demonstration. This is not a real Maximo export and contains no real "
        "work orders, personnel records, vendor records, maintenance history, or operational "
        "vulnerabilities. Condition scores, costs, manufacturers, model numbers, serial numbers, "
        "PM dates, and work order history are all fabricated."
    ),
    "scope": (
        "Derived exclusively from the MECH Revit model. The ARCH and STRUC models are not read. "
        "This removes the ARCH/MECH coordinate origin mismatch and the ARCH/MECH level naming "
        "mismatch from the pipeline entirely: there is one coordinate system and one set of "
        "level names, so no cross-model geometry or name crosswalk is performed anywhere."
    ),
    "source_revit_folder": MECH,
    "source_files_read": {
        "project_info": "project_info.json",
        "levels": "levels.json",
        "spaces": "spaces.json",
        "rooms": "rooms.json (used as spatial source when present)",
        "family_instances": "family_instances.json",
        "family_symbols": "family_symbols.json / family_types.json / type_parameters.json / types.json (all stems tried and merged)",
        "generic_models": "generic_models.json (if present)",
    },
    "tables": {
        "locations.json": (
            "Location hierarchy: BUILDING -> LEVEL -> ROOM. Levels come from MECH levels.json "
            "sorted by elevation. Rooms come from MECH rooms.json, or spaces.json when rooms.json "
            "is absent."
        ),
        "assets.json": (
            "Maintainable and non-maintainable asset records, linked back to the Revit model by "
            "source_unique_id, source_type_id, and source_family_id. source_unique_id is the join "
            "key for highlighting geometry in a model viewer."
        ),
        "workorders.json": "Synthetic work order history and open work linked to assets.",
        "preventive_maintenance.json": "Synthetic PM schedules linked to maintainable assets.",
        "asset_relationships.json": "Synthetic proximity-based dependency relationships between assets.",
        "classification_audit.json": (
            "Diagnostic output. Per-category classification tallies, location assignment quality, "
            "and dimension resolution provenance. Review after every run."
        ),
    },
    "key_fields": {
        "source_unique_id": "Revit UniqueId of the source instance. Stable join key to the model.",
        "asset_type": "Granular synthetic asset type code.",
        "maximo_classification": "Dotted synthetic classification path. Placeholder taxonomy, not validated against any real Maximo classification tree.",
        "maintainable": "False for duct and pipe fittings, which are included for model location Q&A but are not treated as discrete maintainable CMMS assets.",
        "location_id": "Foreign key into locations.json.",
        "room_assignment_method": "How the location was derived: ROOM_NAME_MATCH, NEAREST_ROOM_ON_LEVEL, LEVEL_ONLY, LEVEL_ONLY_ROOM_TOO_FAR, or BUILDING_FALLBACK.",
        "location_confidence": "HIGH, MEDIUM, or LOW. Agents should hedge their wording on LOW.",
        "room_match_distance": "XY distance in model units from the asset to the matched spatial record centroid. Null when not applicable.",
        "level_source": "PARAMETER when the record carried a level, Z_ELEVATION when the level was derived from the z coordinate against levels.json elevations.",
        "dimension_source": "TYPE_PARAMETERS, BOUNDING_BOX, or NONE.",
        "attributes": "Type-specific descriptive fields parsed from family and type names and type parameters.",
    },
    "classification_summary": {
        "included_asset_types": dict(sorted(asset_type_counts.items(), key=lambda kv: -kv[1])),
        "newly_included_vs_prior_version": [
            "Electrical Fixtures (previously fell through all classifier branches and produced zero assets)",
            "Air Terminals (previously fell through all classifier branches and produced zero assets)",
            "Duct Fittings (previously hard-excluded in excluded_cats)",
            "Pipe Fittings (previously hard-excluded in excluded_cats)",
            "Mechanical Equipment / M_WSHP (previously fell through: 'm_wshp' contains no searchable keyword)",
        ],
        "excluded_categories": sorted(EXCLUDED_CATEGORIES),
        "excluded_context_phrases": list(CONTEXT_PHRASES),
    },
    "known_limitations": [
        "No System Classification or System Name parameter is present in this extract, so duct and "
        "pipe fittings are classified by geometry only. The dataset cannot distinguish a chilled "
        "water elbow from a sanitary elbow.",
        "No Panel or Circuit Number parameter is present, so no panel-to-receptacle POWERS "
        "relationships are generated. Questions like 'what is on panel LP-3A' cannot be answered "
        "from this dataset and the agent should say so rather than infer.",
        "All relationships are proximity-based and same-level only. They carry basis = "
        "SYNTHETIC_PROXIMITY and are not derived from Revit system connectivity.",
        "Electrical Fixtures in this extract carry no level parameter. Their level is derived from "
        "the z coordinate against levels.json elevations, with a 1.0 unit tolerance.",
        "Because only the MECH model is read, any assets that existed solely in the ARCH or STRUC "
        "models are absent. This notably includes STRUC generic_models sump pits, and any lighting "
        "fixtures authored in the ARCH model.",
        "maximo_classification values are a placeholder taxonomy invented for this dataset. They "
        "have not been validated against any agency Maximo classification structure.",
    ],
    "risk_score_formula": (
        "risk_score = criticality * (100 - condition_score + 1) "
        "+ failure_count_12mo * 10 + open_workorder_count * 15"
    ),
    "generation_notes": [
        "Source Revit JSON files are never modified.",
        "Assets preserve original source_unique_id values.",
        "No geometry is compared across models, because only one model is read.",
        "nearest_room_on_level applies a hard distance cap (MAX_SPACE_MATCH_DIST) so a distant "
        "room is never asserted as a match. Beyond the cap, the asset gets level-only assignment "
        "with LOW confidence.",
        "Duct and pipe fittings receive asset records but no PM schedules, almost no work orders, "
        "and no relationships.",
        "Asset ID sequence is zero-padded to 4 digits because a single fitting type on a single "
        "level can exceed 999 records.",
    ],
    "record_counts": {
        "locations.json": len(locations),
        "assets.json": len(assets),
        "assets_maintainable": len(maintainable_assets),
        "assets_non_maintainable": len(assets) - len(maintainable_assets),
        "workorders.json": len(workorders),
        "preventive_maintenance.json": len(preventive_maintenance),
        "asset_relationships.json": len(relationships),
    },
}

# ---------------------------------------------------------------------
# Write output files
# ---------------------------------------------------------------------

def write_json(name, data):
    with open(OUT / name, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

write_json("locations.json", locations)
write_json("assets.json", assets)
write_json("workorders.json", workorders)
write_json("preventive_maintenance.json", preventive_maintenance)
write_json("asset_relationships.json", relationships)
write_json("classification_audit.json", classification_audit)
write_json("data_dictionary.json", data_dictionary)

# ---------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------

print()
print("=" * 72)
print(f"Generated synthetic Maximo-style dataset in {OUT}")
print("=" * 72)
print(f"  locations.json               {len(locations):>7}")
print(f"  assets.json                  {len(assets):>7}  "

"({len(maintainable_assets)} maintainable)")
print(f"  workorders.json              {len(workorders):>7}")
print(f"  preventive_maintenance.json  {len(preventive_maintenance):>7}")
print(f"  asset_relationships.json     {len(relationships):>7}")
print(f"  classification_audit.json")
print(f"  data_dictionary.json")

print()
print("Asset counts by type (top 30):")
for t, n in sorted(asset_type_counts.items(), key=lambda kv: -kv[1])[:30]:
    flag = "" if is_maintainable(t) else "   [non-maintainable]"
    print(f"  {t:<28} {n:>6}{flag}")
if len(asset_type_counts) > 30:
    print(f"  ... and {len(asset_type_counts) - 30} more types")

print()
print("Previously-missing categories - verify these are now non-zero:")
WATCH_CATEGORIES = ["Electrical Fixtures", "Air Terminals",
                    "Duct Fittings", "Pipe Fittings", "Mechanical Equipment"]
for cat in WATCH_CATEGORIES:
    c = audit.get(cat)
    if c is None:
        print(f"  {cat:<24} NOT FOUND IN SOURCE DATA  <-- check category name spelling")
        continue
    status = "OK" if c["classified"] > 0 else "*** STILL ZERO ***"
    print(f"  {cat:<24} seen {c['seen']:>6}  classified {c['classified']:>6}   {status}")

print()
print("WSHP check:")
wshp_count = asset_type_counts.get("WSHP", 0)
if wshp_count > 0:
    print(f"  WSHP assets generated: {wshp_count}")
    sample = next((a for a in assets if a["asset_type"] == "WSHP"), None)
    if sample:
        print(f"  sample: {sample['asset_id']}")
        print(f"          {sample['source_family_name']}")
        print(f"          level={sample['level_name']}  "
              f"room={sample['room_number']}  conf={sample['location_confidence']}")
        print(f"          attributes={json.dumps(sample['attributes'])}")
else:
    print("  *** NO WSHP ASSETS GENERATED ***")
    print("  Check classification_audit.json -> categories -> 'Mechanical Equipment'")
    print("  If records_seen is 0, the WSHPs are not in family_instances.json.")

print()
print("Location assignment quality:")
for m, n in sorted(method_counts.items(), key=lambda kv: -kv[1]):
    pct = 100.0 * n / len(assets) if assets else 0.0
    print(f"  {m:<28} {n:>6}  ({pct:>5.1f}%)")
print("  ---")
for cf in ["HIGH", "MEDIUM", "LOW"]:
    n = confidence_counts.get(cf, 0)
    pct = 100.0 * n / len(assets) if assets else 0.0
    print(f"  confidence {cf:<17} {n:>6}  ({pct:>5.1f}%)")

if distance_stats:
    print(f"  match distance  median={distance_stats['median']}  "
          f"p90={distance_stats['p90']}  max={distance_stats['max']}")

bldg_fallback = method_counts.get("BUILDING_FALLBACK", 0)
if bldg_fallback > len(assets) * 0.25 and assets:
    print()
    print(f"  WARNING: {bldg_fallback} assets ({100.0*bldg_fallback/len(assets):.1f}%) "
          f"fell back to building level.")
    print("  Likely causes: spaces.json/rooms.json level names do not match levels.json,")
    print("  or spatial records have no location_point coordinates.")
    print("  See classification_audit.json -> location_assignment.")

print()
print("Level distribution:")
level_counts = {}
for a in assets:
    k = a.get("level_name") or "(no level)"
    level_counts[k] = level_counts.get(k, 0) + 1
for k, n in sorted(level_counts.items(), key=lambda kv: -kv[1]):
    print(f"  {k:<28} {n:>6}")

print()
print("Dimension resolution:")
for ds, n in sorted(dimension_source_counts.items(), key=lambda kv: -kv[1]):
    print(f"  {ds:<28} {n:>6}")
print(f"  type_id join resolved:   {resolved_syms}")
print(f"  type_id join unresolved: {len(assets) - resolved_syms}")

unhandled = {cat: c for cat, c in audit.items()
             if c["unhandled_category"] > 0 or c["unrecognised_in_category"] > 0}
if unhandled:
    print()
    print("Categories with unclassified records (potential silent drops):")
    for cat, c in sorted(unhandled.items(), key=lambda kv: -(kv[1]["unhandled_category"]
                                                             + kv[1]["unrecognised_in_category"])):
        total_skipped = c["unhandled_category"] + c["unrecognised_in_category"]
        print(f"  {cat:<32} {total_skipped:>6} unclassified of {c['seen']} seen")
    print("  Review classification_audit.json -> unresolved_family_names_top25")
    print("  for each, then add handlers or keywords as needed.")

print()
print("Done. Review classification_audit.json before using this dataset.")
