"""Generate the dataset CSV from evaluation.xml (single source of truth).

The gold questions + expected-action descriptions live in evaluation.xml so the
human-readable rationale and the uploaded CSV never drift. This script reads the
<question>/<answer> pairs (matched by id) and writes the two-column CSV that
datasets.yaml points at: `query` (input) and `responses` (expected action).

Run from this directory:
    python build_csv.py
"""

from __future__ import annotations

import csv
import os
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
XML_PATH = os.path.join(HERE, "evaluation.xml")
CSV_PATH = os.path.join(HERE, "examples", "bim-viewer-eval-0.csv")


def _collapse(text: str) -> str:
    """Collapse XML whitespace to single-spaced, trimmed text."""
    return " ".join((text or "").split())


def main() -> None:
    tree = ET.parse(XML_PATH)
    root = tree.getroot()

    questions = {q.get("id"): _collapse(q.text) for q in root.findall("question")}
    answers = {a.get("id"): _collapse(a.text) for a in root.findall("answer")}

    ids = sorted(questions, key=lambda i: int(i))
    missing = [i for i in ids if i not in answers]
    if missing:
        raise SystemExit(f"Questions missing a matching <answer>: {missing}")

    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["query", "responses"])
        for i in ids:
            writer.writerow([questions[i], answers[i]])

    print(f"Wrote {len(ids)} rows to {CSV_PATH}")


if __name__ == "__main__":
    main()
