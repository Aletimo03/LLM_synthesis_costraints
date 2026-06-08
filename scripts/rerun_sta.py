"""Replay OpenSTA on every existing run and rebuild the dataset.

Unlike backfill_paths.py (which only re-parses the old sta.log), this script
re-runs OpenSTA on each run's saved netlist + generated.sdc using the current
TCL — the one with independent per-group `-from`/`-to` queries. That regenerates
sta.log and coverage.txt and recomputes every timing/coverage column, fixing the
coverage false positive where a constrained-but-non-critical path group was
masked in the old single-report output.

No LLM or Yosys calls are made; synthesis results (cells/area) are taken from the
existing dataset row. Requires a working OpenSTA (same as a normal run).

Usage:
    .venv/bin/python scripts/rerun_sta.py
"""
from __future__ import annotations
import csv
import shutil
from pathlib import Path

from pipeline import config, sta, classifier
from pipeline.dataset import FIELDS
from pipeline.orchestrator import compute_coverage, compute_final_status


def _read(p: Path) -> str:
    return p.read_text() if p.exists() else ""


def _as_bool(v) -> bool:
    return str(v).strip().lower() == "true"


def _find_netlist(run_dir: Path) -> Path | None:
    for name in ("netlist.v",):
        p = run_dir / name
        if p.exists():
            return p
    vs = [p for p in run_dir.glob("*.v")]
    return vs[0] if vs else None


def rerun_row(row: dict) -> dict:
    run_dir = Path(row["run_dir"])
    sdc = run_dir / "generated.sdc"
    netlist = _find_netlist(run_dir)
    synthesis_ok = _as_bool(row.get("synthesis_ok", ""))

    if not synthesis_ok or netlist is None or not sdc.exists():
        # Nothing to re-time (synth failed or artifacts missing) — leave as is.
        return row

    res = sta.run(netlist, sdc, row["top"], run_dir)  # regenerates sta.log + coverage.txt

    verilog_src = _read(config.DESIGNS_DIR / f"{row['design']}.v")
    cov = compute_coverage(verilog_src, res)

    final_status = compute_final_status(
        synth_ok=synthesis_ok,
        sta_ok=res.ok,
        no_paths=res.no_paths,
        timing_met=res.timing_met,
        coverage_complete=cov["coverage_complete"],
    )

    generated_sdc = _read(sdc)
    extracted_lines = [l.strip() for l in generated_sdc.splitlines() if l.strip()]
    is_reference = row.get("llm_model", "") == REFERENCE
    raw_sdc = generated_sdc if is_reference else (_read(run_dir / "llm_raw.txt") or generated_sdc)

    cls = classifier.classify(
        extracted_lines=extracted_lines,
        cleaned_sdc=generated_sdc,
        raw_sdc=raw_sdc,
        synthesis_ok=synthesis_ok,
        synthesis_log=_read(run_dir / "yosys.log"),
        sta_ok=res.ok,
        sta_log=res.log,
        timing_met=res.timing_met,
        wns_ns=res.wns_ns,
        no_paths=res.no_paths,
        coverage_complete=cov["coverage_complete"],
        missing_in2reg=cov["missing_in2reg"],
        missing_reg2out=cov["missing_reg2out"],
    )

    def f(x):
        return x if x is not None else ""

    row.update({
        "final_status": final_status,
        "primary_label": cls.primary,
        "all_labels": "|".join(cls.labels),
        "rationale": "; ".join(cls.rationale),
        "sta_ok": res.ok,
        "sta_duration_s": round(res.duration_s, 3),
        "wns_ns": f(res.wns_ns),
        "tns_ns": f(res.tns_ns),
        "min_slack_ns": f(res.min_slack_ns),
        "timing_met": f(res.timing_met),
        "no_paths": res.no_paths,
        "setup_violations": res.setup_violations,
        "n_paths_total": res.n_paths_total,
        "n_in2reg": res.n_in2reg,
        "n_reg2out": res.n_reg2out,
        "n_reg2reg": res.n_reg2reg,
        "n_in2out": res.n_in2out,
        "n_async": res.n_async,
        "wns_in2reg": f(res.wns_in2reg),
        "wns_reg2out": f(res.wns_reg2out),
        "wns_reg2reg": f(res.wns_reg2reg),
        "coverage_complete": cov["coverage_complete"],
    })
    return row


REFERENCE = "reference"


def main() -> None:
    csv_path = config.DATASET_CSV
    if not csv_path.exists():
        raise SystemExit(f"No dataset at {csv_path}")

    with csv_path.open() as f:
        rows = list(csv.DictReader(f))

    backup = csv_path.with_suffix(".csv.prererun.bak")
    shutil.copy(csv_path, backup)
    print(f"Backed up original -> {backup}")

    updated = []
    for i, r in enumerate(rows, 1):
        row = rerun_row(dict(r))
        updated.append(row)
        print(f"  [{i:>2}/{len(rows)}] {row['design']:<10} "
              f"{row.get('prompt_version',''):<10} seed={row.get('seed',''):<3} "
              f"-> {row['final_status']}")

    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in updated:
            w.writerow({k: r.get(k, "") for k in FIELDS})

    print(f"\nRewrote {len(updated)} rows in {csv_path}")
    from collections import Counter
    dist = Counter(r["final_status"] for r in updated)
    print("\nfinal_status distribution after re-STA:")
    for status, n in sorted(dist.items(), key=lambda kv: -kv[1]):
        print(f"  {status:24s} {n}")


if __name__ == "__main__":
    main()
