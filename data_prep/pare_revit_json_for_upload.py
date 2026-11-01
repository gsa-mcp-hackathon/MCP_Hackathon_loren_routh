#!/usr/bin/env python3
"""
Pare down CA8888 translated Revit JSON/TXT files for upload/use in chat.

This script reads source files from:
  CA8888_ARCH_JSON
  CA8888_MECH_JSON
  CA8888_STRUC_JSON

It writes reduced files to:
  CA8888_REDUCED_UPLOAD

It does NOT modify source files.

It supports source files ending in .json or .txt.
"""

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


ARCH_FOLDER = "CA8888_ARCH_JSON"
MECH_FOLDER = "CA8888_MECH_JSON"
STRUC_FOLDER = "CA8888_STRUC_JSON"

DEFAULT_OUTPUT = "CA8888_REDUCED_UPLOAD"


ASSET_CANDIDATE_CATEGORIES = {
    "Mechanical Equipment",
    "Electrical Equipment",
    "Plumbing Fixtures",
    "Sprinklers",
    "Fire Alarm Devices",
    "Communication Devices",
    "Data Devices",
    "Security Devices",
    "Lighting Fixtures",
    "Specialty Equipment",
}

EXCLUDED_CATEGORIES = {
    "Title Blocks",
    "View Titles",
    "Callout Heads",
    "Grids",
    "Levels",
    "Rooms",
    "Areas",
    "Walls",
    "Floors",
    "Ceilings",
    "Doors",
    "Windows",
    "Curtain Panels",
    "Curtain Wall Mullions",
    "Structural Columns",
    "Architectural Columns",
}

ASSET_KEYWORDS = [
    "AHU",
    "Air Handling",
    "VAV",
    "Fan Coil",
    "FCU",
    "Pump",
    "Fan",
    "Exhaust",
    "Panel",
    "Transformer",
    "Generator",
    "Sprinkler",
    "Fire Hydrant",
    "Sump Pit",
    "Fire Alarm",
    "Smoke",
    "Detector",
    "Valve",
    "Tank",
    "Heater",
    "Boiler",
    "Chiller",
]


def read_json_file(path: Path) -> Any:
    """Read a JSON document from .json or .txt."""
    text = path.read_text(encoding="utf-8-sig")
    return json.loads(text)


def write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def find_source_file(folder: Path, base_name: str) -> Optional[Path]:
    """
    Find source file by base name, preferring .json, then .txt.

    Example:
      base_name="rooms"
      checks rooms.json then rooms.txt
    """
    for ext in [".json", ".txt"]:
        candidate = folder / f"{base_name}{ext}"
        if candidate.exists():
            return candidate
    return None


def get_nested(record: Dict[str, Any], path: str, default=None):
    """
    Get nested value from a dictionary.

    Supports:
      record["location"]["x"]
      record["location.x"] if the extractor used literal dotted keys
    """
    if path in record:
        return record.get(path, default)

    current = record
    for part in path.split("."):
        if not isinstance(current, dict):
            return default
        if part not in current:
            return default
        current = current[part]
    return current


def compact_subobject(record: Dict[str, Any], object_name: str, fields: List[str]) -> Optional[Dict[str, Any]]:
    """
    Extract a nested object, supporting both nested and dotted source fields.

    Example:
      object_name="location"
      fields=["type", "x", "y", "z", "rotation"]

    Reads:
      record["location"]["x"]
    or:
      record["location.x"]
    """
    output = {}

    nested_obj = record.get(object_name)
    if isinstance(nested_obj, dict):
        for field in fields:
            if field in nested_obj:
                output[field] = nested_obj.get(field)

    for field in fields:
        dotted_key = f"{object_name}.{field}"
        if dotted_key in record:
            output[field] = record.get(dotted_key)

    return output if output else None


def compact_common_identity(record: Dict[str, Any]) -> Dict[str, Any]:
    fields = [
        "unique_id",
        "category_name",
        "family_name",
        "family_id",
        "type_name",
        "type_id",
        "level_name",
        "phase",
    ]
    return {field: record.get(field) for field in fields if field in record}


def compact_project_info(record: Dict[str, Any]) -> Dict[str, Any]:
    fields = [
        "building_number",
        "project_name",
        "project_number",
        "project_address",
        "project_status",
        "issue_date",
        "unique_id",
    ]
    return {field: record.get(field) for field in fields if field in record}


def compact_level(record: Dict[str, Any]) -> Dict[str, Any]:
    fields = [
        "unique_id",
        "category_name",
        "level_name",
        "elevation",
        "project_elevation",
        "is_building_story",
        "is_structural",
    ]
    return {field: record.get(field) for field in fields if field in record}


def compact_room(record: Dict[str, Any]) -> Dict[str, Any]:
    output = {}

    fields = [
        "unique_id",
        "category_name",
        "room_number",
        "room_name",
        "level_name",
        "area",
        "volume",
        "height",
        "unbounded_height",
    ]

    for field in fields:
        if field in record:
            output[field] = record.get(field)

    location_point = compact_subobject(
        record,
        "location_point",
        ["type", "x", "y", "z"]
    )

    if location_point:
        output["location_point"] = location_point

    # Intentionally exclude heavy fields:
    # boundary_geometry, geom, wkt

    return output


def compact_family_instance(record: Dict[str, Any]) -> Dict[str, Any]:
    output = compact_common_identity(record)

    location = compact_subobject(
        record,
        "location",
        ["type", "x", "y", "z", "rotation"]
    )
    if location:
        output["location"] = location

    bounding_box = compact_subobject(
        record,
        "bounding_box",
        ["min_x", "min_y", "min_z", "max_x", "max_y", "max_z"]
    )
    if bounding_box:
        output["bounding_box"] = bounding_box

    return output


def compact_family_symbol(record: Dict[str, Any], include_parameters: bool = False) -> Dict[str, Any]:
    fields = [
        "unique_id",
        "category_name",
        "family_name",
        "family_id",
        "type_name",
        "type_id",
        "thickness",
        "width",
        "depth",
        "diameter",
        "rotation",
        "sill_height",
    ]

    output = {field: record.get(field) for field in fields if field in record}

    # Parameters can be very large. Keep only if explicitly requested.
    if include_parameters and "parameters" in record:
        output["parameters"] = record.get("parameters")

    return output


def compact_generic_model(record: Dict[str, Any]) -> Dict[str, Any]:
    output = compact_common_identity(record)

    location = compact_subobject(
        record,
        "location",
        ["type", "x", "y", "z", "rotation"]
    )
    if location:
        output["location"] = location

    bounding_box = compact_subobject(
        record,
        "bounding_box",
        ["min_x", "min_y", "min_z", "max_x", "max_y", "max_z"]
    )
    if bounding_box:
        output["bounding_box"] = bounding_box

    return output


def text_blob(record: Dict[str, Any]) -> str:
    parts = [
        str(record.get("category_name", "")),
        str(record.get("family_name", "")),
        str(record.get("type_name", "")),
    ]
    return " ".join(parts)


def is_asset_candidate(record: Dict[str, Any]) -> bool:
    category = record.get("category_name")

    if category in EXCLUDED_CATEGORIES:
        return False

    if category in ASSET_CANDIDATE_CATEGORIES:
        return True

    blob = text_blob(record).lower()

    for keyword in ASSET_KEYWORDS:
        if keyword.lower() in blob:
            return True

    return False


def split_and_write(
    output_dir: Path,
    output_base_name: str,
    records: List[Dict[str, Any]],
    max_records_per_file: int
) -> List[Path]:
    """
    Write records as one file or multiple part files.
    """
    written = []

    if max_records_per_file <= 0 or len(records) <= max_records_per_file:
        output_path = output_dir / f"{output_base_name}.json"
        write_json_file(output_path, records)
        written.append(output_path)
        return written

    total_parts = math.ceil(len(records) / max_records_per_file)

    for part_idx in range(total_parts):
        start = part_idx * max_records_per_file
        end = start + max_records_per_file
        chunk = records[start:end]

        output_path = output_dir / f"{output_base_name}_part{part_idx + 1:03d}.json"
        write_json_file(output_path, chunk)
        written.append(output_path)

    return written


def reduce_file(
    source_path: Path,
    output_dir: Path,
    output_base_name: str,
    reducer,
    max_records_per_file: int,
    filter_func=None
) -> Dict[str, Any]:
    data = read_json_file(source_path)

    if isinstance(data, dict):
        records = [data]
    elif isinstance(data, list):
        records = data
    else:
        raise ValueError(f"Unsupported JSON root in {source_path}: expected list or object")

    reduced = []

    for record in records:
        if not isinstance(record, dict):
            continue

        if filter_func is not None and not filter_func(record):
            continue

        reduced.append(reducer(record))

    written = split_and_write(output_dir, output_base_name, reduced, max_records_per_file)

    return {
        "source": str(source_path),
        "output_base_name": output_base_name,
        "input_count": len(records),
        "output_count": len(reduced),
        "files_written": [str(p) for p in written],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Pare down CA8888 Revit JSON/TXT files for upload."
    )

    parser.add_argument(
        "--root",
        default=".",
        help="Root folder containing CA8888_ARCH_JSON, CA8888_MECH_JSON, and CA8888_STRUC_JSON. Default: current directory."
    )

    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output folder. Default: {DEFAULT_OUTPUT}"
    )

    parser.add_argument(
        "--max-records",
        type=int,
        default=500,
        help="Max records per output file before splitting. Use 0 for no splitting. Default: 500."
    )

    parser.add_argument(
        "--include-symbol-parameters",
        action="store_true",
        help="Include family_symbols.parameters. Usually not recommended because it can be large."
    )

    args = parser.parse_args()

    root = Path(args.root).resolve()
    output_root = Path(args.output).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    report = {
        "root": str(root),
        "output_root": str(output_root),
        "notes": [
            "Original source files were not modified.",
            "Heavy geometry fields such as boundary_geometry, geom, and wkt are excluded.",
            "MECH asset candidates are filtered using category_name, family_name, and type_name.",
        ],
        "files": [],
    }

    arch = root / ARCH_FOLDER
    mech = root / MECH_FOLDER
    struc = root / STRUC_FOLDER

    # ARCH files
    if arch.exists():
        arch_out = output_root / ARCH_FOLDER

        project_info = find_source_file(arch, "project_info")
        if project_info:
            report["files"].append(
                reduce_file(
                    project_info,
                    arch_out,
                    "project_info_min",
                    compact_project_info,
                    args.max_records
                )
            )

        levels = find_source_file(arch, "levels")
        if levels:
            report["files"].append(
                reduce_file(
                    levels,
                    arch_out,
                    "levels_min",
                    compact_level,
                    args.max_records
                )
            )

        rooms = find_source_file(arch, "rooms")
        if rooms:
            report["files"].append(
                reduce_file(
                    rooms,
                    arch_out,
                    "rooms_min",
                    compact_room,
                    args.max_records
                )
            )

    # MECH files
    if mech.exists():
        mech_out = output_root / MECH_FOLDER

        project_info = find_source_file(mech, "project_info")
        if project_info:
            report["files"].append(
                reduce_file(
                    project_info,
                    mech_out,
                    "project_info_min",
                    compact_project_info,
                    args.max_records
                )
            )

        levels = find_source_file(mech, "levels")
        if levels:
            report["files"].append(
                reduce_file(
                    levels,
                    mech_out,
                    "levels_min",
                    compact_level,
                    args.max_records
                )
            )

        rooms = find_source_file(mech, "rooms")
        if rooms:
            report["files"].append(
                reduce_file(
                    rooms,
                    mech_out,
                    "rooms_min",
                    compact_room,
                    args.max_records
                )
            )

        family_instances = find_source_file(mech, "family_instances")
        if family_instances:
            # Full reduced family instances
            report["files"].append(
                reduce_file(
                    family_instances,
                    mech_out,
                    "family_instances_min",
                    compact_family_instance,
                    args.max_records
                )
            )

            # Asset candidate subset
            report["files"].append(
                reduce_file(
                    family_instances,
                    mech_out,
                    "family_instances_asset_candidates_min",
                    compact_family_instance,
                    args.max_records,
                    filter_func=is_asset_candidate
                )
            )

        family_symbols = find_source_file(mech, "family_symbols")
        if family_symbols:
            reducer = lambda record: compact_family_symbol(
                record,
                include_parameters=args.include_symbol_parameters
            )

            report["files"].append(
                reduce_file(
                    family_symbols,
                    mech_out,
                    "family_symbols_min",
                    reducer,
                    args.max_records
                )
            )

    # STRUC files
    if struc.exists():
        struc_out = output_root / STRUC_FOLDER

        project_info = find_source_file(struc, "project_info")
        if project_info:
            report["files"].append(
                reduce_file(
                    project_info,
                    struc_out,
                    "project_info_min",
                    compact_project_info,
                    args.max_records
                )
            )

        levels = find_source_file(struc, "levels")
        if levels:
            report["files"].append(
                reduce_file(
                    levels,
                    struc_out,
                    "levels_min",
                    compact_level,
                    args.max_records
                )
            )

        generic_models = find_source_file(struc, "generic_models")
        if generic_models:
            report["files"].append(
                reduce_file(
                    generic_models,
                    struc_out,
                    "generic_models_min",
                    compact_generic_model,
                    args.max_records
                )
            )

            report["files"].append(
                reduce_file(
                    generic_models,
                    struc_out,
                    "generic_models_asset_candidates_min",
                    compact_generic_model,
                    args.max_records,
                    filter_func=is_asset_candidate
                )
            )

    report_path = output_root / "reduction_report.json"
    write_json_file(report_path, report)

    print(f"Done. Reduced files written to: {output_root}")
    print(f"Report written to: {report_path}")

    for item in report["files"]:
        print(
            f"- {item['output_base_name']}: "
            f"{item['input_count']} input -> {item['output_count']} output"
        )


if __name__ == "__main__":
    main()