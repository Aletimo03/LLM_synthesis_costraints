"""Classify SDC constraint quality based on tool outputs and content."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from .llm import VALID_SDC_PREFIXES


# Classification labels (multi-label allowed)
EMPTY = "empty_output"
NO_VALID_LINES = "no_valid_sdc_lines"
MISSING_CLOCK = "missing_create_clock"
SYNTAX_ERROR = "syntax_error"
PORT_ERROR = "port_reference_error"
NON_NUMERIC = "non_numeric_value"
SYNTH_FAIL = "synthesis_failure"
STA_FAIL = "sta_failure"
TIMING_VIOLATION = "timing_violation"
INEFFECTIVE = "ineffective_constraint"
INCOMPLETE_COVERAGE = "incomplete_timing_coverage"
ACCEPTED = "accepted"
ACCEPTED_PARTIAL = "accepted_partial"


@dataclass
class Classification:
    labels: list[str] = field(default_factory=list)
    primary: str = ""
    rationale: list[str] = field(default_factory=list)


def _detect_port_error(log: str) -> bool:
    patterns = [
        r"no\s+port\s+found",
        r"can't\s+find\s+port",
        r"cannot\s+find\s+port",
        r"port\s+'[^']*'\s+not\s+found",
        r"no\s+object\s+matched",
        r"undefined\s+name",
    ]
    return any(re.search(p, log, re.IGNORECASE) for p in patterns)


def _detect_syntax_error(log: str) -> bool:
    patterns = [
        r"syntax\s+error",
        r"invalid\s+command",
        r"unknown\s+command",
        r"expected\s+\S+\s+but\s+got",
        r"parse\s+error",
        r"wrong\s+#\s*args",
        r"bad\s+option",
        r"invalid\s+value",
    ]
    return any(re.search(p, log, re.IGNORECASE) for p in patterns)


_NUMERIC = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$")


def _detect_non_numeric(sdc: str) -> bool:
    """Find create_clock or set_*_delay with a non-numeric period/delay arg.

    For set_input_delay / set_output_delay the delay value is the first
    bare (non-flag) numeric token, e.g.:
        set_input_delay -max 2.0 -clock clk [get_ports a]
        set_output_delay 0.5 -clock clk [get_ports y]
    """
    for line in sdc.splitlines():
        m = re.search(r"-period\s+(\S+)", line)
        if m and not _NUMERIC.match(m.group(1)):
            return True

        if line.lstrip().startswith(("set_input_delay", "set_output_delay",
                                     "set_max_delay", "set_min_delay")):
            # Tokenize, skip the command, skip flags and their string values.
            tokens = line.split()[1:]
            i = 0
            found_numeric = False
            while i < len(tokens):
                t = tokens[i]
                if t.startswith("-"):
                    # Flag — skip its argument if it isn't itself a flag or a [..] group
                    if i + 1 < len(tokens) and not tokens[i + 1].startswith(("-", "[")):
                        i += 2
                    else:
                        i += 1
                    continue
                if t.startswith("["):
                    break  # entered the port spec, no numeric seen
                if _NUMERIC.match(t):
                    found_numeric = True
                    break
                # First bare non-flag, non-bracket token that isn't numeric → bad
                return True
            # If no numeric token at all, classifier will catch via other means
            _ = found_numeric
    return False


def classify(
    *,
    extracted_lines: list[str],
    cleaned_sdc: str,
    raw_sdc: str,
    synthesis_ok: bool,
    synthesis_log: str,
    sta_ok: bool,
    sta_log: str,
    timing_met: bool | None,
    wns_ns: float | None,
    no_paths: bool = False,
    coverage_complete: bool = True,
    missing_in2reg: bool = False,
    missing_reg2out: bool = False,
) -> Classification:
    labels: list[str] = []
    rationale: list[str] = []

    if not raw_sdc.strip():
        labels.append(EMPTY)
        rationale.append("LLM returned empty output.")
        return Classification(labels=labels, primary=EMPTY, rationale=rationale)

    if not extracted_lines:
        labels.append(NO_VALID_LINES)
        rationale.append("No lines matched valid SDC command prefixes.")

    has_clock = any(l.startswith("create_clock") or l.startswith("create_generated_clock")
                    for l in extracted_lines)
    if extracted_lines and not has_clock:
        labels.append(MISSING_CLOCK)
        rationale.append("No create_clock command found.")

    if _detect_non_numeric(cleaned_sdc):
        labels.append(NON_NUMERIC)
        rationale.append("Non-numeric value used for -period or delay.")

    combined_log = (synthesis_log or "") + "\n" + (sta_log or "")
    if _detect_port_error(combined_log):
        labels.append(PORT_ERROR)
        rationale.append("Tool reported an unresolved port reference.")
    if _detect_syntax_error(combined_log):
        labels.append(SYNTAX_ERROR)
        rationale.append("Tool reported an SDC syntax/parse error.")

    if not synthesis_ok:
        labels.append(SYNTH_FAIL)
        rationale.append("Yosys synthesis did not complete successfully.")
    if not sta_ok:
        labels.append(STA_FAIL)
        rationale.append("OpenSTA failed to run timing analysis.")

    if sta_ok and no_paths:
        labels.append(INEFFECTIVE)
        rationale.append("STA found no timing paths — constraints don't define a launch/capture relationship.")

    if sta_ok and timing_met is False and not no_paths:
        labels.append(TIMING_VIOLATION)
        rationale.append(f"Timing not met (WNS={wns_ns} ns).")

    # Incomplete coverage: STA passed and timing met, but the design's I/O
    # timing environment was never fully constrained. The "pass" was only
    # earned on the paths that happened to be analyzed (usually reg2reg only).
    if sta_ok and not no_paths and timing_met and not coverage_complete:
        labels.append(INCOMPLETE_COVERAGE)
        missing = []
        if missing_in2reg:
            missing.append("input->register")
        if missing_reg2out:
            missing.append("register->output")
        rationale.append(
            "STA met timing but never analyzed "
            + " and ".join(missing)
            + " paths — I/O delays were not clock-associated, so the success is "
              "only on the subset of paths that were timed."
        )

    # Determine primary label
    if not labels:
        labels.append(ACCEPTED)
        primary = ACCEPTED
        rationale.append("All checks passed; timing met with full path coverage.")
    else:
        # Priority order for primary
        priority = [
            EMPTY, NO_VALID_LINES, SYNTAX_ERROR, NON_NUMERIC, PORT_ERROR,
            MISSING_CLOCK, SYNTH_FAIL, STA_FAIL, TIMING_VIOLATION,
            INEFFECTIVE, INCOMPLETE_COVERAGE, ACCEPTED_PARTIAL, ACCEPTED,
        ]
        primary = next((lbl for lbl in priority if lbl in labels), labels[0])

        if synthesis_ok and sta_ok and timing_met and coverage_complete:
            # has minor labels (e.g., missing some delay) but works fully
            if ACCEPTED not in labels:
                labels.append(ACCEPTED_PARTIAL)
                primary = ACCEPTED_PARTIAL

    return Classification(labels=labels, primary=primary, rationale=rationale)
