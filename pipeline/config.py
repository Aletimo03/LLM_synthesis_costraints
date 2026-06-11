from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]

DESIGNS_DIR = ROOT / "designs"
REFERENCE_SDC_DIR = ROOT / "designs" / "reference"
LIBS_DIR = ROOT / "libs"
PROMPTS_DIR = ROOT / "prompts"
RUNS_DIR = ROOT / "runs"
RESULTS_DIR = ROOT / "results"
DATASET_CSV = RESULTS_DIR / "dataset.csv"

LIBERTY_FILE = LIBS_DIR / "Nangate45_typ.lib"

OPENSTA_BIN = os.environ.get("OPENSTA_BIN", str(Path.home() / "tools" / "OpenSTA" / "build" / "sta"))
YOSYS_BIN = os.environ.get("YOSYS_BIN", "yosys")

LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3:8b")

# Models compared by a multi-model run (run_pipeline --compare-models).
# Override via env: LLM_MODELS="qwen3:8b,qwen3:4b,llama3.2:3b"
LLM_MODELS = [m.strip() for m in
              os.environ.get("LLM_MODELS",
                             "qwen3:8b,granite4.1:3b,granite4.1:8b,gemma4:e2b,gemma4:12b"
                             ).split(",") if m.strip()]

LLM_TIMEOUT_S = 180

DEFAULT_CLOCK_PERIOD_NS = 5.0

MAX_CORRECTION_ATTEMPTS = 3

for d in (RUNS_DIR, RESULTS_DIR, REFERENCE_SDC_DIR):
    d.mkdir(parents=True, exist_ok=True)
