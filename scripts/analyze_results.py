"""Summarize the experiment dataset, comparing prompt versions side by side.

Every table is organized around the prompt version (v1_base vs v2_base) so it
is easy to tell whether an outcome is driven by the *prompt* or by the *design*.
Reference (control) runs are shown alongside as the QoR baseline.

Usage:
    .venv/bin/python scripts/analyze_results.py
    .venv/bin/python scripts/analyze_results.py --model qwen3:8b
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

REFERENCE = "reference"

# Final-status values in a fixed, meaningful order (best -> worst).
STATUS_ORDER = [
    "ok",
    "partial_coverage",
    "timing_violation",
    "ineffective_no_paths",
    "sta_failed",
    "synth_failed",
]

# (label, csv column) for the three timing path groups that carry QoR meaning.
GROUPS = [
    ("in2reg", "wns_in2reg"),
    ("reg2out", "wns_reg2out"),
    ("reg2reg", "wns_reg2reg"),
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Dataset not found: {path}")
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(part: int, total: int) -> str:
    return "n/a" if total == 0 else f"{part / total * 100:.0f}%"


def _is_true(value) -> bool:
    return str(value).strip().lower() == "true"


def _slack(value) -> str:
    """Format a worst-slack cell: a number, or '—' when the group was never timed."""
    f = _to_float(value)
    return "—" if f is None else f"{f:+.2f}"


def _prompt_versions(llm_rows: list[dict]) -> list[str]:
    return sorted({r.get("prompt_version", "") for r in llm_rows if r.get("prompt_version")})


def _refs_by_design(rows: list[dict]) -> dict[str, dict]:
    return {r["design"]: r for r in rows if r.get("llm_model") == REFERENCE}


def _hr(title: str) -> None:
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #
def _table_summary(refs, by_version: dict[str, list[dict]], total: int) -> None:
    _hr("Dataset Summary")
    print(f"  {REFERENCE:<12} {len(refs):>3}")
    for v, rows in by_version.items():
        print(f"  {v:<12} {len(rows):>3}")
    print(f"  {'total':<12} {total:>3}")


def _table_status(refs, by_version: dict[str, list[dict]]) -> None:
    """Final-status counts: reference + one column per prompt version."""
    _hr("Final Status  (counts; one column per prompt version)")
    versions = list(by_version)
    cols = [REFERENCE] + versions
    ref_counts = Counter(r["final_status"] for r in refs)
    ver_counts = {v: Counter(r["final_status"] for r in rows) for v, rows in by_version.items()}

    header = f"  {'status':<22}" + "".join(f"{c:>12}" for c in cols)
    print(header)
    for status in STATUS_ORDER:
        ref_n = ref_counts.get(status, 0)
        cells = [f"{ref_n if ref_n else '·':>12}"]
        for v in versions:
            n = ver_counts[v].get(status, 0)
            cells.append(f"{n if n else '·':>12}")
        print(f"  {status:<22}" + "".join(cells))

    # headline success rate per version (ok with full coverage)
    print(f"\n  {'success rate (ok)':<22}" +
          f"{_pct(ref_counts.get('ok', 0), len(refs)):>12}" +
          "".join(f"{_pct(ver_counts[v].get('ok', 0), len(by_version[v])):>12}" for v in versions))


def _table_per_design(by_version: dict[str, list[dict]]) -> None:
    """For each design, show each prompt version's outcomes across seeds.

    Lets you separate design-driven failures (bad on every prompt) from
    prompt-driven failures (fixed by switching prompt version).
    """
    _hr("Per-Design Outcomes  (status counts across seeds, per prompt)")
    designs = sorted({r["design"] for rows in by_version.values() for r in rows})
    print(f"  {'design':<11}{'prompt':<11}outcomes")
    for design in designs:
        for v, rows in by_version.items():
            dr = [r for r in rows if r["design"] == design]
            if not dr:
                continue
            counts = Counter(r["final_status"] for r in dr)
            summary = ", ".join(f"{s}={n}" for s, n in
                                sorted(counts.items(), key=lambda kv: STATUS_ORDER.index(kv[0])
                                       if kv[0] in STATUS_ORDER else 99))
            print(f"  {design:<11}{v:<11}{summary}")
        print()


def _table_coverage(by_version: dict[str, list[dict]]) -> None:
    """Coverage and timing flags, per prompt version."""
    _hr("Coverage & Timing Flags  (share of runs, per prompt)")
    versions = list(by_version)
    print(f"  {'metric':<28}" + "".join(f"{v:>12}" for v in versions))

    def share(pred) -> list[str]:
        out = []
        for v in versions:
            rows = by_version[v]
            out.append(f"{_pct(sum(1 for r in rows if pred(r)), len(rows)):>12}")
        return out

    print(f"  {'full coverage':<28}" + "".join(share(lambda r: _is_true(r.get("coverage_complete")))))
    print(f"  {'timed any in2reg':<28}" + "".join(share(lambda r: _to_float(r.get("n_in2reg")) not in (None, 0))))
    print(f"  {'timed any reg2out':<28}" + "".join(share(lambda r: _to_float(r.get("n_reg2out")) not in (None, 0))))
    print(f"  {'timing met':<28}" + "".join(share(lambda r: _is_true(r.get("timing_met")))))
    print(f"  {'no paths (ineffective)':<28}" + "".join(share(lambda r: _is_true(r.get("no_paths")))))


def _table_slack(refs, by_version: dict[str, list[dict]]) -> None:
    """Per-group worst slack: reference vs each prompt version, per design.

    Worst (minimum) slack across seeds is shown — '—' means the group was never
    analyzed by that prompt (a coverage gap, not a passing result).
    """
    _hr("Per-Group Worst Slack  (worst across seeds; '—' = group never timed)")
    versions = list(by_version)
    cols = [REFERENCE] + versions
    designs = sorted({r["design"] for rows in by_version.values() for r in rows})

    def worst(rows, col) -> str:
        vals = [_to_float(r.get(col)) for r in rows]
        vals = [x for x in vals if x is not None]
        return f"{min(vals):+.2f}" if vals else "—"

    ref_by_design = {r["design"]: r for r in refs}
    print(f"  {'design':<11}{'group':<9}" + "".join(f"{c:>12}" for c in cols))
    for design in designs:
        ref = ref_by_design.get(design, {})
        for label, col in GROUPS:
            ref_cell = _slack(ref.get(col, "")) if ref else "—"
            ver_cells = [worst([r for r in by_version[v] if r["design"] == design], col)
                         for v in versions]
            # skip groups that no run (ref or llm) ever times for this design
            if ref_cell == "—" and all(c == "—" for c in ver_cells):
                continue
            cells = [f"{ref_cell:>12}"] + [f"{c:>12}" for c in ver_cells]
            print(f"  {design:<11}{label:<9}" + "".join(cells))
        print()


def _table_netlist_invariance(refs, llm_rows: list[dict]) -> None:
    """Sanity check: constraints must NOT change synthesis, so cells/area should
    equal the reference for every run. Confirms the QoR comparison is fair."""
    _hr("Netlist Invariance Check  (cells & area vs reference)")
    ref_by_design = {r["design"]: r for r in refs}
    mismatches = []
    for r in llm_rows:
        ref = ref_by_design.get(r["design"])
        if not ref:
            continue
        if (r.get("cells_total") != ref.get("cells_total")
                or r.get("chip_area") != ref.get("chip_area")):
            mismatches.append(r)
    if not mismatches:
        print(f"  OK — cells & area identical to reference for all {len(llm_rows)} runs.")
        print("  (constraints do not change synthesis; only the timing analysis differs,")
        print("   so area/cell deltas carry no information and are omitted.)")
    else:
        print(f"  {len(mismatches)} run(s) differ from reference netlist:")
        for r in mismatches:
            ref = ref_by_design[r["design"]]
            print(f"    {r['design']} seed={r.get('seed','')} prompt={r.get('prompt_version','')}: "
                  f"cells {r.get('cells_total')} vs {ref.get('cells_total')}, "
                  f"area {r.get('chip_area')} vs {ref.get('chip_area')}")


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def analyze(rows: list[dict], model: str) -> None:
    refs = [r for r in rows if r.get("llm_model") == REFERENCE]
    llm_rows = [r for r in rows if r.get("llm_model") == model]
    versions = _prompt_versions(llm_rows)
    by_version = {v: [r for r in llm_rows if r.get("prompt_version") == v] for v in versions}

    print(f"Model under test: {model}")
    _table_summary(refs, by_version, len(rows))
    _table_status(refs, by_version)
    _table_per_design(by_version)
    _table_coverage(by_version)
    _table_slack(refs, by_version)
    _table_netlist_invariance(refs, llm_rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=config.DATASET_CSV)
    parser.add_argument("--model", default="qwen3:8b")
    args = parser.parse_args()
    analyze(_read_rows(args.dataset), model=args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
