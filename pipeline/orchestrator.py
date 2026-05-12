"""End-to-end pipeline: LLM → Yosys → OpenSTA → classify → record."""
from __future__ import annotations
import re
import uuid
import datetime
from dataclasses import asdict
from pathlib import Path
from . import config, llm, synthesis, sta, classifier, dataset


_TOP_PAT = re.compile(r"^\s*module\s+(\w+)\s*[\(#]", re.MULTILINE)


def extract_top(verilog_src: str) -> str:
    m = _TOP_PAT.search(verilog_src)
    if not m:
        raise ValueError("Could not find a `module <name>` declaration in the Verilog source.")
    return m.group(1)


def extract_clock_period(sdc: str, fallback: float) -> float:
    for line in sdc.splitlines():
        m = re.search(r"create_clock\b.*?-period\s+([-\d.]+)", line)
        if m:
            try:
                v = float(m.group(1))
                if v > 0:
                    return v
            except ValueError:
                pass
    return fallback


def _count_lines_starting(lines: list[str], prefix: str) -> int:
    return sum(1 for l in lines if l.startswith(prefix))


def _make_run_id(design: str, seed: int | None) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:6]
    seed_part = f"s{seed}" if seed is not None else "noseed"
    return f"{ts}_{design}_{seed_part}_{short}"


def run_one(
    *,
    design_path: Path,
    seed: int | None = None,
    target_period_ns: float = config.DEFAULT_CLOCK_PERIOD_NS,
    max_corrections: int = config.MAX_CORRECTION_ATTEMPTS,
    use_reference_sdc: bool = False,
    prompt_version: str = "v1_base",
    model: str = config.LLM_MODEL,
) -> dict:
    """Run the full pipeline once for one design.

    If use_reference_sdc is True, skip LLM and use designs/reference/<name>.sdc
    as a control baseline for QoR comparison.
    """
    design_name = design_path.stem
    verilog_src = design_path.read_text()
    top = extract_top(verilog_src)

    run_id = _make_run_id(design_name, seed)
    if use_reference_sdc:
        run_id = run_id + "_reference"
    run_dir = config.RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now().isoformat(timespec="seconds")

    # 1. Get SDC (either LLM or reference)
    correction_attempts = 0
    if use_reference_sdc:
        ref_path = config.REFERENCE_SDC_DIR / f"{design_name}.sdc"
        if not ref_path.exists():
            raise FileNotFoundError(f"No reference SDC for {design_name}: {ref_path}")
        cleaned_sdc = ref_path.read_text().strip()
        raw_sdc = cleaned_sdc
        extracted_lines = [l.strip() for l in cleaned_sdc.splitlines() if l.strip()]
    else:
        result = llm.generate(verilog_src, target_period_ns, model=model, seed=seed)
        raw_sdc = result.raw
        cleaned_sdc = result.cleaned
        extracted_lines = result.extracted_lines
        (run_dir / "llm_raw.txt").write_text(raw_sdc)

    (run_dir / "generated.sdc").write_text(cleaned_sdc + "\n")
    sdc_path = run_dir / "generated.sdc"

    # 2. Extract effective clock period from SDC (for ABC target)
    clock_period_ns = extract_clock_period(cleaned_sdc, fallback=target_period_ns)

    # 3. Run synthesis with correction loop on errors
    synth_result = synthesis.run(design_path, top, run_dir, clock_period_ns=clock_period_ns)

    # If failed and LLM mode, try correction
    while (
        not synth_result.ok
        and not use_reference_sdc
        and correction_attempts < max_corrections
    ):
        correction_attempts += 1
        corr_dir = run_dir / f"correction_{correction_attempts}"
        corr_dir.mkdir(exist_ok=True)
        corr = llm.correct(verilog_src, cleaned_sdc, synth_result.log,
                           clock_period_ns=target_period_ns, model=model)
        (corr_dir / "llm_raw.txt").write_text(corr.raw)
        if not corr.extracted_lines:
            break  # correction also failed to produce valid lines
        cleaned_sdc = corr.cleaned
        extracted_lines = corr.extracted_lines
        sdc_path.write_text(cleaned_sdc + "\n")
        clock_period_ns = extract_clock_period(cleaned_sdc, fallback=target_period_ns)
        synth_result = synthesis.run(design_path, top, corr_dir, clock_period_ns=clock_period_ns)

    # 4. STA (only if synth produced a netlist)
    if synth_result.ok and synth_result.netlist_path is not None:
        sta_result = sta.run(synth_result.netlist_path, sdc_path, top, run_dir)
    else:
        sta_result = sta.STAResult(
            ok=False, returncode=-1, log="(skipped: synthesis failed)",
            wns_ns=None, tns_ns=None, min_slack_ns=None,
            timing_met=None, setup_violations=0,
            errors=["synthesis_failed"], duration_s=0.0,
        )

    # 5. Classify
    cls = classifier.classify(
        extracted_lines=extracted_lines,
        cleaned_sdc=cleaned_sdc,
        raw_sdc=raw_sdc,
        synthesis_ok=synth_result.ok,
        synthesis_log=synth_result.log,
        sta_ok=sta_result.ok,
        sta_log=sta_result.log,
        timing_met=sta_result.timing_met,
        wns_ns=sta_result.wns_ns,
        no_paths=sta_result.no_paths,
    )

    # 6. Final status
    if synth_result.ok and sta_result.ok and sta_result.no_paths:
        final_status = "ineffective_no_paths"
    elif synth_result.ok and sta_result.ok and sta_result.timing_met:
        final_status = "ok"
    elif synth_result.ok and sta_result.ok and sta_result.timing_met is False:
        final_status = "timing_violation"
    elif synth_result.ok:
        final_status = "sta_failed"
    else:
        final_status = "synth_failed"

    # 7. Record
    row = {
        "run_id": run_id,
        "timestamp": timestamp,
        "design": design_name,
        "top": top,
        "llm_model": "reference" if use_reference_sdc else model,
        "seed": seed if seed is not None else "",
        "clock_period_ns": clock_period_ns,
        "prompt_version": "reference" if use_reference_sdc else prompt_version,
        "correction_attempts": correction_attempts,
        "final_status": final_status,
        "primary_label": cls.primary,
        "all_labels": "|".join(cls.labels),
        "n_extracted_lines": len(extracted_lines),
        "has_create_clock": any(l.startswith("create_clock") for l in extracted_lines),
        "n_input_delays": _count_lines_starting(extracted_lines, "set_input_delay"),
        "n_output_delays": _count_lines_starting(extracted_lines, "set_output_delay"),
        "synthesis_ok": synth_result.ok,
        "synthesis_duration_s": round(synth_result.duration_s, 3),
        "cells_total": synth_result.cells if synth_result.cells is not None else "",
        "chip_area": synth_result.chip_area if synth_result.chip_area is not None else "",
        "sta_ok": sta_result.ok,
        "sta_duration_s": round(sta_result.duration_s, 3),
        "wns_ns": sta_result.wns_ns if sta_result.wns_ns is not None else "",
        "tns_ns": sta_result.tns_ns if sta_result.tns_ns is not None else "",
        "min_slack_ns": sta_result.min_slack_ns if sta_result.min_slack_ns is not None else "",
        "no_paths": sta_result.no_paths,
        "timing_met": sta_result.timing_met if sta_result.timing_met is not None else "",
        "setup_violations": sta_result.setup_violations,
        "rationale": "; ".join(cls.rationale),
        "run_dir": str(run_dir),
    }
    dataset.append_row(row)
    return row
