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

from pipeline import orchestrator, config, llm  # noqa: E402


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
    ap.add_argument("--prompt-version", default="v1_base",
                    choices=sorted(llm.PROMPT_FILES),
                    help="Which prompt template to use for LLM generation.")
    ap.add_argument("--compare-prompts", action="store_true",
                    help="Run every prompt version (v1_base and v2_base) on each "
                         "design/seed so results can be compared side by side.")
    ap.add_argument("--with-reference", action="store_true",
                    help="Also run the reference (control) SDC once per design, so a "
                         "single invocation rebuilds references + LLM rows together.")
    ap.add_argument("--fresh", action="store_true",
                    help="Drop the existing dataset.csv before this run so the new "
                         "rows fully replace it (references included). Without this, "
                         "rows are appended and old runs accumulate.")
    args = ap.parse_args()

    if args.all:
        designs = sorted(p for p in config.DESIGNS_DIR.glob("*.v") if p.is_file())
    elif args.design:
        designs = [args.design]
    else:
        ap.error("Provide --design <path> or --all.")

    max_corr = 0 if args.no_correction else config.MAX_CORRECTION_ATTEMPTS

    # Start from a clean dataset if requested (so the whole invocation forms it).
    if args.fresh and config.DATASET_CSV.exists():
        config.DATASET_CSV.unlink()
        print(f"--fresh: dropped existing {config.DATASET_CSV.name}; "
              f"this run will fully replace it.")

    # Which prompt versions to run. Reference mode ignores prompts entirely.
    if args.reference:
        prompt_versions = ["v1_base"]  # label is overridden to "reference" inside run_one
    elif args.compare_prompts:
        prompt_versions = sorted(llm.PROMPT_FILES)
    else:
        prompt_versions = [args.prompt_version]

    def _do(design, seed, pv, reference):
        tag = "reference" if reference else pv
        print(f"\n=== {design.name}  seed={seed}  prompt={tag}  reference={reference} ===")
        row = orchestrator.run_one(
            design_path=design,
            seed=seed,
            target_period_ns=args.period,
            max_corrections=max_corr,
            use_reference_sdc=reference,
            prompt_version=pv,
            model=args.model,
        )
        print(f"  status={row['final_status']:>20}  primary={row['primary_label']}")
        print(f"  coverage_complete={row.get('coverage_complete','')}  "
              f"in2reg={row.get('n_in2reg','')} reg2out={row.get('n_reg2out','')} "
              f"reg2reg={row.get('n_reg2reg','')}")
        print(f"  cells={row['cells_total']}  area={row.get('chip_area','')}  "
              f"WNS={row['wns_ns']} ns")
        print(f"  corrections={row['correction_attempts']}  run_dir={row['run_dir']}")

    for design in designs:
        # Reference once per design (updated baseline) when requested and not a
        # pure reference invocation.
        if args.with_reference and not args.reference:
            _do(design, None, "v1_base", reference=True)
        for seed in args.seeds:
            for pv in prompt_versions:
                _do(design, seed, pv, reference=args.reference)

    print(f"\nDataset: {config.DATASET_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
