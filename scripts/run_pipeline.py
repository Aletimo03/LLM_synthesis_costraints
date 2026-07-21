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
    ap.add_argument("--max-corrections", type=int, default=None,
                    help="Correction-ladder depth (default: config value; "
                         "1 = error only, 2 = + object dictionary).")
    ap.add_argument("--model", default=config.LLM_MODEL)
    ap.add_argument("--compare-models", action="store_true",
                    help="Run every model in config.LLM_MODELS (env LLM_MODELS, "
                         "comma-separated) on each design/seed/prompt so models "
                         "can be compared side by side.")
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

    if args.no_correction:
        max_corr = 0
    elif args.max_corrections is not None:
        max_corr = max(0, args.max_corrections)
    else:
        max_corr = config.MAX_CORRECTION_ATTEMPTS

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

    # Which models to run. Reference runs are model-independent.
    models = config.LLM_MODELS if args.compare_models else [args.model]

    def _do(design, seed, pv, reference, model):
        tag = "reference" if reference else pv
        # Per-design period override (e.g. mult_mcp needs a tight period for the
        # multicycle exception to matter); falls back to the CLI --period.
        period = config.DESIGN_PERIODS.get(design.stem, args.period)
        # Correction is spent only on the prompt versions where a failure means a
        # real capability gap (config.CORRECTION_PROMPT_VERSIONS); other versions
        # still run, they just do not get a retry.
        corr_budget = max_corr if pv in config.CORRECTION_PROMPT_VERSIONS else 0
        print(f"\n=== {design.name}  seed={seed}  prompt={tag}  "
              f"model={'-' if reference else model}  period={period}ns"
              f"{'' if corr_budget else '  (no correction)'} ===")
        row = orchestrator.run_one(
            design_path=design,
            seed=seed,
            target_period_ns=period,
            max_corrections=corr_budget,
            use_reference_sdc=reference,
            prompt_version=pv,
            model=model,
        )
        # Show the full ladder path when the correction loop fired.
        status = row["final_status"]
        if row.get("correction_attempts"):
            status = row.get("correction_path") or f"{row.get('initial_status', '?')}>{status}"
            if row.get("corrected"):
                status += "  [FIXED@level{}]".format(row["correction_attempts"])
        print(f"  status={status}  primary={row['primary_label']}")
        print(f"  coverage_complete={row.get('coverage_complete','')}  "
              f"in2reg={row.get('n_in2reg','')} reg2out={row.get('n_reg2out','')} "
              f"reg2reg={row.get('n_reg2reg','')}")
        print(f"  cells={row['cells_total']}  area={row.get('chip_area','')}  "
              f"WNS={row['wns_ns']} ns")
        print(f"  llm_time={row.get('llm_duration_s','')}s  "
              f"corrections={row['correction_attempts']}  run_dir={row['run_dir']}")

    for model in models:
        for design in designs:
            # Reference once per design (updated baseline) when requested and not
            # a pure reference invocation. Model-independent, so first model only.
            if args.with_reference and not args.reference and model == models[0]:
                _do(design, None, "v1_base", reference=True, model=model)
            for seed in args.seeds:
                for pv in prompt_versions:
                    _do(design, seed, pv, reference=args.reference, model=model)

    print(f"\nDataset: {config.DATASET_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
