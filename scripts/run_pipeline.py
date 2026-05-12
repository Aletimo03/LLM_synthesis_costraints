"""CLI entry point for the constraint-validation pipeline.

Examples:
    # Single LLM run on simple.v
    python scripts/run_pipeline.py --design designs/simple.v

    # Reference (control) run for QoR comparison
    python scripts/run_pipeline.py --design designs/simple.v --reference

    # Multi-seed batch
    python scripts/run_pipeline.py --design designs/simple.v --seeds 1 2 3 4 5

    # All designs, multiple seeds
    python scripts/run_pipeline.py --all --seeds 1 2 3
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

# make project root importable so `from pipeline import ...` works
# regardless of where this script is invoked from.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline import orchestrator, config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--design", type=Path, help="Path to a Verilog design.")
    ap.add_argument("--all", action="store_true", help="Run all designs in designs/.")
    ap.add_argument("--seeds", type=int, nargs="+", default=[None],
                    help="LLM seeds to sample with (one run per seed). Use one value for a single run.")
    ap.add_argument("--period", type=float, default=config.DEFAULT_CLOCK_PERIOD_NS,
                    help="Target clock period in ns (passed to the LLM prompt).")
    ap.add_argument("--reference", action="store_true",
                    help="Use designs/reference/<name>.sdc instead of the LLM (control run).")
    ap.add_argument("--no-correction", action="store_true",
                    help="Disable the correction/retry loop.")
    ap.add_argument("--model", default=config.LLM_MODEL)
    args = ap.parse_args()

    if args.all:
        designs = sorted(p for p in config.DESIGNS_DIR.glob("*.v") if p.is_file())
    elif args.design:
        designs = [args.design]
    else:
        ap.error("Provide --design <path> or --all.")

    max_corr = 0 if args.no_correction else config.MAX_CORRECTION_ATTEMPTS

    for design in designs:
        for seed in args.seeds:
            print(f"\n=== {design.name}  seed={seed}  reference={args.reference} ===")
            row = orchestrator.run_one(
                design_path=design,
                seed=seed,
                target_period_ns=args.period,
                max_corrections=max_corr,
                use_reference_sdc=args.reference,
                model=args.model,
            )
            print(f"  status={row['final_status']:>20}  primary={row['primary_label']}")
            print(f"  cells={row['cells_total']}  area={row.get('chip_area','')}  "
                  f"min_slack={row.get('min_slack_ns','')} ns  WNS={row['wns_ns']} ns")
            print(f"  corrections={row['correction_attempts']}  run_dir={row['run_dir']}")

    print(f"\nDataset: {config.DATASET_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
