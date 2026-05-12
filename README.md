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
│   ├── base.txt             initial constraint generation prompt
│   └── correction.txt       retry prompt fed with the failing SDC + tool error
│
├── libs/
│   └── Nangate45_typ.lib    Nangate 45 nm open standard cell library
│                            (used by Yosys/ABC for tech mapping & by OpenSTA)
│
├── scripts/
│   └── run_pipeline.py    CLI entry point (argparse, runs single or batch)
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
| **Ollama** | local daemon | Serves the LLM |
| **Qwen3** | `qwen3:8b` (5.2 GB) | The local LLM that produces SDC |
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

### 3. Pull the LLM model into Ollama

```bash
ollama pull qwen3:8b
```

Different models are configurable via the `LLM_MODEL` env var or the
`--model` CLI flag. Anything Ollama supports works.

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

1. **LLM stage** — `pipeline/llm.py` reads `prompts/base.txt`, inserts the
   Verilog source and target clock period, calls the local Ollama daemon,
   strips `<think>` reasoning tags and code fences, and extracts only lines
   beginning with valid SDC commands.

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
   to the run based on what the LLM produced and what the tools said.
   Labels: `empty_output`, `no_valid_sdc_lines`, `missing_create_clock`,
   `syntax_error`, `port_reference_error`, `non_numeric_value`,
   `synthesis_failure`, `sta_failure`, `timing_violation`,
   `ineffective_constraint`, `accepted`, `accepted_partial`.

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
| `LLM_MODEL` | `qwen3:8b` | Ollama model name |

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
| `final_status` | enum | `ok`, `timing_violation`, `ineffective_no_paths`, `sta_failed`, `synth_failed` |
| `primary_label` | enum | Top classification label |
| `all_labels` | str | `\|`-separated list of all matched labels |
| `has_create_clock` | bool | Whether the SDC contains `create_clock` |
| `n_input_delays` / `n_output_delays` | int | Counts of those SDC commands |
| `cells_total` | int | Synthesized cell count |
| `chip_area` | float | Total area (Nangate45 units) |
| `wns_ns` / `tns_ns` | float | Worst / total negative slack |
| `min_slack_ns` | float | Minimum slack across all paths (positive = margin) |
| `timing_met` | bool | Whether all paths meet timing |
| `no_paths` | bool | Whether STA found no timing paths (ineffective SDC) |
| `setup_violations` | int | Count of paths with negative slack |
| `run_dir` | path | Folder with full per-run artifacts |

---

## Future steps

- Extend the correction loop to fire on `ineffective_no_paths` and
  `timing_violation`, not just synthesis failure.
- Run a larger statistical batch (10+ seeds per design).
- Add an analysis script: success rate tables, error heatmaps, slack
  distributions.
- Compare prompt versions (`v1_base` vs. an examples-augmented `v2`).
- Compare llm models.
