from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)

case_name = sys.argv[1] if len(sys.argv) > 1 else "smoke"
log_file = REPORTS / f"{case_name}.log"

cmd = [
    "yosys",
    "-c", "scripts/run_yosys.ys",
    "-l", "reports/simple.log"
]

result = subprocess.run(cmd, text=True, cwd=ROOT)
print(f"returncode={result.returncode}")
print(f"log={log_file}")