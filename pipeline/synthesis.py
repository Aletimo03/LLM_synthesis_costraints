"""Yosys synthesis wrapper with ABC + liberty technology mapping."""
from __future__ import annotations
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from . import config


@dataclass
class SynthesisResult:
    ok: bool
    returncode: int
    log: str
    netlist_path: Path | None
    json_path: Path | None
    cells: int | None
    chip_area: float | None
    cell_breakdown: dict
    duration_s: float


def _build_script(
    verilog_path: Path,
    top: str,
    liberty: Path,
    netlist_out: Path,
    json_out: Path,
    clock_period_ns: float | None,
) -> str:
    # ABC takes target delay in picoseconds via -D
    abc_dly = ""
    if clock_period_ns is not None and clock_period_ns > 0:
        abc_dly = f" -D {int(clock_period_ns * 1000)}"

    return f"""
read_verilog {verilog_path}
hierarchy -check -top {top}
proc
flatten
opt
fsm
opt
memory
opt
techmap
opt
dfflibmap -liberty {liberty}
abc -liberty {liberty}{abc_dly}
opt_clean -purge
stat -liberty {liberty}
write_verilog -noattr {netlist_out}
write_json {json_out}
""".strip()


# Patterns for the `stat -liberty <lib>` output format
# "1    4.522 cells" (totals line) or "1    4.522   DFF_X1" (per-cell)
_TOTAL_LINE = re.compile(r"^\s*(\d+)\s+([\d.]+)\s+cells\s*$")
_CELL_BREAK_LINE = re.compile(r"^\s*(\d+)\s+([\d.]+)\s+(\S+)\s*$")
# Fallback for no-liberty format: "1   $_DFF_P_"
_CELL_PLAIN_LINE = re.compile(r"^\s*(\d+)\s+(\S+)\s*$")
_AREA_LINE = re.compile(r"Chip area for module[^:]*:\s*([\d.]+)")


def _parse_stat(log: str) -> tuple[int | None, float | None, dict]:
    """Parse Yosys `stat` block. Returns (total_cells, chip_area, cell_breakdown)."""
    in_stat = False
    cells: dict[str, int] = {}
    total: int | None = None
    area: float | None = None

    for line in log.splitlines():
        m = _AREA_LINE.search(line)
        if m:
            try:
                area = float(m.group(1))
            except ValueError:
                pass

        if line.strip().startswith("=== ") and line.strip().endswith(" ==="):
            in_stat = True
            continue
        if not in_stat:
            continue

        m = _TOTAL_LINE.match(line)
        if m:
            total = int(m.group(1))
            continue

        m = _CELL_BREAK_LINE.match(line)
        if m:
            count, _area, name = m.group(1), m.group(2), m.group(3)
            if name in ("cells", "wires", "ports") or name.startswith("$"):
                continue
            cells[name] = int(count)
            continue

        m = _CELL_PLAIN_LINE.match(line)
        if m:
            count, name = m.group(1), m.group(2)
            if name in ("cells", "wires", "ports", "bits") or name.startswith("$"):
                continue
            cells.setdefault(name, int(count))

    if total is None and cells:
        total = sum(cells.values())
    return total, area, cells


def run(
    verilog_path: Path,
    top: str,
    run_dir: Path,
    clock_period_ns: float | None = None,
    liberty: Path = config.LIBERTY_FILE,
) -> SynthesisResult:
    import time
    run_dir.mkdir(parents=True, exist_ok=True)
    netlist_out = run_dir / "netlist.v"
    json_out = run_dir / "netlist.json"
    log_out = run_dir / "yosys.log"
    script_path = run_dir / "synth.ys"

    script = _build_script(verilog_path, top, liberty, netlist_out, json_out, clock_period_ns)
    script_path.write_text(script)

    t0 = time.time()
    proc = subprocess.run(
        [config.YOSYS_BIN, "-l", str(log_out), "-s", str(script_path)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    duration = time.time() - t0

    log_text = log_out.read_text(errors="ignore") if log_out.exists() else ""
    if proc.stdout:
        log_text += "\n--- STDOUT ---\n" + proc.stdout
    if proc.stderr:
        log_text += "\n--- STDERR ---\n" + proc.stderr

    total, area, breakdown = _parse_stat(log_text)
    ok = proc.returncode == 0 and netlist_out.exists()

    return SynthesisResult(
        ok=ok,
        returncode=proc.returncode,
        log=log_text,
        netlist_path=netlist_out if netlist_out.exists() else None,
        json_path=json_out if json_out.exists() else None,
        cells=total,
        chip_area=area,
        cell_breakdown=breakdown,
        duration_s=duration,
    )
