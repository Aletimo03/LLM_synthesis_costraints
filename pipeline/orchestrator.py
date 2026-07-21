"""End-to-end pipeline: LLM → Yosys → OpenSTA → classify → record."""
from __future__ import annotations
import re
import datetime
import time
from dataclasses import asdict
from pathlib import Path
from . import config, llm, synthesis, sta, classifier, dataset


_TOP_PAT = re.compile(r"^\s*module\s+(\w+)\s*[\(#]", re.MULTILINE)
_PORT_PAT = re.compile(r"^\s*(input|output)\b(.*)$")

CLOCK_NAMES = {"clk", "clock", "clk_i", "i_clk"}
# names whose timing shows up as async recovery/removal, not data setup paths
_RESET_PAT = re.compile(r"^(rst|reset|arst|nrst)(_?n)?$|^.*_(rst|reset)(_?n)?$", re.IGNORECASE)


def _is_clock_port(name: str) -> bool:
    """A port is a clock if it is a known clock name or contains clk/clock.

    Generalizes the fixed CLOCK_NAMES set so multi-clock designs (clk_a, clk_b,
    wr_clk, ...) are not mistaken for data inputs in the coverage check.
    """
    n = name.lower()
    return n in CLOCK_NAMES or "clk" in n or "clock" in n


def extract_top(verilog_src: str) -> str:
    m = _TOP_PAT.search(verilog_src)
    if not m:
        raise ValueError("Could not find a `module <name>` declaration in the Verilog source.")
    return m.group(1)


def design_ports(verilog_src: str) -> tuple[list[str], list[str]]:
    """Return (input_ports, output_ports) parsed from the module declaration.

    Handles ANSI-style headers like `input clk,` and `output reg [15:0] sum,`.
    Bus widths and reg/wire/logic qualifiers are stripped; only the bare port
    identifiers are returned.
    """
    inputs: list[str] = []
    outputs: list[str] = []
    for line in verilog_src.splitlines():
        m = _PORT_PAT.match(line)
        if not m:
            continue
        kind, rest = m.group(1), m.group(2)
        rest = rest.split("//")[0]
        rest = re.sub(r"\b(reg|wire|logic|signed)\b", "", rest)
        rest = re.sub(r"\[[^\]]*\]", "", rest)        # strip bus widths
        rest = rest.replace(");", "").replace(";", "").replace(")", "")
        names = [n.strip() for n in rest.split(",") if n.strip()]
        names = [n for n in names if re.fullmatch(r"\w+", n)]
        (inputs if kind == "input" else outputs).extend(names)
    return inputs, outputs


def expected_coverage(verilog_src: str) -> tuple[bool, bool]:
    """What path groups *should* be timed for this design.

    expect_in2reg : design has a non-clock, non-async-reset data input
    expect_reg2out: design has at least one output port
    """
    inputs, outputs = design_ports(verilog_src)
    data_inputs = [p for p in inputs
                   if not _is_clock_port(p) and not _RESET_PAT.match(p)]
    return bool(data_inputs), bool(outputs)


def compute_coverage(verilog_src: str, sta_result) -> dict:
    """Compare what STA actually timed against what the design requires."""
    expect_in2reg, expect_reg2out = expected_coverage(verilog_src)
    analyzed = bool(sta_result.ok) and not bool(sta_result.no_paths)
    missing_in2reg = analyzed and expect_in2reg and sta_result.n_in2reg == 0
    missing_reg2out = analyzed and expect_reg2out and sta_result.n_reg2out == 0
    coverage_complete = analyzed and not missing_in2reg and not missing_reg2out
    return {
        "expect_in2reg": expect_in2reg,
        "expect_reg2out": expect_reg2out,
        "missing_in2reg": missing_in2reg,
        "missing_reg2out": missing_reg2out,
        "coverage_complete": coverage_complete,
    }


# How usable a constraint set is, best -> worst. Used to tell whether a later
# correction level actually improved on an earlier one.
_SEVERITY = {
    "ok": 0,
    "partial_coverage": 1,
    "timing_violation": 2,
    "ineffective_no_paths": 3,
    "sta_failed": 4,
    "synth_failed": 5,
}


def compute_final_status(synth_ok: bool, sta_ok: bool, no_paths: bool,
                         timing_met, coverage_complete: bool) -> str:
    """Single source of truth for final_status (shared with the backfill script).

    'partial_coverage' is new: STA passed and timing is met, but the I/O timing
    environment was never fully constrained, so the 'ok' was only earned on the
    paths that happened to be analyzed (typically reg2reg only).
    """
    if not synth_ok:
        return "synth_failed"
    if not sta_ok:
        return "sta_failed"
    if no_paths:
        return "ineffective_no_paths"
    if timing_met is False:
        return "timing_violation"
    if timing_met and not coverage_complete:
        return "partial_coverage"
    if timing_met:
        return "ok"
    return "sta_failed"


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


def _make_run_id(design: str, seed: int | None, prompt_version: str,
                 reference: bool, model: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if reference:
        return f"{ts}_{design}_reference"
    seed_part = f"s{seed}" if seed is not None else "noseed"
    model_part = model.replace(":", "-").replace("/", "-")
    return f"{ts}_{design}_{seed_part}_{prompt_version}_{model_part}"


def _evaluate(*, design_path: Path, top: str, work_dir: Path, sdc_path: Path,
              verilog_src: str, cleaned_sdc: str, raw_sdc: str,
              extracted_lines: list[str], target_period_ns: float) -> dict:
    """Synthesis + STA + coverage + classification for one SDC candidate.

    Factored out so the correction loop can re-run the identical evaluation on a
    corrected SDC instead of duplicating the stages.
    """
    clock_period_ns = extract_clock_period(cleaned_sdc, fallback=target_period_ns)
    synth_result = synthesis.run(design_path, top, work_dir,
                                 clock_period_ns=clock_period_ns)
    if synth_result.ok and synth_result.netlist_path is not None:
        sta_result = sta.run(synth_result.netlist_path, sdc_path, top, work_dir)
    else:
        sta_result = sta.STAResult(
            ok=False, returncode=-1, log="(skipped: synthesis failed)",
            wns_ns=None, tns_ns=None, min_slack_ns=None,
            timing_met=None, setup_violations=0,
            errors=["synthesis_failed"], duration_s=0.0,
        )
    cov = compute_coverage(verilog_src, sta_result)
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
        coverage_complete=cov["coverage_complete"],
        missing_in2reg=cov["missing_in2reg"],
        missing_reg2out=cov["missing_reg2out"],
    )
    final_status = compute_final_status(
        synth_ok=synth_result.ok,
        sta_ok=sta_result.ok,
        no_paths=sta_result.no_paths,
        timing_met=sta_result.timing_met,
        coverage_complete=cov["coverage_complete"],
    )
    return {"clock_period_ns": clock_period_ns, "synth": synth_result,
            "sta": sta_result, "cov": cov, "cls": cls,
            "final_status": final_status,
            # kept for the correction feedback: where this attempt's artifacts
            # live (coverage.txt) and the RTL the ports are parsed from.
            "work_dir": work_dir, "verilog_src": verilog_src}


def _tail(text: str, n: int = 20) -> str:
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def _sdc_diagnostics(log: str, n: int = 12) -> str:
    """Tool complaints that name the generated SDC, i.e. the model's own errors.

    Preferring lines that mention generated.sdc keeps unrelated tool noise (e.g.
    deprecation warnings raised by our own TCL) out of the feedback.
    """
    lines = [l.strip() for l in (log or "").splitlines() if l.strip()]
    sdc_hits = [l for l in lines if "generated.sdc" in l]
    if sdc_hits:
        return "\n".join(sdc_hits[:n])
    errs = [l for l in lines if re.search(r"\berror\b", l, re.IGNORECASE)]
    return "\n".join(errs[:n]) if errs else _tail(log, n)


def _data_ports(verilog_src: str) -> tuple[list[str], list[str]]:
    """(data inputs, outputs): the ports whose timing paths the coverage check
    expects — clocks and async resets excluded."""
    if not verilog_src:
        return [], []
    inputs, outputs = design_ports(verilog_src)
    data_in = [p for p in inputs
               if not _is_clock_port(p) and not _RESET_PAT.match(p)]
    return data_in, outputs


_VIOL_PAT = re.compile(r"\[(\w+)\]\s+(\S+)\s+->\s+(\S+)\s+:\s+slack=(-?[\d.]+)")


def _violation_summary(work_dir: Path | None, max_examples: int = 3) -> str:
    """Summarize the violating-path list the STA stage wrote to coverage.txt.

    Quotes tool-reported facts only (which path group violates, example
    endpoints) — the group is the informative part: 'register-to-register
    violates' locates the problem without prescribing the fix.
    """
    if not work_dir:
        return ""
    cov_file = Path(work_dir) / "coverage.txt"
    if not cov_file.exists():
        return ""
    viols = _VIOL_PAT.findall(cov_file.read_text())
    if not viols:
        return ""
    by_group = {}
    for group, s, e, slack in viols:
        by_group.setdefault(group, []).append((s, e, slack))
    parts = []
    for group, items in by_group.items():
        ex = "; ".join(f"{s} -> {e} (slack {sl})" for s, e, sl in items[:max_examples])
        more = f" and {len(items) - max_examples} more" if len(items) > max_examples else ""
        parts.append(f"{len(items)} violating {group} path(s), e.g. {ex}{more}")
    return "Violating paths by group: " + "; ".join(parts) + "."


_DECL_PAT = re.compile(r"^\s*(?:output\s+)?(?:reg|wire|logic)\s*(?:\[[^\]]*\]\s*)?"
                       r"([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*;", re.MULTILINE)


def _internal_nets(verilog_src: str) -> list[str]:
    """RTL-level reg/wire names that are not ports — the names [get_nets] can
    legally target (module-level nets usually survive synthesis, unlike the
    gate-level pin names models tend to invent)."""
    inputs, outputs = design_ports(verilog_src)
    ports = set(inputs) | set(outputs)
    nets: list[str] = []
    for m in _DECL_PAT.finditer(verilog_src):
        for name in (n.strip() for n in m.group(1).split(",")):
            if name and name not in ports and name not in nets:
                nets.append(name)
    return nets


def correction_guidance(level: int, verilog_src: str) -> str:
    """Extra help appended to the symptom at each ladder level.

    Level 1: nothing — the symptom alone (tests: was it just a slip?).
    Level 2+: the legal object vocabulary (tests: was the model only missing the
             namespace? It knows RTL names, but not that synthesis renames them).

    Everything here is derived from the design's own Verilog, which the model
    already sees, so no information from the reference SDC ever reaches it: the
    ladder measures self-repair, not hint-following.
    """
    if level <= 1:
        return ""
    inputs, outputs = design_ports(verilog_src)
    nets = _internal_nets(verilog_src)
    vocab = ("How to reference objects in this flow:\n"
             f"- Module ports (use [get_ports NAME]): "
             f"{', '.join(inputs + outputs)}\n"
             "- All registers collectively: [all_registers]\n")
    if nets:
        vocab += (f"- Internal RTL nets (use [get_nets NAME]): {', '.join(nets)}\n")
    vocab += ("- Do NOT reference gate-level pins (like NAME_reg/Q): internal "
              "names are renamed during synthesis, so RTL-style pin names will "
              "not exist in the netlist.")
    return vocab


def correction_feedback(ev: dict) -> str:
    """The 'here is what went wrong' text handed back to the model.

    Uses the tools' own words wherever a tool actually complained, and a derived
    explanation only for partial_coverage -- the one failure STA stays silent
    about, since it reports clean timing on the subset of paths it did analyze.
    The reference SDC is never revealed; the model only ever sees its own output
    and the consequence of it.
    """
    status = ev["final_status"]
    sta_result, synth_result, cov = ev["sta"], ev["synth"], ev["cov"]

    if status == "synth_failed":
        return ("Synthesis (Yosys) failed with these constraints.\n\n"
                + _tail(synth_result.log))

    if status == "sta_failed":
        return ("Static timing analysis (OpenSTA) rejected these constraints:\n\n"
                + _sdc_diagnostics(sta_result.log))

    if status == "ineffective_no_paths":
        return ("The constraints were accepted, but static timing analysis found "
                "NO timing paths at all, so nothing was actually checked. There is "
                "likely no usable clock definition, or no constrained start/end "
                "points for the analyzer to time.")

    # Note: the two messages below report the symptom only, never the remedy.
    # Naming the fix (e.g. "add -clock", "use a multicycle exception") would turn
    # the measurement into "can the model follow an instruction" instead of "can
    # the model repair its own mistake" -- the remedy-shaped help lives in the
    # later ladder levels, where supplying it is the point. STA's own words are
    # used wherever the tool actually complained; these two are derived only
    # because the tool stays silent about them. Both are made *precise* (which
    # ports, which path group) since vague symptoms measurably do not repair:
    # naming the location is still a symptom, not an instruction.
    if status == "partial_coverage":
        data_in, outs = _data_ports(ev.get("verilog_src", ""))
        missing = []
        if cov["missing_in2reg"]:
            ports = f" (input ports: {', '.join(data_in)})" if data_in else ""
            missing.append(f"paths from the input ports into registers were "
                           f"never timed{ports}")
        if cov["missing_reg2out"]:
            ports = f" (output ports: {', '.join(outs)})" if outs else ""
            missing.append(f"paths from registers to the output ports were "
                           f"never timed{ports}")
        return ("Timing was reported as met, but the analysis was INCOMPLETE: "
                + "; ".join(missing) + ". Those paths were not analyzed at all, "
                "so the reported result covers only the paths that were actually "
                "timed.")

    if status == "timing_violation":
        n = sta_result.setup_violations
        msg = (f"The constraints were accepted and paths were analyzed, but timing "
               f"is NOT met: the worst negative slack is {sta_result.wns_ns} ns "
               f"({n} path(s) with negative slack).")
        viol = _violation_summary(ev.get("work_dir"))
        if viol:
            msg += "\n" + viol
        return msg

    return f"The run did not fully succeed (status: {status})."


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

    run_id = _make_run_id(design_name, seed, prompt_version, use_reference_sdc, model)
    run_dir = config.RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now().isoformat(timespec="seconds")

    # 1. Get SDC (either LLM or reference)
    correction_attempts = 0
    # Two independent timings: first-shot generation and the correction retry.
    # Kept separate (not cumulative) so per-model speed stays comparable no matter
    # how often the retry fires; total LLM cost is simply their sum.
    llm_duration_s = 0.0     # generation only
    llm_correction_s = 0.0   # correction retry only
    if use_reference_sdc:
        ref_path = config.REFERENCE_SDC_DIR / f"{design_name}.sdc"
        if not ref_path.exists():
            raise FileNotFoundError(f"No reference SDC for {design_name}: {ref_path}")
        cleaned_sdc = ref_path.read_text().strip()
        raw_sdc = cleaned_sdc
        extracted_lines = [l.strip() for l in cleaned_sdc.splitlines() if l.strip()]
    else:
        prompt_path = llm.prompt_path_for(prompt_version)
        t0 = time.perf_counter()
        result = llm.generate(verilog_src, target_period_ns, model=model, seed=seed,
                              prompt_path=prompt_path)
        llm_duration_s += time.perf_counter() - t0
        raw_sdc = result.raw
        cleaned_sdc = result.cleaned
        extracted_lines = result.extracted_lines
        (run_dir / "llm_raw.txt").write_text(raw_sdc)

    (run_dir / "generated.sdc").write_text(cleaned_sdc + "\n")
    sdc_path = run_dir / "generated.sdc"

    # 2-3. Evaluate the first attempt: synthesis -> STA -> coverage -> classify.
    ev = _evaluate(design_path=design_path, top=top, work_dir=run_dir,
                   sdc_path=sdc_path, verilog_src=verilog_src,
                   cleaned_sdc=cleaned_sdc, raw_sdc=raw_sdc,
                   extracted_lines=extracted_lines,
                   target_period_ns=target_period_ns)
    initial_status = ev["final_status"]

    # 4. Correction ladder, on ANY non-ok outcome.
    #
    # Runs *after* classification (not between synthesis and STA) so it reacts
    # to the verdict that actually occurs. Each level re-feeds the LATEST SDC and
    # its error with progressively stronger help (see correction_guidance):
    #   level 1  symptom only          -> repairs here were robustness slips
    #   level 2  + object vocabulary   -> repairs here were namespace-limited
    # The level where a run gets repaired is itself the diagnosis, and because
    # every level sees the latest state, the ladder can also recover from a
    # regression introduced by an earlier level. Each attempt is evaluated in its
    # own correction_<n>/ dir, so every attempt's artifacts stay comparable.
    status_path = [ev["final_status"]]
    best_status = ev["final_status"]
    while (
        ev["final_status"] != "ok"
        and not use_reference_sdc
        and correction_attempts < max_corrections
    ):
        correction_attempts += 1
        corr_dir = run_dir / f"correction_{correction_attempts}"
        corr_dir.mkdir(exist_ok=True)
        feedback = correction_feedback(ev)
        guidance = correction_guidance(correction_attempts, verilog_src)
        if guidance:
            feedback = f"{feedback}\n\n{guidance}"
        (corr_dir / "feedback.txt").write_text(feedback)
        t0 = time.perf_counter()
        corr = llm.correct(verilog_src, cleaned_sdc, feedback,
                           clock_period_ns=target_period_ns, model=model)
        llm_correction_s += time.perf_counter() - t0
        (corr_dir / "llm_raw.txt").write_text(corr.raw)
        if not corr.extracted_lines:
            # nothing usable came back; keep the previous verdict but record
            # that this level produced no evaluable SDC
            status_path.append("empty_output")
            break
        cleaned_sdc = corr.cleaned
        extracted_lines = corr.extracted_lines
        raw_sdc = corr.raw
        sdc_path = corr_dir / "generated.sdc"
        sdc_path.write_text(cleaned_sdc + "\n")
        ev = _evaluate(design_path=design_path, top=top, work_dir=corr_dir,
                       sdc_path=sdc_path, verilog_src=verilog_src,
                       cleaned_sdc=cleaned_sdc, raw_sdc=raw_sdc,
                       extracted_lines=extracted_lines,
                       target_period_ns=target_period_ns)
        status_path.append(ev["final_status"])
        # A later level can end up worse than an earlier one, and the loop stops
        # only on 'ok' -- so track the best verdict ever reached separately from
        # the last one, otherwise a late regression hides how close it got.
        if _SEVERITY.get(ev["final_status"], 9) < _SEVERITY.get(best_status, 9):
            best_status = ev["final_status"]

    # The recorded metrics describe the final state; initial_status preserves the
    # pre-correction verdict so the before -> after transition stays measurable.
    synth_result = ev["synth"]
    sta_result = ev["sta"]
    cov = ev["cov"]
    cls = ev["cls"]
    final_status = ev["final_status"]
    clock_period_ns = ev["clock_period_ns"]

    # 5. Record
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
        "initial_status": initial_status,
        "corrected": initial_status != "ok" and final_status == "ok",
        "correction_path": ">".join(status_path) if correction_attempts else "",
        "best_status": best_status,
        "final_status": final_status,
        "primary_label": cls.primary,
        "all_labels": "|".join(cls.labels),
        "n_extracted_lines": len(extracted_lines),
        "has_create_clock": any(l.startswith("create_clock") for l in extracted_lines),
        "n_input_delays": _count_lines_starting(extracted_lines, "set_input_delay"),
        "n_output_delays": _count_lines_starting(extracted_lines, "set_output_delay"),
        "synthesis_ok": synth_result.ok,
        "llm_duration_s": round(llm_duration_s, 3),
        "llm_correction_s": round(llm_correction_s, 3),
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
        "n_paths_total": sta_result.n_paths_total,
        "n_in2reg": sta_result.n_in2reg,
        "n_reg2out": sta_result.n_reg2out,
        "n_reg2reg": sta_result.n_reg2reg,
        "n_in2out": sta_result.n_in2out,
        "n_async": sta_result.n_async,
        "wns_in2reg": sta_result.wns_in2reg if sta_result.wns_in2reg is not None else "",
        "wns_reg2out": sta_result.wns_reg2out if sta_result.wns_reg2out is not None else "",
        "wns_reg2reg": sta_result.wns_reg2reg if sta_result.wns_reg2reg is not None else "",
        "coverage_complete": cov["coverage_complete"],
        "rationale": "; ".join(cls.rationale),
        "run_dir": str(run_dir),
    }
    dataset.append_row(row)
    return row
