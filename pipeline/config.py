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
                             "qwen3:8b,granite4.1:3b,granite4.1:8b,gemma4:e2b,gemma4:e4b,minimax-m3:cloud"
                             ).split(",") if m.strip()]

LLM_TIMEOUT_S = 180

DEFAULT_CLOCK_PERIOD_NS = 5.0

# Per-design target clock period override (ns). Designs not listed use the CLI
# --period (default DEFAULT_CLOCK_PERIOD_NS). mult_mcp needs a period below the
# multiplier's single-cycle delay (~1.77 ns) so the multicycle exception is
# load-bearing: at 1.5 ns the ra/rb -> product path violates in one cycle and
# only passes with set_multicycle_path 2.
DESIGN_PERIODS = {
    "mult_mcp": 1.5,
}

# Depth of the correction ladder (retries after a non-ok run). Each level feeds
# progressively stronger help (see orchestrator.correction_guidance):
#   1 = the tool's error plus the model's own previous SDC
#   2 = + the object dictionary (real port/net names, [all_registers], and the
#       fact that RTL pin names do not survive synthesis)
# The level at which a run is repaired is the diagnosis: a repair at 1 was a
# robustness slip, at 2 a namespace limitation, never = a deeper gap.
# At this depth nothing derived from the reference SDC ever reaches the model.
MAX_CORRECTION_ATTEMPTS = 2

# Prompt versions whose failures get a correction retry. Generation runs on
# every version, but correcting v1 would mostly re-measure the missing
# -name/-clock scaffold that v2 already fixes, so the retry budget is spent
# where the failures are real capability gaps.
CORRECTION_PROMPT_VERSIONS = {"v2_base"}

for d in (RUNS_DIR, RESULTS_DIR, REFERENCE_SDC_DIR):
    d.mkdir(parents=True, exist_ok=True)
