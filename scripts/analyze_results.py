"""Summarize the current experiment dataset.

This script intentionally uses only columns that already exist in
results/dataset.csv. It focuses on the current comparison: reference SDC runs
versus qwen3:8b LLM-generated runs.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path
import sys


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline import config  # noqa: E402


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Dataset not found: {path}")
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _to_float(value: str) -> float | None:
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{part / total * 100:.1f}%"


def _print_counter(title: str, counter: Counter[str], total: int) -> None:
    print(f"\n{title}")
    if not counter:
        print("  (none)")
        return
    for key, count in counter.most_common():
        label = key if key != "" else "missing"
        print(f"  {label:<24} {count:>3}  {_pct(count, total):>6}")


def _latest_reference_by_design(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    refs: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.get("llm_model") == "reference":
            refs[row["design"]] = row
    return refs


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _format_float(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _print_qor_delta(rows: list[dict[str, str]], reference_by_design: dict[str, dict[str, str]]) -> None:
    area_deltas: list[float] = []
    cell_deltas: list[float] = []
    slack_deltas: list[float] = []

    print("\nQoR Delta vs Reference")
    print("  design       seed  status                 area_delta%  cell_delta  min_slack_delta")

    for row in rows:
        if row.get("llm_model") == "reference":
            continue

        ref = reference_by_design.get(row["design"])
        if ref is None:
            continue

        area = _to_float(row.get("chip_area", ""))
        ref_area = _to_float(ref.get("chip_area", ""))
        cells = _to_float(row.get("cells_total", ""))
        ref_cells = _to_float(ref.get("cells_total", ""))
        slack = _to_float(row.get("min_slack_ns", ""))
        ref_slack = _to_float(ref.get("min_slack_ns", ""))

        area_delta_pct = None
        if area is not None and ref_area not in (None, 0):
            area_delta_pct = (area - ref_area) / ref_area * 100
            area_deltas.append(area_delta_pct)

        cell_delta = None
        if cells is not None and ref_cells is not None:
            cell_delta = cells - ref_cells
            cell_deltas.append(cell_delta)

        slack_delta = None
        if slack is not None and ref_slack is not None:
            slack_delta = slack - ref_slack
            slack_deltas.append(slack_delta)

        print(
            f"  {row['design']:<11} {row.get('seed', ''):<5} "
            f"{row.get('final_status', ''):<22} "
            f"{_format_float(area_delta_pct):>11}  "
            f"{_format_float(cell_delta, digits=0):>10}  "
            f"{_format_float(slack_delta):>15}"
        )

    print("\nQoR Delta Means")
    print(f"  area_delta%       {_format_float(_mean(area_deltas))}")
    print(f"  cell_delta        {_format_float(_mean(cell_deltas), digits=0)}")
    print(f"  min_slack_delta   {_format_float(_mean(slack_deltas))}")


def analyze(rows: list[dict[str, str]], model: str) -> None:
    filtered = [r for r in rows if r.get("llm_model") in {"reference", model}]
    llm_rows = [r for r in filtered if r.get("llm_model") == model]
    ref_rows = [r for r in filtered if r.get("llm_model") == "reference"]

    print("Dataset Summary")
    print(f"  rows total         {len(rows)}")
    print(f"  reference rows     {len(ref_rows)}")
    print(f"  {model} rows       {len(llm_rows)}")

    _print_counter("Reference Final Status", Counter(r["final_status"] for r in ref_rows), len(ref_rows))
    _print_counter(f"{model} Final Status", Counter(r["final_status"] for r in llm_rows), len(llm_rows))
    _print_counter("Primary Labels", Counter(r["primary_label"] for r in llm_rows), len(llm_rows))

    print("\nPer-Design Outcomes")
    by_design: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in llm_rows:
        by_design[row["design"]].append(row)
    for design in sorted(by_design):
        design_rows = by_design[design]
        counts = Counter(r["final_status"] for r in design_rows)
        summary = ", ".join(f"{status}={count}" for status, count in counts.most_common())
        print(f"  {design:<11} {summary}")

    _print_counter("No-Paths Flag", Counter(r["no_paths"] for r in llm_rows), len(llm_rows))
    _print_counter("Timing Met Flag", Counter(r["timing_met"] for r in llm_rows), len(llm_rows))

    reference_by_design = _latest_reference_by_design(ref_rows)
    _print_qor_delta(llm_rows, reference_by_design)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=config.DATASET_CSV)
    parser.add_argument("--model", default="qwen3:8b")
    args = parser.parse_args()

    rows = _read_rows(args.dataset)
    analyze(rows, model=args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
