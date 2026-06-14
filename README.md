# LLM Synthesis Constraints — Validation Framework

A pipeline that uses a local Large Language Model (Qwen3 via Ollama) to generate
Synopsys Design Constraints (SDC) for Verilog ASIC designs, then validates them
through a full Yosys synthesis + OpenSTA static timing analysis flow.

Each run records errors, classification labels, and Quality-of-Results (QoR)
metrics (cell count, area, slack) into a master CSV dataset, so the reliability
of LLM-generated constraints can be studied quantitatively.

---

## Project Goals

1. **Generate** SDC constraints from natural-language prompts using an LLM.
2. **Validate** the constraints by running them through real EDA tools.
3. **Classify** failures: syntax errors, port-reference errors, non-numeric
   values, missing `create_clock`, ineffective constraints (no timing paths),
   timing violations.
4. **Analyze QoR impact**: compare cells / area / slack across reference vs.
   LLM-generated constraints.
5. **(Advanced)** Self-correct failing constraints by feeding the tool error
   back to the LLM in a retry loop.

---

## Directory Layout

```
LLM_synthesis_costraints/
├── pipeline/              Python package — the validation framework itself
│   ├── config.py          paths, default clock period, tool binaries
│   ├── llm.py             Ollama generation + output cleaning + correction prompt
│   ├── synthesis.py       Yosys + ABC wrapper, writes gate-level netlist
│   ├── sta.py             OpenSTA wrapper, parses WNS / TNS / min-slack
│   ├── classifier.py      multi-label error categorization
│   ├── dataset.py         append-only CSV writer
│   └── orchestrator.py    glues all stages together (run_one entry point)
│
├── designs/               Verilog RTL test designs
│   ├── simple.v             1-bit D flip-flop (smallest sanity check)
│   ├── counter.v            8-bit up-counter with async reset + enable
│   ├── fsm.v                4-state finite state machine
│   ├── adder.v              16-bit registered adder with carry
│   ├── alu.v                8-bit ALU with 8 operations
│   ├── shift_reg.v          8-bit serial-in / parallel-out shift register
│   └── reference/         known-good SDC per design — used as QoR control
│
├── prompts/               LLM prompt templates
│   ├── base_v1.txt          original prompt (v1_base) — missing -name / -clock
│   ├── base_v2.txt          revised prompt (v2_base) — adds -name and -clock requirements
│   └── correction.txt       retry prompt fed with the failing SDC + tool error
│
├── libs/
│   └── Nangate45_typ.lib    Nangate 45 nm open standard cell library
│                            (used by Yosys/ABC for tech mapping & by OpenSTA)
│
├── scripts/
│   ├── run_pipeline.py    CLI entry point (argparse, runs single or batch)
│   ├── analyze_results.py summary tables (v1-vs-v2, coverage, per-group slack)
│   └── rerun_sta.py       replay OpenSTA on saved netlists, rebuild dataset
│
├── results/
│   └── dataset.csv          append-only master dataset, one row per run
│
├── runs/                  per-run artifacts (one folder per pipeline run)
│   └── <timestamp>_<design>_<seed>_<hash>/
│         llm_raw.txt          raw LLM output before cleaning
│         generated.sdc        cleaned SDC fed into the tools
│         synth.ys             generated Yosys script
│         yosys.log            full Yosys log
│         netlist.v / .json    synthesized gate-level netlist
│         sta.tcl              generated OpenSTA Tcl script
│         sta.log              full OpenSTA log
│         coverage.txt         path-group counts + full violating-path list
│         correction_<n>/      artifacts of correction-loop retries
│
├── pyproject.toml         declares the `pipeline` Python package
├── .gitignore             excludes runs/, dataset.csv, .venv/, .idea/, etc.
└── README.md              this file
```

---

## Tool Stack

| Tool | Version (M1/M4 Mac) | Role |
|---|---|---|
| **Python** | 3.11 | Pipeline language |
| **Ollama** | local daemon | Serves the LLMs |
| **Models** | `qwen3:8b`, `granite4.1:3b/8b`, `gemma4:e2b/12b` | Local LLMs compared across families & sizes (set in `LLM_MODELS`) |
| **Yosys** | 0.63 | Verilog → gate-level synthesis |
| **ABC** | bundled with Yosys | Timing-driven technology mapping |
| **OpenSTA** | 3.1.0 (built from source) | Static Timing Analysis (WNS, TNS, slack) |
| **Nangate45** | open standard cell lib | Liberty file used by both ABC and OpenSTA |


## One-Time Setup

### 1. System tools (via Homebrew on macOS)

```bash
brew install yosys ollama cmake swig bison tcl-tk@8
brew tap mht208/formal
brew install mht208/formal/cudd
```

### 2. Build OpenSTA from source (Apple Silicon supported)

```bash
mkdir -p ~/tools && cd ~/tools
git clone --depth=1 https://github.com/parallaxsw/OpenSTA.git
cd OpenSTA && mkdir build && cd build
export PATH="/opt/homebrew/opt/bison/bin:$PATH"
cmake .. -DCMAKE_BUILD_TYPE=Release \
  -DTCL_LIBRARY=/opt/homebrew/opt/tcl-tk@8/lib/libtcl8.6.dylib \
  -DTCL_HEADER=/opt/homebrew/opt/tcl-tk@8/include/tcl-tk/tcl.h \
  -DCUDD_DIR=/opt/homebrew/opt/cudd \
  -DFLEX_INCLUDE_DIR=/usr/include
make -j8

```
# binary ends up at ~/tools/OpenSTA/build/sta

`pipeline/config.py` looks for the OpenSTA binary at `~/tools/OpenSTA/build/sta`
by default. Override via the `OPENSTA_BIN` environment variable if you put it
elsewhere.

### 3. Start the Ollama server and pull the LLM model

The pipeline's `pipeline/llm.py` uses the `ollama` Python client, which talks to
the local Ollama **daemon** over HTTP (`http://localhost:11434`). That daemon
must be running before any LLM run, otherwise generation fails with a connection
error.

```bash
ollama serve            
ollama pull qwen3:8b    
ollama list             
```

`ollama serve` runs in the foreground — leave it in its own terminal, or rely on
the macOS Ollama.app which runs it as a background service. `ollama pull` and
every pipeline run route through this same daemon. Reference runs
(`--reference`) skip the LLM entirely and therefore do **not** need the daemon.

Different models are configurable via the `LLM_MODEL` env var or the
`--model` CLI flag. Anything Ollama serves works.

### 4. Python environment

```bash
cd LLM_synthesis_costraints
python3.11 -m venv .venv
.venv/bin/pip install -e .          # reads pyproject.toml, installs `pipeline`
```

The `-e` (editable) install means `from pipeline import ...` works from
anywhere — PyCharm, scripts, REPL — without `sys.path` hacks.

### 5. (Already done — included in the repo)

- `libs/Nangate45_typ.lib` — Nangate 45 nm liberty file
- `designs/reference/*.sdc` — golden constraints per design, used as the QoR
  baseline / control group

---

## Running the Pipeline

All commands run from the project root with the venv Python.

### Single run, LLM mode

```bash
.venv/bin/python scripts/run_pipeline.py --design designs/simple.v
```

### Single run, reference (control) — no LLM call

```bash
.venv/bin/python scripts/run_pipeline.py --design designs/simple.v --reference
```

### Multi-seed batch (sample the LLM N times)

```bash
.venv/bin/python scripts/run_pipeline.py --design designs/adder.v --seeds 1 2 3 4 5
```

### Full sweep — every design, every seed

```bash
.venv/bin/python scripts/run_pipeline.py --all --seeds 1 2 3
```

### Reference baseline across all designs

```bash
.venv/bin/python scripts/run_pipeline.py --all --reference
```

### Compare prompt versions (v1 vs v2)

`v2_base` adds the two structural requirements `v1_base` lacked — name the clock
with `-name`, and attach `-clock` to every `set_input_delay` / `set_output_delay`
(delay values are deliberately left to the model). This is what eliminates the
`partial_coverage` failures. Run both on the same designs/seeds to measure the
difference:

```bash
# every design, seeds 1-3, both prompt versions
.venv/bin/python scripts/run_pipeline.py --all --seeds 1 2 3 --compare-prompts

# or a single prompt version explicitly
.venv/bin/python scripts/run_pipeline.py --design designs/adder.v --prompt-version v2_base
```

### Full experiment in one go (fresh dataset: reference + both prompts, 3 seeds)

```bash
# one command rebuilds the entire dataset from scratch (needs ollama serve):
#   --fresh           drop the old dataset.csv first
#   --with-reference  re-run the control baseline for every design
#   --compare-prompts run both v1_base and v2_base
.venv/bin/python scripts/run_pipeline.py --all --seeds 1 2 3 \
    --compare-prompts --with-reference --fresh

# multi-model version: every model in LLM_MODELS runs the full matrix
# (reference is model-independent, so it runs once, on the first pass)
LLM_MODELS="qwen3:8b,qwen3:4b" .venv/bin/python scripts/run_pipeline.py \
    --all --seeds 1 2 3 --compare-prompts --with-reference --fresh --compare-models

# then the summary tables (per-model deep dive + cross-model comparison):
.venv/bin/python scripts/analyze_results.py
```

`--fresh` deletes `results/dataset.csv` at the start so the new rows fully
replace it — references included and updated. **Without `--fresh`, rows are
appended** and old runs (even from previous days) accumulate. Use `--fresh` only
on the *first* command of a session; a second `--fresh` command would wipe the
rows the first one just wrote.

### Re-timing existing runs (no LLM re-sampling)

If you change the STA stage (the OpenSTA TCL or the slack/coverage parsing in
`pipeline/sta.py`), the existing rows in `dataset.csv` were produced by the old
logic. `rerun_sta.py` replays OpenSTA on each run's **saved netlist + SDC** with
the current TCL and rebuilds every timing/coverage column — without re-calling
the LLM or Yosys:

```bash
.venv/bin/python scripts/rerun_sta.py
```

This is deterministic and cheap (~6 s for 60 runs). Use it instead of re-running
the whole pipeline, which would re-sample the LLM (non-deterministic) and waste
synthesis time on an unchanged netlist.

### Useful flags

| Flag | Default | Meaning |
|---|---|---|
| `--design <path>` | — | Single Verilog file to test |
| `--all` | — | Iterate every `.v` in `designs/` |
| `--seeds N [N ...]` | one run, no seed | Run the LLM with each seed (variability sampling) |
| `--period <ns>` | 5.0 | Target clock period passed to the prompt |
| `--reference` | off | Skip LLM, use `designs/reference/<name>.sdc` |
| `--no-correction` | off | Disable the LLM correction/retry loop |
| `--model <name>` | `qwen3:8b` | Override the Ollama model |
| `--compare-models` | off | Run every model in `LLM_MODELS` (config/env) on each design/seed/prompt |
| `--prompt-version <v>` | `v1_base` | Prompt template: `v1_base` or `v2_base` |
| `--compare-prompts` | off | Run every prompt version on each design/seed for comparison |
| `--with-reference` | off | Also run the control reference SDC once per design |
| `--fresh` | off | Drop `dataset.csv` before this run so new rows fully replace it |

### Running in PyCharm IDE

1. Open the project root in PyCharm.
2. Set the interpreter to `.venv/bin/python` (Settings → Project → Python Interpreter).
3. Right-click `scripts/run_pipeline.py` → Modify Run Configuration → set
   "Parameters" to e.g. `--design designs/simple.v --reference`.
4. Run.

---

## What Each Pipeline Stage Does

```
┌───────────┐   ┌────────────┐   ┌────────────┐   ┌────────────┐   ┌─────────┐
│ LLM       │──▶│ SDC clean  │──▶│ Yosys+ABC  │──▶│ OpenSTA    │──▶│ classify│
│ (Ollama)  │   │ (regex)    │   │ +liberty   │   │ +liberty   │   │ +record │
└───────────┘   └────────────┘   └────────────┘   └────────────┘   └─────────┘
      │                                                                │
      │       ◀────── correction loop on synthesis failure ────────────┘
```

1. **LLM stage** — `pipeline/llm.py` reads the selected prompt template
   (`prompts/base_v1.txt` or `prompts/base_v2.txt`, controlled by
   `--prompt-version`), inserts the Verilog source and target clock period,
   calls the local Ollama daemon, strips `<think>` reasoning tags and code
   fences, and extracts only lines beginning with valid SDC commands.

2. **Synthesis** — `pipeline/synthesis.py` writes a Yosys script that
   reads the Verilog, runs the standard passes (proc, opt, fsm, memory,
   techmap), maps to the Nangate45 liberty via `abc -liberty`, and writes
   a gate-level netlist plus stats (cell count, area).

3. **STA** — `pipeline/sta.py` writes an OpenSTA Tcl script that loads
   the liberty, the netlist, and the generated SDC, then reports
   `report_checks`, `report_wns`, `report_tns`. The log is parsed to
   extract worst negative slack, total negative slack, the minimum slack
   across all paths, and a "no paths found" flag.

4. **Classification** — `pipeline/classifier.py` assigns multi-label tags
   to the run based on what the LLM produced and what the tools said. These are
   different from `final_status`: labels explain the cause, while `final_status`
   gives the simplified end result used for high-level summaries. A run can have
   multiple labels in `all_labels`; `primary_label` is the highest-priority label.

   | Label | Meaning | Typical cause / interpretation |
   |---|---|---|
   | `empty_output` | The LLM returned no usable text. | Model call produced an empty response. |
   | `no_valid_sdc_lines` | The LLM responded, but no cleaned line started with a supported SDC command. | Markdown, explanations, malformed commands, or non-SDC text dominated the response. |
   | `missing_create_clock` | Valid SDC lines exist, but no `create_clock` or `create_generated_clock` was found. | The timing environment is incomplete; STA may not form meaningful paths. |
   | `syntax_error` | Yosys/OpenSTA reported an SDC syntax or command error. | Bad option, invalid command, wrong argument count, parse error, or invalid value. |
   | `port_reference_error` | A constraint referenced a port/object the tool could not find. | The LLM invented or misspelled a Verilog port name. |
   | `non_numeric_value` | A clock period or delay value was not numeric. | Example: `-period five` or a placeholder delay string. |
   | `synthesis_failure` | Yosys did not complete successfully. | RTL/tool failure or a synthesis-stage issue; currently correction only triggers on this class. |
   | `sta_failure` | OpenSTA failed to complete timing analysis. | Usually malformed SDC or invalid objects/options during `read_sdc` or timing setup. |
   | `timing_violation` | STA ran and found timing paths, but at least one path had negative slack. | The generated constraints are valid enough to analyze, but timing is not met. |
   | `ineffective_constraint` | STA ran but reported no timing paths. | The SDC is syntactically accepted but does not define a useful launch/capture timing relationship. |
   | `accepted` | Synthesis and STA passed, timing paths exist, and timing is met. | Fully successful run. |
   | `accepted_partial` | The run passed timing despite minor classification labels. | Reserved for usable-but-imperfect constraint sets. |

   The simplified `final_status` field is assigned later in
   `pipeline/orchestrator.py`:

   | Final status | Meaning | Closest classification label |
   |---|---|---|
   | `ok` | Synthesis passed, STA passed, timing met, **and all required path groups (in2reg, reg2out) were actually analyzed.** | `accepted` |
   | `partial_coverage` | Synthesis and STA passed and timing is met, **but the I/O timing environment was never fully constrained** — typically only reg2reg paths were analyzed because `set_input_delay` / `set_output_delay` lacked a `-clock` association. The "pass" is only earned on the subset of paths that were timed. | `incomplete_timing_coverage` |
   | `timing_violation` | Synthesis and STA passed, but timing failed. | `timing_violation` |
   | `ineffective_no_paths` | Synthesis and STA passed, but STA found no timing paths. | `ineffective_constraint` |
   | `sta_failed` | Synthesis passed, but OpenSTA failed. | `sta_failure` |
   | `synth_failed` | Yosys synthesis failed, so STA was skipped. | `synthesis_failure` |

   **Why `partial_coverage` matters:** OpenSTA silently skips unconstrained ports
   rather than erroring, so a constraint set that forgets to clock-associate its
   I/O delays still reports clean timing — but only on register-to-register
   paths, which are timed by `create_clock` alone and essentially cannot fail.
   The pipeline therefore parses every reported path into a *path group*
   (`in2reg`, `reg2out`, `reg2reg`, `in2out`, `async`) and compares observed
   coverage against what the design structurally requires. A run is only `ok`
   when the input→register and register→output groups are genuinely analyzed.

5. **Recording** — `pipeline/dataset.py` appends one row to
   `results/dataset.csv` with everything the report needs.

---

## Configuration

`pipeline/config.py` is the single source of truth for paths and defaults.
Most things can also be overridden via environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `OPENSTA_BIN` | `~/tools/OpenSTA/build/sta` | Path to the OpenSTA binary |
| `YOSYS_BIN` | `yosys` (PATH) | Path to Yosys |
| `LLM_MODEL` | `qwen3:8b` | Ollama model name (single-model runs) |
| `LLM_MODELS` | `qwen3:8b` | Comma-separated model list for `--compare-models`, e.g. `qwen3:8b,qwen3:4b` |

Constants tuned in `config.py`:

- `DEFAULT_CLOCK_PERIOD_NS = 5.0`
- `MAX_CORRECTION_ATTEMPTS = 3`
- `LLM_TIMEOUT_S = 180`

---

## Dataset Schema (`results/dataset.csv`)

One row per pipeline run. Key columns:

| Column | Type | Meaning |
|---|---|---|
| `run_id` | str | Unique identifier, also the run-dir name |
| `design` | str | Design module name |
| `llm_model` | str | Model used, or `reference` for control runs |
| `seed` | int | LLM sampling seed (blank for reference / single runs) |
| `clock_period_ns` | float | Effective period (from SDC or fallback) |
| `correction_attempts` | int | How many retries the correction loop ran |
| `final_status` | enum | `ok`, `partial_coverage`, `timing_violation`, `ineffective_no_paths`, `sta_failed`, `synth_failed` |
| `primary_label` | enum | Top classification label |
| `all_labels` | str | `\|`-separated list of all matched labels |
| `has_create_clock` | bool | Whether the SDC contains `create_clock` |
| `n_input_delays` / `n_output_delays` | int | Counts of those SDC commands |
| `llm_duration_s` | float | LLM generation time, incl. correction retries (0 for reference) |
| `cells_total` | int | Synthesized cell count |
| `chip_area` | float | Total area (Nangate45 units) |
| `wns_ns` / `tns_ns` | float | Worst / total negative slack |
| `min_slack_ns` | float | Minimum slack across all paths (positive = margin) |
| `timing_met` | bool | Whether all paths meet timing |
| `no_paths` | bool | Whether STA found no timing paths (ineffective SDC) |
| `setup_violations` | int | Count of paths with negative slack |
| `n_paths_total` | int | Total timing paths OpenSTA reported |
| `n_in2reg` | int | Paths from an input port to a register (data setup) |
| `n_reg2out` | int | Paths from a register to an output port |
| `n_reg2reg` | int | Paths between registers (timed by `create_clock` alone) |
| `n_in2out` | int | Pure combinational input→output paths |
| `n_async` | int | Reset recovery/removal checks |
| `wns_in2reg` | float | Worst slack among in2reg paths (blank if none timed) |
| `wns_reg2out` | float | Worst slack among reg2out paths (blank if none timed) |
| `wns_reg2reg` | float | Worst slack among reg2reg paths (blank if none timed) |
| `coverage_complete` | bool | Whether the required I/O path groups were actually analyzed |
| `run_dir` | path | Folder with full per-run artifacts (incl. `coverage.txt`) |

---

## Results so far

As of June 2026 the dataset holds 186 runs (6 reference + 5 models × 36). The
headline findings:

- **The prompt fix is universal.** Every model collapses to ~0% success on
  `v1_base` and jumps to 89–100% on `v2_base`. The only difference is `v2_base`
  requiring `-name` on the clock and `-clock` on every I/O delay. Without it,
  runs land in `partial_coverage` (clean timing, but only on reg2reg paths) —
  confirming **tool acceptance alone is not a valid success criterion**.
- **Speed spans two orders of magnitude** (v2_base, mean LLM time per run):

  | Model | v2 ok% | avg LLM s |
  |---|---|---|
  | `granite4.1:8b` | 100% | 5.4 |
  | `granite4.1:3b` | 89% | 2.3 |
  | `gemma4:e2b` | 100% | 17.8 |
  | `qwen3:8b` | 100% | 87.1 |
  | `gemma4:12b` | 94% | 292.6 |

  Granite (built for structured output) reaches 100% ~16× faster than qwen3:8b.
  The *effective-2B* `gemma4:e2b` beats the full `gemma4:12b` on both axes.
- **Intrinsic knowledge vs. prompting.** Only `gemma4:12b` gets any `ok` runs on
  `v1_base` — it spontaneously writes `-name`/`-clock` unprompted. The others
  only do so when `v2_base` reminds them.

**Netlist invariance and per-group slack.** Cells/area match the reference for
every run because synthesis is currently driven only by the clock period — the
SDC's I/O delays cannot change the circuit, only its timing analysis (feeding
constraints into the mapper is a future step, see below). What *does* vary is the
worst slack per path group, which exposes each model's chosen I/O delay (smaller
delay → more slack). `reg2reg` is identical everywhere (timed by `create_clock`
alone); `in2reg`/`reg2out` track the delay choice — `qwen3:8b` matches the
reference exactly (0 ns delays), `granite4.1:8b` is the most conservative:

  | adder (v2) | in2reg | reg2out | counter (v2) | in2reg | reg2out | reg2reg |
  |---|---|---|---|---|---|---|
  | reference | +3.72 | +4.42 | reference | +4.27 | +4.39 | +4.66 |
  | qwen3:8b | +3.72 | +4.42 | qwen3:8b | +4.27 | +4.39 | +4.66 |
  | granite4.1:3b | +3.22 | +3.92 | granite4.1:3b | +3.05 | +2.89 | +4.66 |
  | granite4.1:8b | +2.22 | +2.92 | granite4.1:8b | +4.27 | +4.39 | +4.66 |
  | gemma4:e2b | +3.22 | +3.92 | gemma4:e2b | +3.77 | +3.89 | +4.66 |
  | gemma4:12b | +3.22 | +3.92 | gemma4:12b | +3.77 | +3.89 | +4.66 |

## Future steps

- Extend the correction loop to fire on `partial_coverage`,
  `ineffective_no_paths` and `timing_violation`, not just synthesis failure.
- Add harder designs (multiple clocks, generated clocks, false/multicycle paths).
- Run a larger statistical batch (5–10+ seeds) on the fastest accurate models.
- Explore SDC-aware synthesis (feed constraints into the mapper) so the harder
  question — can the LLM *safely relax* timing to cut area without breaking
  function — becomes measurable.
