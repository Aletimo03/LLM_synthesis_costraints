"""Append-only CSV dataset of pipeline runs."""
from __future__ import annotations
import csv
from pathlib import Path
from . import config


FIELDS = [
    "run_id",
    "timestamp",
    "design",
    "top",
    "llm_model",
    "seed",
    "clock_period_ns",
    "prompt_version",
    "correction_attempts",
    "initial_status",      # final_status of the first attempt (before correction)
    "corrected",          # correction turned a non-ok first attempt into ok
    "correction_path",    # per-level verdicts, e.g. "sta_failed>sta_failed>ok"
    "best_status",        # best verdict reached at any level (final may be worse)
    "final_status",
    "primary_label",
    "all_labels",
    "n_extracted_lines",
    "has_create_clock",
    "n_input_delays",
    "n_output_delays",
    "synthesis_ok",
    "llm_duration_s",        # first-shot generation time
    "llm_correction_s",      # correction-retry time (0 if the retry never fired)
    "synthesis_duration_s",
    "cells_total",
    "chip_area",
    "sta_ok",
    "sta_duration_s",
    "wns_ns",
    "tns_ns",
    "min_slack_ns",
    "timing_met",
    "no_paths",
    "setup_violations",
    "n_paths_total",
    "n_in2reg",
    "n_reg2out",
    "n_reg2reg",
    "n_in2out",
    "n_async",
    "wns_in2reg",
    "wns_reg2out",
    "wns_reg2reg",
    "coverage_complete",
    "rationale",
    "run_dir",
]


def _migrate_header(csv_path: Path) -> None:
    """Rewrite an existing dataset whose header predates a FIELDS change.

    Normally a no-op: it returns immediately once the header already matches, so
    it does nothing at all unless FIELDS has changed since the file was written.
    It is kept because the failure it prevents is silent rather than loud --
    appending rows with new columns under an old header does not raise, it just
    shifts every value one position, and the corruption may only surface hours
    into a sweep. On a mismatch the file is rewritten once with the current
    header and the missing columns backfilled empty; existing rows survive.
    """
    if not csv_path.exists():
        return
    with csv_path.open(newline="") as f:
        try:
            header = next(csv.reader(f))
        except StopIteration:
            return
    if header == FIELDS:
        return
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def append_row(row: dict, csv_path: Path = config.DATASET_CSV) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    _migrate_header(csv_path)
    is_new = not csv_path.exists()
    # Coerce missing keys to empty
    full = {f: row.get(f, "") for f in FIELDS}
    with csv_path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            w.writeheader()
        w.writerow(full)
