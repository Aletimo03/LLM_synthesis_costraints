"""Summarize the experiment dataset, comparing prompts and models side by side.

Every table is organized around the prompt version (v1_base vs v2_base) with one
column per model inside each version, so a single table shows both axes at once:
whether an outcome is driven by the *prompt*, the *model*, or the *design*.
Reference (control) runs are shown alongside as the QoR baseline.

Usage:
    .venv/bin/python scripts/analyze_results.py
    .venv/bin/python scripts/analyze_results.py --model qwen3:8b
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline import config  # noqa: E402

REFERENCE = "reference"

# Final-status values in a fixed, meaningful order (best -> worst).
STATUS_ORDER = [
    "ok",
    "partial_coverage",
    "timing_violation",
    "ineffective_no_paths",
    "sta_failed",
    "synth_failed",
]

# (label, csv column) for the three timing path groups that carry QoR meaning.
GROUPS = [
    ("in2reg", "wns_in2reg"),
    ("reg2out", "wns_reg2out"),
    ("reg2reg", "wns_reg2reg"),
]

# Column geometry for the grouped (version x model) tables.
LABEL_W = 22   # leading label column (incl. the 2-space indent)
CELL_W = 7     # per-model data cell
VSEP = " │"    # vertical separator between prompt-version blocks


def _block(cells: list[str], cell_w: int = CELL_W) -> str:
    """One version's worth of right-aligned model cells, as a single string."""
    return "".join(f"{c:>{cell_w}}" for c in cells)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Dataset not found: {path}")
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(part: int, total: int) -> str:
    return "n/a" if total == 0 else f"{part / total * 100:.0f}%"


def _is_true(value) -> bool:
    return str(value).strip().lower() == "true"


def _gen_status(r: dict) -> str:
    """The verdict on what the model generated *first*, before any correction.

    Correction only runs on some prompt versions, so `final_status` is not
    comparable across versions: a v2 row may include up to two repair attempts
    while a v1 row is pure generation. Anything measuring *generation* quality
    must use this instead, or a prompt effect and a correction effect get
    silently added together. Rows written before the correction loop existed
    have no initial_status, so fall back to final_status.
    """
    return r.get("initial_status") or r.get("final_status", "")


def _slack(value) -> str:
    """Format a worst-slack cell: a number, or '—' when the group was never timed."""
    f = _to_float(value)
    return "—" if f is None else f"{f:+.2f}"


def _abbr(model: str) -> str:
    """Compact column label for a model name (keeps tables narrow)."""
    return (model.replace("granite4.1:", "gr")
                 .replace("gemma4:", "g")
                 .replace("qwen3-coder:", "qc")
                 .replace("qwen3:", "q")
                 .replace("minimax-m3:cloud", "mmx")
                 .replace("kimi-k2.7-code:cloud", "kimi"))


def _hr(title: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'=' * 76}")


def _grouped_header(label: str, versions: list[str], models: list[str],
                    with_ref: bool, cell_w: int = CELL_W) -> None:
    """Two-row header: prompt versions spanning a block of per-model columns."""
    span = cell_w * len(models)
    line1 = " " * LABEL_W + (f"{'':>{cell_w}}" if with_ref else "")
    line1 += VSEP.join(f"{v:^{span}}" for v in versions)
    line2 = f"  {label:<{LABEL_W - 2}}" + (f"{'ref':>{cell_w}}" if with_ref else "")
    line2 += VSEP.join(_block([_abbr(m) for m in models], cell_w) for _ in versions)
    print(line1.rstrip())
    print(line2)


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #
def _table_summary(refs, models, versions, vm_rows, total) -> None:
    _hr("Dataset Summary")
    print(f"  {'total runs':<20}{total:>5}")
    print(f"  {'reference':<20}{len(refs):>5}")
    print(f"  {'models':<20}{len(models):>5}   {', '.join(models)}")
    designs = sorted({r["design"] for rows in vm_rows.values() for r in rows})
    print(f"  {'designs':<20}{len(designs):>5}   {', '.join(designs)}")
    for v in versions:
        n = sum(len(vm_rows[(v, m)]) for m in models)
        print(f"  {v:<20}{n:>5}   ({len(models)} models)")


def _corrected_rows(llm_rows: list[dict]) -> list[dict]:
    """Runs where the correction loop actually fired."""
    return [r for r in llm_rows
            if str(r.get("correction_attempts", "0")).strip() not in ("", "0")
            and r.get("initial_status") and r.get("final_status")]


def _short_ver(v: str) -> str:
    """'v2_base' -> 'v2' for compact column headings."""
    return v.split("_")[0] if v else v


def _table_headline(refs, models, versions, vm_rows) -> None:
    """The three numbers that carry the whole experiment, one row per model.

    Success on each prompt version as *generated*, then again after the
    correction loop for the version(s) that get one. Read left to right: the
    jump between versions is the prompt effect, the jump to the 'after' column
    is the self-repair effect. Keeping them in separate columns is what stops a
    prompt gain and a repair gain from being silently added together.
    """
    _hr("Headline  (share of runs reaching ok)")
    # Which versions actually had a retry, so 'after' is only shown where it means
    # something rather than repeating the generated figure.
    corrected_versions = [v for v in versions
                          if any(_corrected_rows(vm_rows[(v, m)]) for m in models)]
    heads = [f"ok {_short_ver(v)}" for v in versions]
    heads += [f"ok {_short_ver(v)} after" for v in corrected_versions]
    print(f"  {'model':<18}" + "".join(f"{h:>14}" for h in heads) + f"{'gain':>8}")

    def rate(rows, key):
        return _pct(sum(1 for r in rows if key(r) == "ok"), len(rows))

    for m in models + ["ALL"]:
        pick = (lambda v: [r for mm in models for r in vm_rows[(v, mm)]]) if m == "ALL" \
            else (lambda v: vm_rows[(v, m)])
        cells = [rate(pick(v), _gen_status) for v in versions]
        after = [rate(pick(v), lambda r: r["final_status"]) for v in corrected_versions]
        # gain attributable to correction, on the last corrected version
        gain = ""
        if corrected_versions:
            v = corrected_versions[-1]
            rows = pick(v)
            if rows:
                b = sum(1 for r in rows if _gen_status(r) == "ok")
                a = sum(1 for r in rows if r["final_status"] == "ok")
                gain = f"{(a - b) / len(rows) * 100:+.0f}"
        label = m if m != "ALL" else "— all models —"
        print(f"  {label:<18}" + "".join(f"{c:>14}" for c in cells + after)
              + f"{gain:>8}")
    if refs:
        ref_ok = sum(1 for r in refs if r["final_status"] == "ok")
        print(f"\n  reference (control): {_pct(ref_ok, len(refs))} ok "
              f"({ref_ok}/{len(refs)} designs)")
    print("  'after' columns exist only for "
          f"{'/'.join(corrected_versions) or 'no version'} — the retry runs there only.")


def _table_status(refs, models, versions, vm_rows) -> None:
    """Generation-quality status counts: reference + one column per (version, model).

    Reports the *generated* verdict (pre-correction) so the prompt versions stay
    comparable; the correction tables below report what the retries changed.
    """
    _hr("Generated Status  (counts, BEFORE correction; one column per version x model)")
    ref_counts = Counter(r["final_status"] for r in refs)
    vm_counts = {k: Counter(_gen_status(r) for r in rows) for k, rows in vm_rows.items()}

    _grouped_header("status", versions, models, with_ref=True)
    for status in STATUS_ORDER:
        ref_n = ref_counts.get(status, 0)
        row = f"  {status:<{LABEL_W - 2}}{(ref_n or '·'):>{CELL_W}}"
        row += VSEP.join(_block([(vm_counts[(v, m)].get(status, 0) or '·') for m in models])
                         for v in versions)
        print(row)

    # headline success rate (ok) per (version, model), first-shot
    def ok_rate(v, m):
        rows = vm_rows[(v, m)]
        return _pct(sum(1 for r in rows if _gen_status(r) == "ok"), len(rows))
    row = f"\n  {'first-shot ok':<{LABEL_W - 2}}{_pct(ref_counts.get('ok', 0), len(refs)):>{CELL_W}}"
    row += VSEP.join(_block([ok_rate(v, m) for m in models]) for v in versions)
    print(row)

    # and the same after correction, so the delta is visible in one place
    def ok_after(v, m):
        rows = vm_rows[(v, m)]
        return _pct(sum(1 for r in rows if r["final_status"] == "ok"), len(rows))
    row = f"  {'after correction':<{LABEL_W - 2}}{_pct(ref_counts.get('ok', 0), len(refs)):>{CELL_W}}"
    row += VSEP.join(_block([ok_after(v, m) for m in models]) for v in versions)
    print(row)
    print("\n  Counts are pre-correction. 'after correction' differs only where the")
    print("  retry loop ran (see config.CORRECTION_PROMPT_VERSIONS).")


def _table_coverage(models, versions, vm_rows) -> None:
    """Coverage and timing flags (share of runs), per (version, model)."""
    _hr("Coverage & Timing Flags  (share of runs, per version x model)")
    _grouped_header("metric", versions, models, with_ref=False)

    def line(label, pred) -> None:
        def cell(v, m):
            rows = vm_rows[(v, m)]
            return _pct(sum(1 for r in rows if pred(r)), len(rows))
        row = f"  {label:<{LABEL_W - 2}}"
        row += VSEP.join(_block([cell(v, m) for m in models]) for v in versions)
        print(row)

    line("full coverage", lambda r: _is_true(r.get("coverage_complete")))
    line("timed in2reg", lambda r: _to_float(r.get("n_in2reg")) not in (None, 0))
    line("timed reg2out", lambda r: _to_float(r.get("n_reg2out")) not in (None, 0))
    line("timing met", lambda r: _is_true(r.get("timing_met")))
    line("no paths (ineff.)", lambda r: _is_true(r.get("no_paths")))


def _table_per_design(models, versions, vm_rows) -> None:
    """For each design x model, show the outcome mix under each prompt version.

    Lets you separate design-driven failures (bad on every prompt/model) from
    prompt- or model-driven failures.
    """
    _hr("Per-Design Outcomes  (status counts across seeds; one row per design x model)")
    designs = sorted({r["design"] for rows in vm_rows.values() for r in rows})

    def outcomes(rows) -> str:
        counts = Counter(r["final_status"] for r in rows)
        return ", ".join(f"{s}={n}" for s, n in sorted(
            counts.items(),
            key=lambda kv: STATUS_ORDER.index(kv[0]) if kv[0] in STATUS_ORDER else 99))

    vlabels = f" {VSEP} ".join(f"{v}: <outcomes>" for v in versions)
    print(f"  {'design':<11}{'model':<8}{vlabels}")
    for design in designs:
        for m in models:
            cells = []
            for v in versions:
                rows = [r for r in vm_rows[(v, m)] if r["design"] == design]
                cells.append(f"{v}: {outcomes(rows) or '-'}" if rows else f"{v}: -")
            print(f"  {design:<11}{_abbr(m):<8}" + f" {VSEP} ".join(cells))
        print()


def _table_slack(refs, models, versions, vm_rows) -> None:
    """Per-group slack range: reference + each (version, model), per design.

    Shows the min/max slack across seeds. A single number means every timed seed
    agreed (deterministic delay choice); a 'min/max' pair means the model's delay
    choice — and thus the slack — varied across seeds. '—' means the group was
    never analyzed by any seed (a coverage gap, not a passing result). Only seeds
    that actually timed the group contribute; '—' is never treated as a slack
    value, since "no path timed" is a coverage outcome, not a timing number.
    """
    _hr("Per-Group Slack  (median [min,max] across seeds; '—' = group never timed)")
    SCW = 20   # wide cell: 'median [min,max]' needs room
    designs = sorted({r["design"] for rows in vm_rows.values() for r in rows})
    ref_by_design = {r["design"]: r for r in refs}

    def spread(rows, col) -> str:
        """Median plus the observed extremes.

        Deliberately not mean +- sd: a model picks one of a few discrete I/O
        delay values, so the per-cell samples are a handful of levels rather
        than draws from a continuous distribution. A standard deviation would
        imply a smooth spread that does not exist and would bury the single
        seed that chose a wildly different value -- which is exactly the
        behaviour worth seeing. The median is robust on few-valued data, and
        [min,max] keeps the outlier visible.
        """
        vals = sorted(x for x in (_to_float(r.get(col)) for r in rows)
                      if x is not None)
        if not vals:
            return "—"
        lo, hi = vals[0], vals[-1]
        if abs(hi - lo) < 0.005:          # every seed agreed -> one number
            return f"{lo:+.2f}"
        n = len(vals)
        med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        return f"{med:+.2f} [{lo:+.2f},{hi:+.2f}]"

    # One block per prompt version rather than side by side: a 'median [min,max]'
    # cell is ~20 chars, so both versions on one line would run past 300 columns
    # and stop being readable.
    for v in versions:
        print(f"\n  --- {v} ---")
        print(f"  {'design / group':<{LABEL_W - 2}}{'ref':>{SCW}}"
              + "".join(f"{_abbr(m):>{SCW}}" for m in models))
        for design in designs:
            ref = ref_by_design.get(design, {})
            printed = False
            for label, col in GROUPS:
                ref_cell = _slack(ref.get(col, "")) if ref else "—"
                cells = [spread([r for r in vm_rows[(v, m)] if r["design"] == design], col)
                         for m in models]
                # skip groups that no run (ref or llm) ever times for this design
                if ref_cell == "—" and all(c == "—" for c in cells):
                    continue
                tag = f"{design}/{label}"
                print(f"  {tag:<{LABEL_W - 2}}{ref_cell:>{SCW}}"
                      + "".join(f"{c:>{SCW}}" for c in cells))
                printed = True
            if printed:
                print()


def _table_netlist_invariance(refs, llm_rows: list[dict]) -> None:
    """Sanity check: constraints must NOT change synthesis, so cells/area should
    equal the reference for every run. Confirms the QoR comparison is fair."""
    _hr("Netlist Invariance Check  (cells & area vs reference)")
    ref_by_design = {r["design"]: r for r in refs}
    mismatches = []
    for r in llm_rows:
        ref = ref_by_design.get(r["design"])
        if not ref:
            continue
        if (r.get("cells_total") != ref.get("cells_total")
                or r.get("chip_area") != ref.get("chip_area")):
            mismatches.append(r)
    if not mismatches:
        print(f"  OK — cells & area identical to reference for all {len(llm_rows)} runs.")
        print("  (constraints do not change synthesis; only the timing analysis differs,")
        print("   so area/cell deltas carry no information and are omitted.)")
    else:
        print(f"  {len(mismatches)} run(s) differ from reference netlist:")
        for r in mismatches:
            ref = ref_by_design[r["design"]]
            print(f"    {r['design']} seed={r.get('seed','')} model={_abbr(r.get('llm_model',''))} "
                  f"prompt={r.get('prompt_version','')}: "
                  f"cells {r.get('cells_total')} vs {ref.get('cells_total')}, "
                  f"area {r.get('chip_area')} vs {ref.get('chip_area')}")


def _table_models(models, versions, vm_rows) -> None:
    """Cross-model comparison: effectiveness and speed, per model x prompt."""
    _hr("Model Comparison  (effectiveness & speed, per model x prompt)")
    print(f"  {'model':<16}{'prompt':<11}{'runs':>6}{'gen ok':>8}{'final ok':>10}"
          f"{'full cov %':>12}{'gen s':>9}{'corr s':>9}{'total s':>10}")
    for model in models:
        for v in versions:
            vr = vm_rows[(v, model)]
            if not vr:
                continue
            n_gen = sum(1 for r in vr if _gen_status(r) == "ok")
            n_ok = sum(1 for r in vr if r["final_status"] == "ok")
            n_cov = sum(1 for r in vr if _is_true(r.get("coverage_complete")))
            gen_t, corr_t, tot_t = [], [], []
            for r in vr:
                gen = _to_float(r.get("llm_duration_s"))
                corr = _to_float(r.get("llm_correction_s")) or 0.0
                if gen is not None:
                    gen_t.append(gen)      # generation only: comparable across models
                    corr_t.append(corr)
                parts = [_to_float(r.get(c)) for c in
                         ("llm_duration_s", "llm_correction_s",
                          "synthesis_duration_s", "sta_duration_s")]
                if any(p is not None for p in parts):
                    tot_t.append(sum(p or 0.0 for p in parts))
            avg = lambda xs: f"{sum(xs)/len(xs):.1f}" if xs else "—"
            print(f"  {model:<16}{v:<11}{len(vr):>6}{_pct(n_gen, len(vr)):>8}"
                  f"{_pct(n_ok, len(vr)):>10}{_pct(n_cov, len(vr)):>12}"
                  f"{avg(gen_t):>9}{avg(corr_t):>9}{avg(tot_t):>10}")
        print()
    print("  gen ok   = share ok on the FIRST attempt (comparable across prompts).")
    print("  final ok = share ok after correction; identical to gen ok wherever the")
    print("             retry loop does not run, so only v2 rows can differ.")
    print("  gen s    = mean first-shot generation time (llm_duration_s), retry excluded;")
    print("             this is the figure comparable across models.")
    print("  corr s   = mean correction-retry time (llm_correction_s), 0 where none fired.")
    print("  total s  = mean generation + correction + synthesis + STA per run.")


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
# Severity ranking used to tell a repair from a regression. The ordering is by
# how usable the constraint set is: 'ok' is complete; 'partial_coverage' still
# parses and times real paths but misses some; 'timing_violation' is a valid,
# fully-analyzed set that simply does not meet timing; 'ineffective_no_paths'
# parses but checks nothing; 'sta_failed'/'synth_failed' are rejected outright.
SEVERITY = {
    "ok": 0,
    "partial_coverage": 1,
    "timing_violation": 2,
    "ineffective_no_paths": 3,
    "sta_failed": 4,
    "synth_failed": 5,
}
_BROKEN = {"sta_failed", "synth_failed"}   # tool refuses the SDC outright




def _table_correction_summary(models, llm_rows) -> None:
    """Per model: did the retry help, hurt, or do nothing?

    'ok %' can only rise, because a run that is already ok never enters the loop.
    That makes it a misleading headline on its own -- a retry that turns a
    partially-working SDC into one the tool rejects does not move ok % at all.
    The repaired/regressed split is what shows the real effect.
    """
    _hr("Correction Loop  (effect of the one-shot retry)")
    fired = _corrected_rows(llm_rows)
    if not fired:
        print("  No corrected runs in this dataset "
              "(correction_attempts = 0 everywhere).")
        return
    # ok% must be measured over the rows that could actually be corrected. The
    # retry only runs on some prompt versions, so pooling every version dilutes
    # the effect: a model whose v2 goes 80% -> 98% reads as 80% -> 89% once its
    # never-corrected v1 rows are mixed in.
    eligible_versions = {r.get("prompt_version") for r in fired}
    print(f"  {'model':<18}{'fired':>7}{'repaired':>10}{'regressed':>11}"
          f"{'unchanged':>11}{'lost ok':>9}{'ok% before':>12}{'ok% after':>11}")
    for m in models + ["ALL"]:
        sub = [r for r in llm_rows
               if (m == "ALL" or r["llm_model"] == m)
               and r.get("prompt_version") in eligible_versions]
        f = _corrected_rows(sub)
        if not f:
            continue
        rep = sum(r["final_status"] == "ok" for r in f)
        reg = sum(SEVERITY.get(r["final_status"], 9) > SEVERITY.get(r["initial_status"], 9)
                  for r in f)
        unch = sum(r["final_status"] == r["initial_status"] for r in f)
        # reached ok at some level, then a later level broke it again
        lost = sum(r.get("best_status") == "ok" and r["final_status"] != "ok" for r in f)
        okb = sum(_gen_status(r) == "ok" for r in sub)
        oka = sum(r.get("final_status") == "ok" for r in sub)
        label = m if m != "ALL" else "— all models —"
        print(f"  {label:<18}{len(f):>7}{rep:>10}{reg:>11}{unch:>11}{lost:>9}"
              f"{_pct(okb, len(sub)):>12}{_pct(oka, len(sub)):>11}")
    broke = sum(r["final_status"] in _BROKEN and r["initial_status"] not in _BROKEN
                for r in fired)
    rep = sum(r["final_status"] == "ok" for r in fired)
    reg = sum(SEVERITY.get(r['final_status'], 9) > SEVERITY.get(r['initial_status'], 9)
              for r in fired)
    print()
    print(f"  repaired  : {rep:>4} / {len(fired)}  ({_pct(rep, len(fired))})")
    print(f"  regressed : {reg:>4} / {len(fired)}  (retry produced a strictly worse outcome)")
    print(f"  of which  : {broke:>4} went from a parsing SDC to one the tool rejects")
    print(f"  ok% columns cover only {'/'.join(sorted(eligible_versions))} — the")
    print("  prompt version(s) where the retry actually runs.")
    print("  'lost ok' = reached ok at one level and a later level broke it again;")
    print("  the loop stops at the first ok, so this can only be non-zero if an")
    print("  earlier attempt passed and was then superseded.")


def _table_transition(llm_rows) -> None:
    """Full initial -> final matrix. The off-diagonal is the story."""
    _hr("Correction Transitions  (rows = before retry, columns = after)")
    fired = _corrected_rows(llm_rows)
    if not fired:
        print("  No corrected runs in this dataset.")
        return
    order = [s for s in SEVERITY if any(r["initial_status"] == s or
                                        r["final_status"] == s for r in fired)]
    tm = {a: Counter() for a in order}
    for r in fired:
        tm.setdefault(r["initial_status"], Counter())[r["final_status"]] += 1
    W = 15
    print(f"  {'before / after':<22}" + "".join(f"{s[:13]:>{W}}" for s in order)
          + f"{'repair%':>9}")
    for a in order:
        if a not in tm or not sum(tm[a].values()):
            continue
        tot = sum(tm[a].values())
        cells = "".join(f"{(tm[a][b] or '·'):>{W}}" for b in order)
        print(f"  {a:<22}{cells}{_pct(tm[a]['ok'], tot):>9}")
    print()
    print("  Columns run best -> worst, so for each row: the 'ok' column is a repair,")
    print("  the cell matching the row's own status means the retry changed nothing,")
    print("  and anything further right is a regression. (There is no 'ok' row --")
    print("  a run that already passed never enters the loop.)")


def _ladder_level(r) -> int | None:
    """Level (1-based) at which this corrected run first reached ok; None = never.

    Uses correction_path when present; older rows fall back to initial/final.
    """
    path = (r.get("correction_path") or "").split(">")
    if len(path) < 2:  # no recorded path: reconstruct a 1-level view
        path = [r.get("initial_status", ""), r.get("final_status", "")]
    for i, status in enumerate(path[1:], start=1):
        if status == "ok":
            return i
    return None


def _table_repair_ladder(models, llm_rows) -> None:
    """Where on the feedback ladder each model's failures get repaired.

    level 1 = tool symptom only            -> repaired here: robustness slip
    level 2 = + legal object vocabulary    -> repaired here: namespace-limited
    'never' = unrepaired even with the object vocabulary supplied.
    """
    _hr("Repair Ladder  (level at which corrected runs reach ok)")
    fired = _corrected_rows(llm_rows)
    if not fired:
        print("  No corrected runs in this dataset.")
        return
    # Levels actually present, so the table follows whatever depth was run.
    depth = max((len((r.get("correction_path") or "").split(">")) - 1
                 for r in fired), default=1)
    depth = max(depth, 1)
    names = {1: "@1 symptom", 2: "@2 dict"}
    heads = "".join(f"{names.get(i, f'@{i}'):>12}" for i in range(1, depth + 1))
    print(f"  {'model':<18}{'fired':>7}{heads}{'never':>8}")
    for m in models + ["ALL"]:
        f = fired if m == "ALL" else [r for r in fired if r["llm_model"] == m]
        if not f:
            continue
        levels = Counter(_ladder_level(r) for r in f)
        label = m if m != "ALL" else "— all models —"
        cells = "".join(f"{levels.get(i, 0):>12}" for i in range(1, depth + 1))
        print(f"  {label:<18}{len(f):>7}{cells}{levels.get(None, 0):>8}")
    print()
    heads = "".join(f"{'@' + str(i):>6}" for i in range(1, depth + 1))
    print(f"  {'design':<14}{'fired':>7}{heads}{'never':>8}")
    for d in sorted({r["design"] for r in fired}):
        f = [r for r in fired if r["design"] == d]
        levels = Counter(_ladder_level(r) for r in f)
        cells = "".join(f"{levels.get(i, 0):>6}" for i in range(1, depth + 1))
        print(f"  {d:<14}{len(f):>7}{cells}{levels.get(None, 0):>8}")


def _table_failure_reasons(models, versions, vm_rows) -> None:
    """*Why* runs fail, from the classifier's fine-grained label.

    final_status says how badly a run failed; primary_label says what was wrong
    with the text the model produced. The distinction matters because one coarse
    status covers several very different causes -- 'sta_failed' alone spans a
    malformed command, a port that does not exist, and a non-numeric delay --
    and those call for different fixes.
    """
    _hr("Failure Reasons  (classifier label, per version x model; excludes passes)")
    passing = {"accepted", "accepted_partial"}
    labels = sorted({r.get("primary_label", "") for rows in vm_rows.values()
                     for r in rows if r.get("primary_label") not in passing
                     and r.get("primary_label")})
    if not labels:
        print("  No classifier labels recorded.")
        return
    _grouped_header("primary_label", versions, models, with_ref=False)
    counts = {k: Counter(r.get("primary_label", "") for r in rows)
              for k, rows in vm_rows.items()}
    for lab in labels:
        row = f"  {lab[:LABEL_W - 2]:<{LABEL_W - 2}}"
        row += VSEP.join(_block([(counts[(v, m)].get(lab, 0) or '·') for m in models])
                         for v in versions)
        print(row)
    print()
    print("  These are *final* labels (after any correction). A label like")
    print("  port_reference_error or non_numeric_value points at the SDC text")
    print("  itself, whereas incomplete_timing_coverage is a semantic gap.")


def _table_repair_by_design(llm_rows) -> None:
    """Which designs are actually repairable from the error message alone."""
    _hr("Repair Rate by Design  (runs where the correction loop fired)")
    fired = _corrected_rows(llm_rows)
    if not fired:
        print("  No corrected runs in this dataset.")
        return
    print(f"  {'design':<14}{'fired':>7}{'repaired':>10}{'rate':>8}   dominant before-state")
    agg = {}
    for r in fired:
        d = r["design"]
        n, ok, states = agg.get(d, (0, 0, Counter()))
        states[r["initial_status"]] += 1
        agg[d] = (n + 1, ok + (r["final_status"] == "ok"), states)
    for d in sorted(agg, key=lambda k: -agg[k][0]):
        n, ok, states = agg[d]
        top = states.most_common(1)[0]
        print(f"  {d:<14}{n:>7}{ok:>10}{_pct(ok, n):>8}   {top[0]} ({top[1]})")


def analyze(rows: list[dict], model: str | None) -> None:
    refs = [r for r in rows if r.get("llm_model") == REFERENCE]
    if model:
        models = [model]
    else:
        models = sorted({r["llm_model"] for r in rows if r.get("llm_model") != REFERENCE})
    llm_rows = [r for r in rows if r.get("llm_model") in models]
    versions = sorted({r.get("prompt_version", "") for r in llm_rows if r.get("prompt_version")})

    # One lookup keyed by (version, model); every table indexes into it so the
    # model is just another column rather than a repeated per-model section.
    vm_rows = {(v, m): [r for r in llm_rows
                        if r.get("prompt_version") == v and r.get("llm_model") == m]
               for v in versions for m in models}

    abbr_legend = ", ".join(f"{_abbr(m)}={m}" for m in models)
    print(f"\nModels (column abbreviations): {abbr_legend}")

    _table_summary(refs, models, versions, vm_rows, len(rows))
    _table_headline(refs, models, versions, vm_rows)
    _table_status(refs, models, versions, vm_rows)
    _table_coverage(models, versions, vm_rows)
    _table_per_design(models, versions, vm_rows)
    _table_slack(refs, models, versions, vm_rows)
    _table_netlist_invariance(refs, llm_rows)
    _table_failure_reasons(models, versions, vm_rows)
    _table_correction_summary(models, llm_rows)
    _table_transition(llm_rows)
    _table_repair_ladder(models, llm_rows)
    _table_repair_by_design(llm_rows)
    if len(models) > 1:
        _table_models(models, versions, vm_rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=config.DATASET_CSV)
    parser.add_argument("--model", default=None,
                        help="Restrict to one model; default analyzes every "
                             "model present in the dataset.")
    args = parser.parse_args()
    analyze(_read_rows(args.dataset), model=args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
