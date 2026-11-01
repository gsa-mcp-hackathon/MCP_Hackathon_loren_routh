"""CLI: upload a CSV dataset to Phoenix (or validate it without uploading).

Reads the dataset registry (datasets.yaml), locates the CSV, checks the required
columns exist, then uploads via the Phoenix client. Run from eval/phoenix/.

Examples:
    uv run python create_dataset.py --list
    uv run python create_dataset.py --dataset-name bim-viewer-eval-0 --dry-run
    uv run python create_dataset.py --dataset-name bim-viewer-eval-0
"""

from __future__ import annotations

import argparse
import os
import sys

import pandas as pd
import yaml

from eval.phoenix import _env

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(HERE, "datasets.yaml")
DEFAULT_PHOENIX_ENDPOINT = "http://localhost:6006"


def _load_registry() -> dict:
    with open(REGISTRY_PATH, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("datasets", {})


def _resolve_csv(entry: dict) -> str:
    csv_path = entry["csv_path"]
    if not os.path.isabs(csv_path):
        csv_path = os.path.join(HERE, csv_path)
    return csv_path


def _validate(entry: dict, csv_path: str) -> pd.DataFrame:
    if not os.path.exists(csv_path):
        raise SystemExit(f"CSV not found: {csv_path}")
    df = pd.read_csv(csv_path)
    required = list(entry.get("input_keys", [])) + list(
        entry.get("output_keys", [])
    )
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise SystemExit(
            f"CSV {csv_path} is missing required columns: {missing}. "
            f"Found: {list(df.columns)}"
        )
    if df.empty:
        raise SystemExit(f"CSV {csv_path} has no rows.")
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload a dataset to Phoenix.")
    parser.add_argument("--list", action="store_true", help="List datasets.")
    parser.add_argument("--dataset-name", help="Registry key to upload.")
    parser.add_argument(
        "--phoenix-name",
        help="Name to register in Phoenix (defaults to --dataset-name).",
    )
    parser.add_argument("--csv-path", help="Override the registry CSV path.")
    parser.add_argument(
        "--phoenix-endpoint",
        default=os.environ.get("PHOENIX_ENDPOINT", DEFAULT_PHOENIX_ENDPOINT),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the CSV but do not upload.",
    )
    args = parser.parse_args()

    registry = _load_registry()

    if args.list:
        if not registry:
            print("No datasets registered.")
            return
        for name, entry in registry.items():
            print(f"{name}: {entry.get('description', '').strip()}")
        return

    if not args.dataset_name:
        parser.error("--dataset-name is required (or use --list).")

    if args.dataset_name not in registry:
        raise SystemExit(
            f"Unknown dataset '{args.dataset_name}'. "
            f"Known: {list(registry)}"
        )

    entry = dict(registry[args.dataset_name])
    if args.csv_path:
        entry["csv_path"] = args.csv_path
    csv_path = _resolve_csv(entry)
    df = _validate(entry, csv_path)

    print(f"Validated {csv_path}: {len(df)} rows, columns {list(df.columns)}.")

    if args.dry_run:
        print("Dry run — not uploading.")
        return

    _env.load_eval_env()
    from phoenix.client import Client

    client = Client(base_url=args.phoenix_endpoint)
    phoenix_name = args.phoenix_name or args.dataset_name
    dataset = client.datasets.create_dataset(
        name=phoenix_name,
        dataframe=df,
        input_keys=list(entry.get("input_keys", [])),
        output_keys=list(entry.get("output_keys", [])),
        dataset_description=entry.get("description", "").strip(),
    )
    print(f"Uploaded dataset '{phoenix_name}' to Phoenix at {args.phoenix_endpoint}.")
    ds_id = getattr(dataset, "id", None)
    if ds_id:
        print(f"Dataset id: {ds_id}")


if __name__ == "__main__":
    sys.exit(main())
