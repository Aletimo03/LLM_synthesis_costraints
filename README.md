# LLM Synthesis Constraints — Validation Framework

A pipeline that uses Large Language Models (five local models plus one hosted
model, all served through Ollama) to generate Synopsys Design Constraints (SDC)
for Verilog ASIC designs, then validates them through a full Yosys synthesis +
OpenSTA static timing analysis flow.

Each run records errors, classification labels and timing metrics into a master
CSV dataset, so the reliability of LLM-generated constraints can be studied
quantitatively. Constraints are also fed back to the model when the tools reject
or under-cover them, which makes *self-repair* measurable alongside generation.
Note the netlist is invariant across runs (the SDC never reaches synthesis), so
area and cell count are a fairness check rather than a score — see
[Results](#results).

---

## Project Goals

1. **Generate** SDC constraints from natural-language prompts using an LLM.
2. **Validate** the constraints by running them through real EDA tools.
3. **Classify** failures: syntax errors, port-reference errors, non-numeric
   values, missing `create_clock`, ineffective constraints (no timing paths),
   timing violations.
4. **Analyze timing impact**: compare per-path-group slack and coverage against
   the reference constraints (cells/area are invariant and serve as a control).
5. **Self-correct** failing constraints with an escalating feedback ladder
   (tool symptom → object dictionary) that doubles as a diagnostic — see
   [Correction ladder](#correction-ladder) and
   [docs/correction_loop_plan.md](docs/correction_loop_plan.md).

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
│   │                        --- advanced designs (force non-trivial SDC) ---
│   ├── async_in.v           async input through a 2-flop sync (set_false_path)
│   ├── cdc_sync.v           two-clock CDC synchronizer (set_clock_groups)
│   ├── clkdiv.v             ÷2 clock divider (create_generated_clock)
│   ├── mult_mcp.v           multi-cycle 16×16 multiply (set_multicycle_path)
│   └── reference/         known-good SDC per design — used as QoR control
│
├── prompts/               LLM prompt templates
│   ├── base_v1.txt          original prompt (v1_base) — missing -name / -clock
│   ├── base_v2.txt          revised prompt (v2_base) — adds -name and -clock requirements
│   └── correction.txt       retry prompt (minimal-edit rules) + failing SDC + error
│
├── libs/
│   └── Nangate45_typ.lib    Nangate 45 nm open standard cell library
│                            (used by Yosys/ABC for tech mapping & by OpenSTA)
│
├── scripts/
│   ├── run_pipeline.py    CLI entry point (argparse, runs single or batch)
│   ├── analyze_results.py summary tables (headline, coverage, correction, slack)
│   ├── rerun_sta.py       replay OpenSTA on saved netlists, rebuild dataset
│   └── relabel_dataset.py re-derive classifier labels from saved artifacts
│
├── docs/
│   └── correction_loop_plan.md  design + evidence behind the correction ladder
│
├── project_status.tex     formal status report (LaTeX)
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
| **Models** | `qwen3:8b`, `granite4.1:3b/8b`, `gemma4:e2b/e4b` (local) + `minimax-m3:cloud` (cloud) | LLMs compared across families & sizes (set in `LLM_MODELS`); the cloud model runs on Ollama's hosted endpoint, zero local RAM |
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
.venv/bin/python scripts/run_pipeline.py --all --seeds 1 2 3 4 5
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
# every design, seeds 1-5, both prompt versions
.venv/bin/python scripts/run_pipeline.py --all --seeds 1 2 3 4 5 --compare-prompts

# or a single prompt version explicitly
.venv/bin/python scripts/run_pipeline.py --design designs/adder.v --prompt-version v2_base
```

### Full experiment in one go (fresh dataset: reference + both prompts, 3 seeds)

```bash
# one command rebuilds the entire dataset from scratch (needs ollama serve):
#   --fresh           drop the old dataset.csv first
#   --with-reference  re-run the control baseline for every design
#   --compare-prompts run both v1_base and v2_base
.venv/bin/python scripts/run_pipeline.py --all --seeds 1 2 3 4 5 \
    --compare-prompts --with-reference --fresh

# multi-model version: every model in LLM_MODELS runs the full matrix
# (reference is model-independent, so it runs once, on the first pass)
LLM_MODELS="qwen3:8b,granite4.1:3b" .venv/bin/python scripts/run_pipeline.py \
    --all --seeds 1 2 3 4 5 --compare-prompts --with-reference --fresh --compare-models

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

This is deterministic and cheap (~0.1 s per run, so well under a minute for the
whole 370-run dataset). Use it instead of re-running
the whole pipeline, which would re-sample the LLM (non-deterministic) and waste
synthesis time on an unchanged netlist.

### Useful flags

| Flag | Default | Meaning |
|---|---|---|
| `--design <path>` | — | Single Verilog file to test |
| `--all` | — | Iterate every `.v` in `designs/` |
| `--seeds N [N ...]` | one run, no seed | Run the LLM with each seed (variability sampling; the reported dataset uses 1–5) |
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
      │  ◀── correction ladder: up to 3 levels on any final_status ≠ ok ┘
```

### Correction ladder

The retry runs **after classification**, not between synthesis and STA, so it can
react to the verdict that actually occurs. It fires on **any** `final_status != ok`
and escalates through up to two levels (`MAX_CORRECTION_ATTEMPTS = 2`, override
with `--max-corrections`, disable with `--no-correction`; skipped for reference
runs and for prompt versions outside `CORRECTION_PROMPT_VERSIONS`). Each level re-feeds the **latest** SDC and its error with cumulatively
stronger help — and the level at which a run gets repaired is itself the
diagnosis:

| Level | Feedback adds | A repair here means the failure was… |
|---|---|---|
| 1 | the tool's error + the model's own previous SDC | a robustness slip |
| 2 | + the object dictionary: real port names, `[get_nets]`-able internal nets, `[all_registers]`, and that RTL pin names do not survive synthesis | namespace-limited |

At this depth **nothing derived from the reference SDC ever reaches the model** —
the dictionary is drawn from the design's own Verilog, which the model already
sees. Correction is also spent only where failures are real capability gaps:
`config.CORRECTION_PROMPT_VERSIONS` restricts it to `v2_base`, so `v1_base` runs
generate but never retry.

The symptom is the tools' own words wherever a tool complained (`sta_failed`:
OpenSTA lines naming `generated.sdc`; `synth_failed`: Yosys log tail) and a
precise derived statement where the tools stay silent (`partial_coverage`: which
ports went untimed; `timing_violation`: the violating paths by group, quoted from
`coverage.txt`). The correction prompt enforces **minimal edits** — keep working
lines identical, add nothing unless required, invent no options — because the
first symptom-only sweep showed repairs are small edits while regressions are
expansions (8% repaired vs 26% regressed, dominated by invented pin names and
hallucinated flags). **Nothing derived from the reference SDC reaches the model at
any level** — the dictionary is built from the design's own Verilog, which the
model already sees — so this measures self-repair rather than hint-following.

Each level is evaluated in its own `correction_<n>/` directory (with the exact
`feedback.txt` it saw), so every attempt stays comparable. Recorded columns:
`initial_status`, `correction_path` (e.g. `sta_failed>sta_failed>ok`),
`corrected`, `best_status` (best verdict reached at any level — the final one can
be worse), `final_status`; the remaining metrics describe the **final** state.
`analyze_results.py` reports a *Repair Ladder* table (repairs per level / never,
by model and by design).

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
   | `synthesis_failure` | Yosys did not complete successfully. | RTL/tool failure. Never observed in practice: the SDC is not an input to synthesis, so constraints cannot break it. |
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
| `LLM_MODELS` | `qwen3:8b,granite4.1:3b,granite4.1:8b,gemma4:e2b,gemma4:e4b,minimax-m3:cloud` | Comma-separated model list for `--compare-models` |

Constants tuned in `config.py`:

- `DEFAULT_CLOCK_PERIOD_NS = 5.0` — target period unless overridden per design
- `DESIGN_PERIODS = {"mult_mcp": 1.5}` — per-design period override. `mult_mcp`
  runs at 1.5 ns because the multiply's single-cycle delay is ~1.73 ns: below
  that period one cycle genuinely violates, so `set_multicycle_path 2` is
  load-bearing instead of decorative (at 5 ns it would pass either way).
- `MAX_CORRECTION_ATTEMPTS = 2` — depth of the correction ladder, see
  [Correction ladder](#correction-ladder)
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
| `correction_attempts` | int | How many ladder levels ran (0–2; always 0 for `v1_base`) |
| `initial_status` | enum | `final_status` of the first attempt, before any correction |
| `corrected` | bool | The correction turned a non-`ok` first attempt into `ok` |
| `correction_path` | str | Per-level verdicts, e.g. `sta_failed>sta_failed>ok` (empty if no level ran) |
| `best_status` | enum | Best verdict reached at any level (`final_status` may be worse) |
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

## Results

As of July 2026 the dataset holds **610 runs**: 10 reference plus a complete
factorial of **6 models × 10 designs × 5 seeds × 2 prompt versions** (600 LLM
runs, no cell missing or duplicated). Correction is spent only on `v2_base`
failures, so `v1_base` figures are pure generation.

| Model | ok v1 | ok v2 | ok v2 *after correction* | gain |
|---|---|---|---|---|
| `gemma4:e2b` | 10% | 80% | 80% | +0 |
| `gemma4:e4b` | 18% | 74% | 78% | +4 |
| `granite4.1:3b` | 2% | 58% | 68% | +10 |
| `granite4.1:8b` | 0% | 70% | 82% | +12 |
| `qwen3:8b` | 2% | 78% | 92% | +14 |
| `minimax-m3:cloud` | **80%** | 80% | **98%** | +18 |
| **all models** | **19%** | **73%** | **83%** | +10 |

Reference control: 100% (10/10). A run is `ok` only when synthesis and STA pass,
timing is met, **and** every required path group was actually analyzed.

### Key findings

- **Tool acceptance is not a valid success criterion.** STA silently skips
  unconstrained ports rather than erroring, so a constraint set that forgets
  `-clock` on its I/O delays still reports clean timing — earned entirely on
  reg2reg paths, which are timed by `create_clock` alone and essentially cannot
  fail. Under `v1_base` this `partial_coverage` pattern is the dominant failure
  (25 of 50 runs for `granite4.1:8b`). Verifying path-group coverage is what
  makes every other number here meaningful.

- **Prompt design dominates everything else.** v1→v2 lifts the aggregate from
  19% to 73%; the whole two-level correction ladder adds a further 10 points.
  The entire v1/v2 difference is two structural requirements: `-name` on the
  clock, `-clock` on every I/O delay (values deliberately left to the model).

- **Generating and self-repairing are independent abilities.** `gemma4:e2b` and
  `minimax-m3:cloud` both generate at exactly 80% on `v2_base` — after
  correction one is unchanged and the other reaches 98%. Ranking models on
  one-shot accuracy would call them equivalent.

- **Capable models repair; small models actively regress.** Of the 80 runs that
  entered the loop, 29 (36%) were repaired and 22 (28%) came out *strictly
  worse* — and the split tracks model strength almost perfectly:

  | Model | entered | repaired | regressed | unchanged |
  |---|---|---|---|---|
  | `minimax-m3:cloud` | 10 | **9** | **0** | 1 |
  | `qwen3:8b` | 11 | **7** | **0** | 3 |
  | `granite4.1:8b` | 15 | 6 | 3 | 6 |
  | `granite4.1:3b` | 21 | 5 | **8** | 8 |
  | `gemma4:e4b` | 13 | 2 | **6** | 3 |
  | `gemma4:e2b` | 10 | **0** | **5** | 5 |

  21 of the 22 regressions turned an SDC the tool had *accepted* into one it
  rejects: shown its own error, a weak model rewrites too aggressively —
  stripping `[get_ports …]`, inventing flags — and destroys working syntax.
  **A feedback loop is not uniformly beneficial; below a capability threshold it
  is harmful.** The aggregate `ok%` cannot show this, since a run that already
  passed never enters the loop.

- **Feedback quality decides whether correction helps or harms.** A first design
  returning only the raw tool symptom repaired 8% while regressing 26%. Adding
  minimal-edit rules, precise symptoms (which ports, which violating group) and
  an object dictionary raised the repair rate to **36%** on the same models and
  designs.

- **The residual failures are naming, not comprehension.** `clkdiv` is the
  sharpest case: **no model on any seed solves it first try (0/30)**, yet the
  object dictionary alone rescues 13 of 30. Models know they need
  `create_generated_clock`; they cannot name the target, because RTL signal
  names do not survive synthesis:

  ```tcl
  attempt 0  create_generated_clock -divide_by 2 -name clk_div [get_ports clk]
  level 1    ... -source clk -name clk_div [get_ports clk]        # -source fixed, wrong object
  level 2    ... -source [get_ports clk] -name clk_div [get_nets clk_div]   # ok
  ```

  `mult_mcp` is the same story further out: 26 of 30 runs emit a
  `set_multicycle_path` at some point, but only 6 pass — the command is known,
  applying it to the right objects is not.

- **Failure modes differ qualitatively by model.** `syntax_error` — an invented
  option such as `-edge`, `-relative`, `-add_shift` — is the dominant `v1_base`
  failure for every local model (7–21 runs each). `minimax-m3:cloud` produces
  **zero** under either prompt: it never hallucinates a flag.

- **Speed spans ~35×** (v2 first-shot generation): `granite4.1:3b` 3.1 s,
  `minimax-m3:cloud` 7.3 s, `granite4.1:8b` 7.6 s, `gemma4:e4b` 11.6 s,
  `gemma4:e2b` 22.3 s, `qwen3:8b` 115.2 s. Correction adds its own cost, tracked
  separately in `llm_correction_s`.

### Per-design outcome (v2_base, 30 runs each)

| Design | first-shot | after | | Design | first-shot | after |
|---|---|---|---|---|---|---|
| `adder` | 30/30 | 30/30 | | `async_in` | 24/30 | **30/30** |
| `alu` | 30/30 | 30/30 | | `cdc_sync` | 20/30 | 23/30 |
| `counter` | 30/30 | 30/30 | | `clkdiv` | **0/30** | **14/30** |
| `fsm` | 30/30 | 30/30 | | `mult_mcp` | **0/30** | 6/30 |
| `shift_reg` | 29/30 | 29/30 | | `simple` | 27/30 | 27/30 |

Six of ten designs are solved perfectly first-shot by every model; everything
residual sits in the four added to break the saturation.

### Netlist invariance and slack calibration

Cells/area are identical to the reference for **all 600 runs** — the SDC reaches
only the timing analyzer, never synthesis. So no run can be judged on area, and
**slack magnitude is not a merit**: a value *above* the reference just means a
looser, under-constrained I/O budget. What slack does reveal is calibration.
Cells below are `median [min,max]` across seeds; a single value means every seed
agreed. `minimax-m3:cloud` picks one delay and holds it, sitting at the
reference; the smallest models scatter run to run.

| v2_base | adder in2reg | adder reg2out | counter in2reg | counter reg2out |
|---|---|---|---|---|
| reference | +3.72 | +4.42 | +4.27 | +4.39 |
| `minimax-m3:cloud` | +3.72 [+3.22,+3.72] | +4.42 [+3.92,+4.42] | **+4.27** | **+4.39** |
| `qwen3:8b` | +3.72 [+3.72,+4.22] | +4.42 [+4.42,+4.92] | +4.27 [+4.27,+4.77] | +4.39 [+4.39,+4.89] |
| `granite4.1:8b` | +3.82 [+2.22,+3.82] | +4.52 [+2.92,+4.62] | +4.77 [+4.27,+4.77] | +4.89 [+4.39,+4.89] |
| `granite4.1:3b` | +3.72 [+2.22,+3.92] | +3.92 [+2.92,+4.62] | +3.05 [+3.05,+4.05] | +3.89 [+2.89,+4.59] |
| `gemma4:e2b` | +3.22 [+3.22,+4.22] | +3.92 [+3.92,+4.92] | +4.77 [+3.77,+4.77] | +4.89 [+3.89,+4.89] |
| `gemma4:e4b` | +3.72 [+3.22,+3.92] | +4.42 [+3.92,+4.62] | +4.77 [+3.77,+4.77] | +4.89 [+3.89,+4.89] |

`reg2reg` is identical everywhere (timed by `create_clock` alone), including the
shared `mult_mcp` violation at −0.27 where no model supplied the exception.

## Future steps

- **Run the correction experiment.** The loop is implemented but the dataset
  predates it, so no run has exercised it yet. Re-running the sweep produces the
  `initial_status → final_status` transition per failure class, which separates
  *robustness slips* (fixable from the error text) from *genuine knowledge gaps*
  — and shows whether self-repair closes the `clkdiv`/`mult_mcp` gap no model
  solves first-shot. Then extend `analyze_results.py` with a transition view.
- **Phase 2: targeted feedback** — condition the hint on each model's
  characteristic failure mode and compare against the uniform feedback above.
  Design in [docs/correction_loop_plan.md](docs/correction_loop_plan.md).
- Make synthesis SDC-driven (feed constraints to the mapper) so the netlist
  actually responds to constraint quality, with a two-SDC sign-off (build with
  the model's SDC, sign off against the reference) so under-constraining cannot
  masquerade as a QoR win.
- Run a larger statistical batch (5–10+ seeds) on the fastest accurate models.
- Explore SDC-aware synthesis (feed constraints into the mapper) so the harder
  question — can the LLM *safely relax* timing to cut area without breaking
  function — becomes measurable.
