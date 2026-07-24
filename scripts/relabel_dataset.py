"""Re-derive the classifier columns from saved run artifacts.

Labels are *derived* data, not measurements: primary_label / all_labels /
rationale are whatever `classifier.classify()` says about artifacts that are all
still on disk. So when the classifier is corrected, the existing dataset can be
brought up to date without re-calling any LLM and without re-running Yosys or
OpenSTA -- unlike rerun_sta.py, which actually re-invokes the timing engine.

Only the three label columns are touched. Every measured column (timing, slack,
coverage, cells, durations) is left exactly as recorded.

Usage:
    .venv/bin/python scripts/relabel_dataset.py            # dry run, prints diff
    .venv/bin/python scripts/relabel_dataset.py --apply    # writes, after backup
"""
from __future__ import annotations
import argparse
import csv
import shutil
from collections import Counter
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline import config, classifier              # noqa: E402
from pipeline.dataset import FIELDS                  # noqa: E402
from pipeline.orchestrator import expected_coverage  # noqa: E402

REFERENCE = "reference"


def _read(p: Path) -> str:
    return p.read_text() if p.exists() else ""


def _as_bool(v) -> bool:
    return str(v).strip().lower() == "true"


def _as_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _attempt_dir(row: dict) -> Path:
    """The attempt whose outcome this row records (last correction, if any)."""
    run_dir = Path(row["run_dir"])
    n = _as_int(row.get("correction_attempts"))
    if n > 0:
        d = run_dir / f"correction_{n}"
        if (d / "generated.sdc").exists():
            return d
    return run_dir


def reclassify(row: dict) -> dict | None:
    """Return the row with refreshed label columns, or None if artifacts are gone."""
    d = _attempt_dir(row)
    sdc_path = d / "generated.sdc"
    if not sdc_path.exists():
        return None

    sdc = _read(sdc_path)
    lines = [l.strip() for l in sdc.splitlines() if l.strip()]
    is_ref = row.get("llm_model") == REFERENCE
    raw = sdc if is_ref else (_read(d / "llm_raw.txt") or sdc)

    # Coverage flags are not stored per-direction, so re-derive them from the
    # design's ports plus the recorded per-group path counts.
    verilog = _read(config.DESIGNS_DIR / f"{row['design']}.v")
    exp_in, exp_out = expected_coverage(verilog) if verilog else (False, False)
    analyzed = _as_bool(row.get("sta_ok")) and not _as_bool(row.get("no_paths"))
    missing_in = analyzed and exp_in and _as_int(row.get("n_in2reg")) == 0
    missing_out = analyzed and exp_out and _as_int(row.get("n_reg2out")) == 0

    tm = row.get("timing_met")
    timing_met = None if tm in ("", None) else _as_bool(tm)
    try:
        wns = float(row.get("wns_ns")) if row.get("wns_ns") not in ("", None) else None
    except ValueError:
        wns = None

    cls = classifier.classify(
        extracted_lines=lines,
        cleaned_sdc=sdc,
        raw_sdc=raw,
        synthesis_ok=_as_bool(row.get("synthesis_ok")),
        synthesis_log=_read(d / "yosys.log"),
        sta_ok=_as_bool(row.get("sta_ok")),
        sta_log=_read(d / "sta.log"),
        timing_met=timing_met,
        wns_ns=wns,
        no_paths=_as_bool(row.get("no_paths")),
        coverage_complete=_as_bool(row.get("coverage_complete")),
        missing_in2reg=missing_in,
        missing_reg2out=missing_out,
    )
    row = dict(row)
    row["primary_label"] = cls.primary
    row["all_labels"] = "|".join(cls.labels)
    row["rationale"] = "; ".join(cls.rationale)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=config.DATASET_CSV)
    ap.add_argument("--apply", action="store_true",
                    help="Write the result (a .prerelabel.bak backup is made first). "
                         "Without this the script only reports what would change.")
    args = ap.parse_args()

    if not args.dataset.exists():
        raise SystemExit(f"No dataset at {args.dataset}")
    rows = list(csv.DictReader(args.dataset.open(newline="")))

    updated, moves, skipped, changed_passing = [], Counter(), 0, 0
    for r in rows:
        new = reclassify(r)
        if new is None:
            skipped += 1
            updated.append(r)
            continue
        if new["primary_label"] != r.get("primary_label"):
            moves[f"{r.get('primary_label')} -> {new['primary_label']}"] += 1
            if r.get("final_status") == "ok":
                changed_passing += 1
        updated.append(new)

    total = sum(moves.values())
    print(f"rows: {len(rows)}   artifacts missing (left as-is): {skipped}")
    print(f"labels changed: {total}")
    for k, n in moves.most_common():
        print(f"  {n:4}  {k}")
    # A run that passed every tool check should never change label; if it does,
    # the classifier has started flagging something that demonstrably works.
    print(f"\n  of which rows whose final_status is ok: {changed_passing}"
          f"{'   <-- INVESTIGATE' if changed_passing else '  (expected: 0)'}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write these labels.")
        return 0

    backup = args.dataset.with_suffix(".csv.prerelabel.bak")
    shutil.copy(args.dataset, backup)
    print(f"\nBacked up -> {backup}")
    with args.dataset.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in updated:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    print(f"Rewrote {len(updated)} rows in {args.dataset}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
