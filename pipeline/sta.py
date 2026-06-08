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
    # --- per-path-group coverage (filled by parse_path_groups) ---
    n_paths_total: int = 0     # total reported timing paths
    n_in2reg: int = 0          # input port  -> register (data setup)
    n_reg2out: int = 0         # register     -> output port
    n_reg2reg: int = 0         # register     -> register
    n_in2out: int = 0          # input port  -> output port (pure combinational)
    n_async: int = 0           # any          -> recovery/removal check (reset)
    wns_in2reg: float | None = None   # worst slack among in2reg paths
    wns_reg2out: float | None = None  # worst slack among reg2out paths
    wns_reg2reg: float | None = None  # worst slack among reg2reg paths


@dataclass
class PathGroups:
    counts: dict
    worst: dict
    violations: list  # list of (start_name, end_name, bucket, slack) with slack < 0


_START_PAT = re.compile(r"^\s*Startpoint:\s+(\S+)\s+\((.*)\)\s*$")
_END_PAT = re.compile(r"^\s*Endpoint:\s+(\S+)\s+\((.*)\)\s*$")


def _start_type(desc: str) -> str:
    d = desc.lower()
    if "input port" in d:
        return "input"
    if "flip-flop" in d or "latch" in d:
        return "reg"
    return "other"


def _end_type(desc: str) -> str:
    d = desc.lower()
    if "output port" in d:
        return "output"
    if "recovery" in d or "removal" in d:
        return "async"
    if "flip-flop" in d or "latch" in d:
        return "reg"
    return "other"


def _bucket(start: str | None, end: str | None) -> str:
    if end == "async":
        return "async"
    if start == "input" and end == "reg":
        return "in2reg"
    if start == "reg" and end == "output":
        return "reg2out"
    if start == "reg" and end == "reg":
        return "reg2reg"
    if start == "input" and end == "output":
        return "in2out"
    return "other"


def parse_path_groups(log: str) -> PathGroups:
    """Classify every reported timing path into a coverage bucket.

    A path block in the OpenSTA report looks like:
        Startpoint: a[2] (input port clocked by clk)
        Endpoint:   _276_ (rising edge-triggered flip-flop clocked by clk)
        ...
                 4.10   slack (MET)

    We pair each Startpoint/Endpoint with the slack line that closes the block
    and tally it into one of: in2reg / reg2out / reg2reg / in2out / async / other.
    This lets us tell a genuinely complete analysis apart from one that only saw
    reg2reg paths because the I/O delays were never clock-associated.
    """
    counts = {"in2reg": 0, "reg2out": 0, "reg2reg": 0,
              "in2out": 0, "async": 0, "other": 0}
    worst: dict = {"in2reg": None, "reg2out": None, "reg2reg": None}
    violations: list = []

    s_name = e_name = None
    s_type = e_type = None
    for line in log.splitlines():
        m = _START_PAT.match(line)
        if m:
            s_name, s_type = m.group(1), _start_type(m.group(2))
            continue
        m = _END_PAT.match(line)
        if m:
            e_name, e_type = m.group(1), _end_type(m.group(2))
            continue
        m = _SLACK_PAT.match(line)
        if m:
            try:
                slack = float(m.group(1))
            except ValueError:
                s_type = e_type = None
                continue
            b = _bucket(s_type, e_type)
            counts[b] += 1
            if b in worst and (worst[b] is None or slack < worst[b]):
                worst[b] = slack
            if slack < 0:
                violations.append((s_name, e_name, b, slack))
            s_name = e_name = s_type = e_type = None
    return PathGroups(counts=counts, worst=worst, violations=violations)


def _write_coverage_report(run_dir: Path, pg: PathGroups) -> None:
    """Human-readable per-run coverage summary + full violating-path list."""
    c = pg.counts
    total = sum(c.values())
    lines = [
        "# Timing-path coverage summary",
        f"total_paths      = {total}",
        f"in2reg  (in->reg)  = {c['in2reg']}   worst_slack={pg.worst['in2reg']}",
        f"reg2out (reg->out) = {c['reg2out']}   worst_slack={pg.worst['reg2out']}",
        f"reg2reg (reg->reg) = {c['reg2reg']}   worst_slack={pg.worst['reg2reg']}",
        f"in2out  (in->out)  = {c['in2out']}",
        f"async   (reset)    = {c['async']}",
        f"other              = {c['other']}",
        "",
        f"# Violating paths ({len(pg.violations)}):",
    ]
    if pg.violations:
        for s_name, e_name, bucket, slack in pg.violations:
            lines.append(f"  [{bucket}] {s_name} -> {e_name} : slack={slack}")
    else:
        lines.append("  (none)")
    (run_dir / "coverage.txt").write_text("\n".join(lines) + "\n")


_SECTION_MARK = "@@@SECTION "


def _tcl_script(liberty: Path, netlist: Path, sdc: Path, top: str) -> str:
    """STA script with explicit, independently-filtered per-group path queries.

    The design-wide `report_checks` (the "all" section) only prints the single
    worst path per endpoint, so a path group that is constrained but never
    critical at any endpoint can be invisible (e.g. an input setup path masked
    by a register feedback path). The per-group queries below use `-from`/`-to`
    filters so each group is reported on its own and cannot be masked — this is
    what makes coverage detection reliable.
    """
    return f"""
read_liberty {liberty}
read_verilog {netlist}
link_design {top}
read_sdc {sdc}
puts "{_SECTION_MARK}all"
report_checks -path_delay max -group_count 200 -slack_max 1e30
report_wns
report_tns
puts "{_SECTION_MARK}in2reg"
catch {{ report_checks -from [all_inputs] -to [all_registers -data_pins] \
    -path_delay max -group_count 1000 -slack_max 1e30 }}
puts "{_SECTION_MARK}reg2out"
catch {{ report_checks -from [all_registers -clock_pins] -to [all_outputs] \
    -path_delay max -group_count 1000 -slack_max 1e30 }}
puts "{_SECTION_MARK}reg2reg"
catch {{ report_checks -from [all_registers -clock_pins] -to [all_registers -data_pins] \
    -path_delay max -group_count 1000 -slack_max 1e30 }}
puts "{_SECTION_MARK}end"
exit
""".strip()


def _split_sections(log: str) -> dict:
    """Split an STA log into the named sections emitted by the TCL markers.

    Returns a dict {section_name: text}. If no markers are present (e.g. a log
    produced by the older single-report TCL), returns {'all': <whole log>} so
    callers stay backward-compatible.
    """
    if _SECTION_MARK not in log:
        return {"all": log}
    sections: dict = {}
    cur = "preamble"
    buf: list = []
    for line in log.splitlines():
        if line.startswith(_SECTION_MARK):
            sections[cur] = "\n".join(buf)
            cur = line[len(_SECTION_MARK):].strip()
            buf = []
        else:
            buf.append(line)
    sections[cur] = "\n".join(buf)
    return sections


def _section_paths(text: str) -> list:
    """Return [(start_name, end_name, slack), ...] for every path in a section.

    The section is already filtered to one group by the -from/-to query, so we
    just pair each Startpoint/Endpoint with its closing slack line and trust the
    filter — no start/end type heuristic needed, and nothing can be masked.
    """
    out: list = []
    s_name = e_name = None
    for line in text.splitlines():
        m = _START_PAT.match(line)
        if m:
            s_name = m.group(1)
            continue
        m = _END_PAT.match(line)
        if m:
            e_name = m.group(1)
            continue
        m = _SLACK_PAT.match(line)
        if m:
            try:
                out.append((s_name, e_name, float(m.group(1))))
            except ValueError:
                pass
            s_name = e_name = None
    return out


def _coverage_from_sections(sections: dict) -> PathGroups:
    """Build path-group coverage from the independent per-group queries.

    For the new section-delimited logs, in2reg / reg2out / reg2reg counts come
    from their own `-from`/`-to` filtered queries, so a constrained-but-non-
    critical path can no longer be masked by a competing path at the same
    endpoint. in2out / async remain informational, derived from the design-wide
    "all" report.

    For older logs without section markers, fall back to the heuristic
    `parse_path_groups` over the whole log so the backfill path still works.
    """
    if "in2reg" not in sections:          # legacy single-report log
        return parse_path_groups(sections.get("all", ""))

    info = parse_path_groups(sections.get("all", ""))  # for in2out / async / other
    counts = {
        "in2reg": 0, "reg2out": 0, "reg2reg": 0,
        "in2out": info.counts["in2out"],
        "async": info.counts["async"],
        "other": info.counts["other"],
    }
    worst: dict = {"in2reg": None, "reg2out": None, "reg2reg": None}
    violations: list = []

    for grp in ("in2reg", "reg2out", "reg2reg"):
        paths = _section_paths(sections.get(grp, ""))
        counts[grp] = len(paths)
        slacks = [s for _, _, s in paths]
        worst[grp] = min(slacks) if slacks else None
        violations.extend((sn, en, grp, s) for sn, en, s in paths if s < 0)

    # carry through informational async / in2out violations from the all-report
    violations.extend(v for v in info.violations if v[2] in ("async", "in2out"))
    return PathGroups(counts=counts, worst=worst, violations=violations)


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

    sections = _split_sections(log_text)
    all_sec = sections.get("all", log_text)

    # Design-wide metrics come from the "all" section (report_wns / report_tns
    # and the global report_checks). Restricting to all_sec avoids double-counting
    # slack lines from the per-group sections.
    wns, tns, min_slack, violations, _ = _parse(all_sec)
    # Fatal errors can occur anywhere (e.g. read_sdc in the preamble), so scan
    # the whole log for them; but only the "all" section decides "no paths".
    _, _, _, _, errors = _parse(log_text)
    no_paths = bool(_NO_PATHS_PAT.search(all_sec))
    fatal_errors = [e for e in errors if e.lower().startswith("error")]
    ok = proc.returncode == 0 and not fatal_errors
    # timing_met is only meaningful if STA actually analyzed paths
    if not ok or no_paths:
        timing_met = None
    else:
        timing_met = (wns is not None and wns >= 0)

    # Per-path-group coverage, from the independent per-group queries.
    pg = _coverage_from_sections(sections)
    _write_coverage_report(run_dir, pg)
    c = pg.counts

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
        n_paths_total=sum(c.values()),
        n_in2reg=c["in2reg"],
        n_reg2out=c["reg2out"],
        n_reg2reg=c["reg2reg"],
        n_in2out=c["in2out"],
        n_async=c["async"],
        wns_in2reg=pg.worst["in2reg"],
        wns_reg2out=pg.worst["reg2out"],
        wns_reg2reg=pg.worst["reg2reg"],
    )
