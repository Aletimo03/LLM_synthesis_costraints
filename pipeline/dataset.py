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
    "final_status",
    "primary_label",
    "all_labels",
    "n_extracted_lines",
    "has_create_clock",
    "n_input_delays",
    "n_output_delays",
    "synthesis_ok",
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
    "rationale",
    "run_dir",
]


def append_row(row: dict, csv_path: Path = config.DATASET_CSV) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not csv_path.exists()
    # Coerce missing keys to empty
    full = {f: row.get(f, "") for f in FIELDS}
    with csv_path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            w.writeheader()
        w.writerow(full)
