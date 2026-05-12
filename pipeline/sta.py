"""OpenSTA wrapper for static timing analysis."""
from __future__ import annotations
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from . import config


@dataclass
class STAResult:
    ok: bool
    returncode: int
    log: str
    wns_ns: float | None       # worst negative slack (0 when timing met)
    tns_ns: float | None       # total negative slack
    min_slack_ns: float | None # minimum slack across all reported paths (can be positive)
    timing_met: bool | None    # all slacks >= 0
    setup_violations: int      # number of paths with negative slack
    no_paths: bool = False     # STA found no timing paths (ineffective constraints)
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0


def _tcl_script(liberty: Path, netlist: Path, sdc: Path, top: str) -> str:
    return f"""
read_liberty {liberty}
read_verilog {netlist}
link_design {top}
read_sdc {sdc}
report_checks -path_delay max -group_count 50 -slack_max 1e30
report_wns
report_tns
exit
""".strip()


_SLACK_PAT = re.compile(r"^\s*([-\d.]+)\s+slack\s+\((MET|VIOLATED)\)", re.IGNORECASE)
# OpenSTA formats: "wns max 0.00" or "wns 0.00"
_WNS_PAT = re.compile(r"\bwns\b(?:\s+(?:max|min))?\s+([-\d.]+)", re.IGNORECASE)
_TNS_PAT = re.compile(r"\btns\b(?:\s+(?:max|min))?\s+([-\d.]+)", re.IGNORECASE)
_ERROR_PAT = re.compile(r"^(Error|Warning):.*$", re.IGNORECASE)
_NO_PATHS_PAT = re.compile(r"\bNo paths found\b", re.IGNORECASE)


def _parse(log: str) -> tuple[float | None, float | None, float | None, int, list[str]]:
    slacks: list[float] = []
    wns: float | None = None
    tns: float | None = None
    errors: list[str] = []

    for line in log.splitlines():
        m = _SLACK_PAT.match(line)
        if m:
            try:
                slacks.append(float(m.group(1)))
            except ValueError:
                pass
            continue
        m = _WNS_PAT.search(line)
        if m and wns is None:
            try:
                wns = float(m.group(1))
            except ValueError:
                pass
        m = _TNS_PAT.search(line)
        if m and tns is None:
            try:
                tns = float(m.group(1))
            except ValueError:
                pass
        m = _ERROR_PAT.match(line)
        if m:
            errors.append(line.strip())

    setup_violations = sum(1 for s in slacks if s < 0)
    min_slack = min(slacks) if slacks else None
    if wns is None and slacks:
        wns = min(0.0, min_slack) if min_slack is not None else None
    if tns is None and slacks:
        tns = sum(s for s in slacks if s < 0)
    return wns, tns, min_slack, setup_violations, errors


def run(
    netlist: Path,
    sdc: Path,
    top: str,
    run_dir: Path,
    liberty: Path = config.LIBERTY_FILE,
) -> STAResult:
    import time
    run_dir.mkdir(parents=True, exist_ok=True)
    script_path = run_dir / "sta.tcl"
    log_path = run_dir / "sta.log"

    script_path.write_text(_tcl_script(liberty, netlist, sdc, top))

    t0 = time.time()
    try:
        proc = subprocess.run(
            [config.OPENSTA_BIN, "-no_init", "-no_splash", "-exit", str(script_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as e:
        return STAResult(ok=False, returncode=-1, log=f"OpenSTA binary not found: {e}",
                         wns_ns=None, tns_ns=None, min_slack_ns=None,
                         timing_met=None, setup_violations=0,
                         errors=[str(e)], duration_s=0.0)
    duration = time.time() - t0

    log_text = (proc.stdout or "") + "\n--- STDERR ---\n" + (proc.stderr or "")
    log_path.write_text(log_text)

    wns, tns, min_slack, violations, errors = _parse(log_text)
    no_paths = bool(_NO_PATHS_PAT.search(log_text))
    # OpenSTA exits non-zero on real errors; warnings are tolerable
    fatal_errors = [e for e in errors if e.lower().startswith("error")]
    ok = proc.returncode == 0 and not fatal_errors
    # timing_met is only meaningful if STA actually analyzed paths
    if not ok or no_paths:
        timing_met = None
    else:
        timing_met = (wns is not None and wns >= 0)

    return STAResult(
        ok=ok,
        returncode=proc.returncode,
        log=log_text,
        wns_ns=wns,
        tns_ns=tns,
        min_slack_ns=min_slack,
        timing_met=timing_met,
        setup_violations=violations,
        no_paths=no_paths,
        errors=fatal_errors,
        duration_s=duration,
    )
