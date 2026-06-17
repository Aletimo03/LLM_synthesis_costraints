"""Summarize the experiment dataset, comparing prompts and models side by side.

Every table is organized around the prompt version (v1_base vs v2_base) with one
column per model inside each version, so a single table shows both axes at once:
whether an outcome is driven by the *prompt*, the *model*, or the *design*.
Reference (control) runs are shown alongside as the QoR baseline.

Usage:
    .venv/bin/python scripts/analyze_results.py
    .venv/bin/python scripts/analyze_results.py --model qwen3:8b
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
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

# Column geometry for the grouped (version x model) tables.
LABEL_W = 22   # leading label column (incl. the 2-space indent)
CELL_W = 7     # per-model data cell
VSEP = " │"    # vertical separator between prompt-version blocks


def _block(cells: list[str], cell_w: int = CELL_W) -> str:
    """One version's worth of right-aligned model cells, as a single string."""
    return "".join(f"{c:>{cell_w}}" for c in cells)


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


def _abbr(model: str) -> str:
    """Compact column label for a model name (keeps tables narrow)."""
    return (model.replace("granite4.1:", "gr")
                 .replace("gemma4:", "g")
                 .replace("qwen3-coder:", "qc")
                 .replace("qwen3:", "q")
                 .replace("minimax-m3:cloud", "mmx")
                 .replace("kimi-k2.7-code:cloud", "kimi"))


def _hr(title: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'=' * 76}")


def _grouped_header(label: str, versions: list[str], models: list[str],
                    with_ref: bool, cell_w: int = CELL_W) -> None:
    """Two-row header: prompt versions spanning a block of per-model columns."""
    span = cell_w * len(models)
    line1 = " " * LABEL_W + (f"{'':>{cell_w}}" if with_ref else "")
    line1 += VSEP.join(f"{v:^{span}}" for v in versions)
    line2 = f"  {label:<{LABEL_W - 2}}" + (f"{'ref':>{cell_w}}" if with_ref else "")
    line2 += VSEP.join(_block([_abbr(m) for m in models], cell_w) for _ in versions)
    print(line1.rstrip())
    print(line2)


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #
def _table_summary(refs, models, versions, vm_rows, total) -> None:
    _hr("Dataset Summary")
    print(f"  {'total runs':<20}{total:>5}")
    print(f"  {'reference':<20}{len(refs):>5}")
    print(f"  {'models':<20}{len(models):>5}   {', '.join(models)}")
    designs = sorted({r["design"] for rows in vm_rows.values() for r in rows})
    print(f"  {'designs':<20}{len(designs):>5}   {', '.join(designs)}")
    for v in versions:
        n = sum(len(vm_rows[(v, m)]) for m in models)
        print(f"  {v:<20}{n:>5}   ({len(models)} models)")


def _table_status(refs, models, versions, vm_rows) -> None:
    """Final-status counts: reference + one column per (version, model)."""
    _hr("Final Status  (counts; reference + one column per version x model)")
    ref_counts = Counter(r["final_status"] for r in refs)
    vm_counts = {k: Counter(r["final_status"] for r in rows) for k, rows in vm_rows.items()}

    _grouped_header("status", versions, models, with_ref=True)
    for status in STATUS_ORDER:
        ref_n = ref_counts.get(status, 0)
        row = f"  {status:<{LABEL_W - 2}}{(ref_n or '·'):>{CELL_W}}"
        row += VSEP.join(_block([(vm_counts[(v, m)].get(status, 0) or '·') for m in models])
                         for v in versions)
        print(row)

    # headline success rate (ok) per (version, model)
    def ok_rate(v, m):
        rows = vm_rows[(v, m)]
        return _pct(sum(1 for r in rows if r["final_status"] == "ok"), len(rows))
    row = f"\n  {'success rate (ok)':<{LABEL_W - 2}}{_pct(ref_counts.get('ok', 0), len(refs)):>{CELL_W}}"
    row += VSEP.join(_block([ok_rate(v, m) for m in models]) for v in versions)
    print(row)


def _table_coverage(models, versions, vm_rows) -> None:
    """Coverage and timing flags (share of runs), per (version, model)."""
    _hr("Coverage & Timing Flags  (share of runs, per version x model)")
    _grouped_header("metric", versions, models, with_ref=False)

    def line(label, pred) -> None:
        def cell(v, m):
            rows = vm_rows[(v, m)]
            return _pct(sum(1 for r in rows if pred(r)), len(rows))
        row = f"  {label:<{LABEL_W - 2}}"
        row += VSEP.join(_block([cell(v, m) for m in models]) for v in versions)
        print(row)

    line("full coverage", lambda r: _is_true(r.get("coverage_complete")))
    line("timed in2reg", lambda r: _to_float(r.get("n_in2reg")) not in (None, 0))
    line("timed reg2out", lambda r: _to_float(r.get("n_reg2out")) not in (None, 0))
    line("timing met", lambda r: _is_true(r.get("timing_met")))
    line("no paths (ineff.)", lambda r: _is_true(r.get("no_paths")))


def _table_per_design(models, versions, vm_rows) -> None:
    """For each design x model, show the outcome mix under each prompt version.

    Lets you separate design-driven failures (bad on every prompt/model) from
    prompt- or model-driven failures.
    """
    _hr("Per-Design Outcomes  (status counts across seeds; one row per design x model)")
    designs = sorted({r["design"] for rows in vm_rows.values() for r in rows})

    def outcomes(rows) -> str:
        counts = Counter(r["final_status"] for r in rows)
        return ", ".join(f"{s}={n}" for s, n in sorted(
            counts.items(),
            key=lambda kv: STATUS_ORDER.index(kv[0]) if kv[0] in STATUS_ORDER else 99))

    vlabels = f" {VSEP} ".join(f"{v}: <outcomes>" for v in versions)
    print(f"  {'design':<11}{'model':<8}{vlabels}")
    for design in designs:
        for m in models:
            cells = []
            for v in versions:
                rows = [r for r in vm_rows[(v, m)] if r["design"] == design]
                cells.append(f"{v}: {outcomes(rows) or '-'}" if rows else f"{v}: -")
            print(f"  {design:<11}{_abbr(m):<8}" + f" {VSEP} ".join(cells))
        print()


def _table_slack(refs, models, versions, vm_rows) -> None:
    """Per-group slack range: reference + each (version, model), per design.

    Shows the min/max slack across seeds. A single number means every timed seed
    agreed (deterministic delay choice); a 'min/max' pair means the model's delay
    choice — and thus the slack — varied across seeds. '—' means the group was
    never analyzed by any seed (a coverage gap, not a passing result). Only seeds
    that actually timed the group contribute; '—' is never treated as a slack
    value, since "no path timed" is a coverage outcome, not a timing number.
    """
    _hr("Per-Group Slack Range  (min/max across seeds; '—' = group never timed)")
    SCW = 14   # wider cell: a 'min/max' pair needs room (e.g. +3.97/+4.57)
    designs = sorted({r["design"] for rows in vm_rows.values() for r in rows})
    ref_by_design = {r["design"]: r for r in refs}

    def minmax(rows, col) -> str:
        vals = [_to_float(r.get(col)) for r in rows]
        vals = [x for x in vals if x is not None]
        if not vals:
            return "—"
        lo, hi = min(vals), max(vals)
        if abs(hi - lo) < 0.005:          # seeds agree → one number
            return f"{lo:+.2f}"
        return f"{lo:+.2f}/{hi:+.2f}"      # seeds varied → worst/best pair

    _grouped_header("design / group", versions, models, with_ref=True, cell_w=SCW)
    for design in designs:
        ref = ref_by_design.get(design, {})
        for label, col in GROUPS:
            ref_cell = _slack(ref.get(col, "")) if ref else "—"
            blocks = [[minmax([r for r in vm_rows[(v, m)] if r["design"] == design], col)
                       for m in models] for v in versions]
            # skip groups that no run (ref or llm) ever times for this design
            if ref_cell == "—" and all(c == "—" for blk in blocks for c in blk):
                continue
            tag = f"{design}/{label}"
            row = f"  {tag:<{LABEL_W - 2}}{ref_cell:>{SCW}}"
            row += VSEP.join(_block(blk, SCW) for blk in blocks)
            print(row)
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
            print(f"    {r['design']} seed={r.get('seed','')} model={_abbr(r.get('llm_model',''))} "
                  f"prompt={r.get('prompt_version','')}: "
                  f"cells {r.get('cells_total')} vs {ref.get('cells_total')}, "
                  f"area {r.get('chip_area')} vs {ref.get('chip_area')}")


def _table_models(models, versions, vm_rows) -> None:
    """Cross-model comparison: effectiveness and speed, per model x prompt."""
    _hr("Model Comparison  (effectiveness & speed, per model x prompt)")
    print(f"  {'model':<16}{'prompt':<11}{'runs':>6}{'ok':>6}{'ok %':>8}"
          f"{'full cov %':>12}{'avg llm s':>11}{'avg total s':>12}")
    for model in models:
        for v in versions:
            vr = vm_rows[(v, model)]
            if not vr:
                continue
            n_ok = sum(1 for r in vr if r["final_status"] == "ok")
            n_cov = sum(1 for r in vr if _is_true(r.get("coverage_complete")))
            llm_t = [x for x in (_to_float(r.get("llm_duration_s")) for r in vr) if x is not None]
            tot_t = []
            for r in vr:
                parts = [_to_float(r.get(c)) for c in
                         ("llm_duration_s", "synthesis_duration_s", "sta_duration_s")]
                if any(p is not None for p in parts):
                    tot_t.append(sum(p or 0.0 for p in parts))
            avg_llm = f"{sum(llm_t)/len(llm_t):.1f}" if llm_t else "—"
            avg_tot = f"{sum(tot_t)/len(tot_t):.1f}" if tot_t else "—"
            print(f"  {model:<16}{v:<11}{len(vr):>6}{n_ok:>6}"
                  f"{_pct(n_ok, len(vr)):>8}{_pct(n_cov, len(vr)):>12}"
                  f"{avg_llm:>11}{avg_tot:>12}")
        print()
    print("  avg llm s   = mean LLM generation time per run (incl. corrections)")
    print("  avg total s = mean LLM + synthesis + STA time per run")


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def analyze(rows: list[dict], model: str | None) -> None:
    refs = [r for r in rows if r.get("llm_model") == REFERENCE]
    if model:
        models = [model]
    else:
        models = sorted({r["llm_model"] for r in rows if r.get("llm_model") != REFERENCE})
    llm_rows = [r for r in rows if r.get("llm_model") in models]
    versions = sorted({r.get("prompt_version", "") for r in llm_rows if r.get("prompt_version")})

    # One lookup keyed by (version, model); every table indexes into it so the
    # model is just another column rather than a repeated per-model section.
    vm_rows = {(v, m): [r for r in llm_rows
                        if r.get("prompt_version") == v and r.get("llm_model") == m]
               for v in versions for m in models}

    abbr_legend = ", ".join(f"{_abbr(m)}={m}" for m in models)
    print(f"\nModels (column abbreviations): {abbr_legend}")

    _table_summary(refs, models, versions, vm_rows, len(rows))
    _table_status(refs, models, versions, vm_rows)
    _table_coverage(models, versions, vm_rows)
    _table_per_design(models, versions, vm_rows)
    _table_slack(refs, models, versions, vm_rows)
    _table_netlist_invariance(refs, llm_rows)
    if len(models) > 1:
        _table_models(models, versions, vm_rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=config.DATASET_CSV)
    parser.add_argument("--model", default=None,
                        help="Restrict to one model; default analyzes every "
                             "model present in the dataset.")
    args = parser.parse_args()
    analyze(_read_rows(args.dataset), model=args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
