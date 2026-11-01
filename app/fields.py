"""Fields the tools can filter and color by, including columns merged in from other tables."""
import re
from collections import Counter

from db import VIEW, query_rows, safe_name

# name -> (SQL expression over the elements view, aliased "w", and kind: "category" or "number")
BASE_FIELDS = {
    "level": ("w.LEVEL_NAME", "category"),
    "wall_type": ("w.WALL_TYPE", "category"),
    "wall_family": ("w.WALL_FAMILY", "category"),
    "building": ("CAST(w.BLDG_NO AS STRING)", "category"),
    "shell_core": ("CAST(w.IS_SHELLCORE AS STRING)", "category"),
    "width_ft": ("w.WIDTH", "number"),
    "height_ft": ("w.HEIGHT", "number"),
    "length_ft": ("w.LENGTH", "number"),
}

# Merge more columns onto the elements through a shared key (compared as text). Each entry adds one field.
# Example, once such a table exists:
# {"field": "department", "table": "main.default.assignments", "key": "UNIQUE_ID",
#  "column": "DEPARTMENT", "kind": "category"},
MERGES = []

PALETTE = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
           "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac"]
GREY = "#555555"
LOW, HIGH = (59, 130, 246), (239, 68, 68)  # numeric gradient: blue to red


def _ident(name):
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise RuntimeError(f"Unsafe column name: {name!r}")
    return name


def field_names():
    return list(BASE_FIELDS) + [m["field"] for m in MERGES]


def kind_of(field):
    if field in BASE_FIELDS:
        return BASE_FIELDS[field][1]
    return next(m.get("kind", "category") for m in MERGES if m["field"] == field)


def select_elements(fields, filters):
    """Rows of {UNIQUE_ID, <FIELD>...} for elements matching all filters ({field: [values]}).
    Unknown fields raise ValueError listing the valid ones."""
    filters = {f: v for f, v in filters.items() if v}  # an empty value list means no filter
    known = set(field_names())
    for f in list(fields) + list(filters):
        if f not in known:
            raise ValueError(f"Unknown field '{f}'. Valid fields: {', '.join(sorted(known))}")
    merges = {m["field"]: (i, m) for i, m in enumerate(MERGES)}
    expr, joins = {}, []
    for f in set(fields) | set(filters):
        if f in BASE_FIELDS:
            expr[f] = BASE_FIELDS[f][0]
        else:
            i, m = merges[f]
            expr[f] = f"m{i}.{_ident(m['column'])}"
            joins.append(f"LEFT JOIN {safe_name(m['table'])} m{i} "
                         f"ON CAST(w.UNIQUE_ID AS STRING) = CAST(m{i}.{_ident(m['key'])} AS STRING)")
    params, conditions = {}, []
    for n, (f, values) in enumerate(filters.items()):
        names = []
        for k, v in enumerate(values):
            params[f"f{n}_{k}"] = str(v)
            names.append(f":f{n}_{k}")
        conditions.append(f"CAST({expr[f]} AS STRING) IN ({', '.join(names)})")
    columns = "".join(f", {expr[f]} AS `{f}`" for f in fields)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return query_rows(f"SELECT w.UNIQUE_ID{columns} FROM {VIEW} w {' '.join(joins)} {where}", params)


def _mix(t):
    return "#%02x%02x%02x" % tuple(round(a + (b - a) * t) for a, b in zip(LOW, HIGH))


def build_colors(field, filters):
    """Returns ({UNIQUE_ID: "#rrggbb"}, legend, kind). Categories get one color each; numbers get a gradient."""
    rows = select_elements([field], filters)
    key, kind = field.upper(), kind_of(field)
    if kind == "number":
        values = [float(r[key]) for r in rows if r[key] is not None]
        lo, hi = (min(values), max(values)) if values else (0.0, 0.0)

        def colour(v):
            return _mix(0 if hi == lo else (float(v) - lo) / (hi - lo))

        colors = {r["UNIQUE_ID"]: colour(r[key]) if r[key] is not None else GREY for r in rows}
        legend = [{"label": f"{v:g}", "color": colour(v)} for v in (lo, (lo + hi) / 2, hi)]
        return colors, legend, kind
    labels = {r["UNIQUE_ID"]: (str(r[key]) if r[key] is not None else "(none)") for r in rows}
    counts = Counter(labels.values())
    assigned = {}
    for i, (label, _) in enumerate(counts.most_common()):
        assigned[label] = GREY if label == "(none)" else PALETTE[i % len(PALETTE)]
    colors = {uid: assigned[label] for uid, label in labels.items()}
    legend = [{"label": label, "color": assigned[label], "count": n} for label, n in counts.most_common(30)]
    return colors, legend, kind
