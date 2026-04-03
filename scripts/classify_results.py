from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
log_path = ROOT / "reports" / "run.log"

if len(sys.argv) > 1:
    log_path = Path(sys.argv[1])

if not log_path.exists():
    print("classification=no_run")
    raise SystemExit(0)

text = log_path.read_text(errors="ignore").lower()

if "error" in text or "failed" in text:
    if "wrong_clk" in text or "not found" in text or "no object" in text:
        print("classification=incorrect_constraint")
    else:
        print("classification=syntax_or_tool_error")
elif "warning" in text:
    print("classification=accepted_with_warning")
else:
    print("classification=accepted")