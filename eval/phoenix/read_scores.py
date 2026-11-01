"""Read Phoenix experiment scores for the BIM viewer eval and print a summary.

Pulls every experiment for the dataset, groups by the system-prompt version in
its description, and reports:

  1. Overall mean correctness and scope-pass rates (the headline numbers).
  2. A PER-QUESTION consistency breakdown: for each of the 10 Gold questions,
     how many of the experiment's repetitions passed correctness. This is what
     the mean alone cannot tell you — whether a 90% mean is "the same 1 question
     fails every rep" (stable/reproducible) or "different questions flip across
     reps" (flaky). A question that passes k/N reps is stable at its extremes
     (N/N or 0/N) and flaky in between.

Read-only. Pass --per-question to include the breakdown; default prints only the
summary rows (same as before).

Usage:
    python read_scores.py
    python read_scores.py --per-question
    python read_scores.py --per-question --filter "final consistency matrix"
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict

from phoenix.client import Client

DATASET = "bim-viewer-eval-0"


def _example_query_map(ds) -> dict[str, str]:
    """example_id -> the plain-English question text, for readable output."""
    out: dict[str, str] = {}
    try:
        df = ds.to_dataframe()
    except Exception:
        return out
    for example_id, row in df.iterrows():
        inp = row.get("input") if hasattr(row, "get") else None
        query = ""
        if isinstance(inp, dict):
            query = str(inp.get("query", ""))
        out[str(example_id)] = query
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--per-question",
        action="store_true",
        help="Also print the per-question pass-rate breakdown across repetitions.",
    )
    parser.add_argument(
        "--filter",
        default="",
        help="Only report experiments whose description contains this substring.",
    )
    args = parser.parse_args()

    client = Client(base_url="http://localhost:6006")
    ds = client.datasets.get_dataset(dataset=DATASET)
    ds_id = ds.id
    query_by_example = _example_query_map(ds)

    experiments = client.experiments.list(dataset_id=ds_id)
    rows = []
    for exp in experiments:
        exp_id = exp.get("id") if isinstance(exp, dict) else exp.id
        desc = (exp.get("description") if isinstance(exp, dict) else exp.description) or ""
        if args.filter and args.filter not in desc:
            continue
        detail = client.experiments.get_experiment(experiment_id=exp_id)

        # Map each task run -> which Gold example it answered. Task runs carry
        # dataset_example_id + repetition_number; eval runs link back by
        # experiment_run_id. We use that chain to attribute each correctness
        # score to a specific question.
        task_runs = detail.get("task_runs", []) or []
        example_of_run: dict[str, str] = {}
        for tr in task_runs:
            run_id = tr.get("id")
            ex_id = tr.get("dataset_example_id")
            if run_id is not None and ex_id is not None:
                example_of_run[str(run_id)] = str(ex_id)

        eval_runs = detail.get("evaluation_runs", []) or []
        buckets: dict[str, list[float]] = defaultdict(list)
        # per (judge_name, example_id) -> list of scores across repetitions
        per_q: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        for run in eval_runs:
            name = getattr(run, "name", None)
            result = getattr(run, "result", None) or {}
            score = result.get("score")
            if name is None or not isinstance(score, (int, float)):
                continue
            buckets[name].append(float(score))
            exp_run_id = getattr(run, "experiment_run_id", None)
            ex_id = example_of_run.get(str(exp_run_id))
            if ex_id is not None:
                per_q[name][ex_id].append(float(score))

        ver = "?"
        m = re.search(r"\((v\d)\)", desc)
        if m:
            ver = m.group(1)

        summary = {
            "version": ver,
            "experiment_id": exp_id,
            "description": desc,
            "n_eval_runs": len(eval_runs),
        }
        for name, scores in buckets.items():
            if scores:
                summary[f"{name}_mean"] = round(sum(scores) / len(scores), 3)
                summary[f"{name}_n"] = len(scores)
        summary["_per_q"] = per_q
        rows.append(summary)

    rows.sort(key=lambda r: (r["version"], r["experiment_id"]))
    for r in rows:
        per_q = r.pop("_per_q", {})
        print(r)
        if args.per_question and "correctness" in per_q:
            cq = per_q["correctness"]
            print("  per-question correctness (passed / reps):")
            for ex_id in sorted(cq, key=lambda e: (-len(cq[e]), e)):
                scores = cq[ex_id]
                passed = sum(1 for s in scores if s >= 1.0)
                n = len(scores)
                flag = "" if passed in (0, n) else "  <-- FLAKY (flips across reps)"
                q = query_by_example.get(ex_id, "")
                q_short = (q[:60] + "…") if len(q) > 61 else q
                print(f"    {passed}/{n}  {q_short}{flag}")
            print()


if __name__ == "__main__":
    main()
