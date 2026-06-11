"""LLM constraint generation + output cleaning."""
from __future__ import annotations
import re
import ollama
from dataclasses import dataclass
from . import config


VALID_SDC_PREFIXES = (
    "create_clock",
    "create_generated_clock",
    "set_input_delay",
    "set_output_delay",
    "set_clock_uncertainty",
    "set_clock_latency",
    "set_clock_groups",
    "set_false_path",
    "set_multicycle_path",
    "set_max_delay",
    "set_min_delay",
    "set_load",
    "set_driving_cell",
    "set_max_transition",
    "set_max_fanout",
    "set_disable_timing",
    "set_case_analysis",
    "set_propagated_clock",
)


# Maps the prompt_version label recorded in the dataset to its template file.
PROMPT_FILES = {
    "v1_base": "base_v1.txt",
    "v2_base": "base_v2.txt",
}


def prompt_path_for(version: str):
    """Resolve a prompt_version label (e.g. 'v2_base') to its template path."""
    fname = PROMPT_FILES.get(version)
    if fname is None:
        raise ValueError(
            f"Unknown prompt_version '{version}'. Known: {sorted(PROMPT_FILES)}"
        )
    return config.PROMPTS_DIR / fname


@dataclass
class LLMResult:
    raw: str
    cleaned: str
    extracted_lines: list[str]


def _strip_think_tags(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)


def _strip_code_fences(text: str) -> str:
    fence_pat = re.compile(r"```(?:\w+)?\s*\n?(.*?)```", re.DOTALL)
    matches = fence_pat.findall(text)
    if matches:
        return "\n".join(matches)
    return text


def clean_sdc(raw: str) -> tuple[str, list[str]]:
    """Extract valid SDC lines from raw LLM output.

    Returns (cleaned_text, list_of_valid_lines).
    """
    text = _strip_think_tags(raw)
    text = _strip_code_fences(text)

    valid_lines: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        # tolerate a leading backtick or numbering like "1. "
        s = re.sub(r"^\d+[\.\)]\s*", "", s)
        s = s.strip("`").strip()
        if s.startswith(VALID_SDC_PREFIXES):
            valid_lines.append(s)

    return "\n".join(valid_lines), valid_lines


def generate(
    verilog_src: str,
    clock_period_ns: float = config.DEFAULT_CLOCK_PERIOD_NS,
    model: str = config.LLM_MODEL,
    seed: int | None = None,
    prompt_path=None,
) -> LLMResult:
    """Call the local LLM to generate SDC for a Verilog design."""
    prompt_path = prompt_path or (config.PROMPTS_DIR / "base.txt")
    prompt_tmpl = prompt_path.read_text()
    prompt = prompt_tmpl.format(verilog=verilog_src, clock_period_ns=clock_period_ns)

    # Disable chain-of-thought so every model is compared on equal footing (direct
    # structured output, no reasoning). It also roughly halves qwen3:8b's runtime,
    # and is harmless for non-reasoning models (granite) or models whose thinking is
    # token-controlled rather than option-controlled (gemma4). Reasoning models that
    # stream their whole answer into resp["thinking"] would otherwise leave
    # resp["response"] empty within the generation budget — hence the fallback below.
    options: dict = {"think": False}
    if seed is not None:
        options["seed"] = seed
        options["temperature"] = 0.8  # need variability when sampling

    resp = ollama.generate(model=model, prompt=prompt, options=options)
    raw = resp.get("response") or resp.get("thinking") or ""
    cleaned, lines = clean_sdc(raw)
    return LLMResult(raw=raw, cleaned=cleaned, extracted_lines=lines)


def correct(
    verilog_src: str,
    previous_sdc: str,
    error_message: str,
    clock_period_ns: float = config.DEFAULT_CLOCK_PERIOD_NS,
    model: str = config.LLM_MODEL,
) -> LLMResult:
    """Ask the LLM to fix a failing SDC given the tool error."""
    prompt_tmpl = (config.PROMPTS_DIR / "correction.txt").read_text()
    prompt = prompt_tmpl.format(
        previous_sdc=previous_sdc,
        error_message=error_message[-1500:],  # tail to keep prompt small
        verilog=verilog_src,
        clock_period_ns=clock_period_ns,
    )
    resp = ollama.generate(model=model, prompt=prompt, options={"think": False})
    raw = resp.get("response") or resp.get("thinking") or ""
    cleaned, lines = clean_sdc(raw)
    return LLMResult(raw=raw, cleaned=cleaned, extracted_lines=lines)
