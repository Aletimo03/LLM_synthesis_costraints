import ollama
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_FILE = ROOT / "prompts" / "simple_prompt.txt"
OUT_FILE = ROOT / "constraints" / "generated.sdc"

prompt = PROMPT_FILE.read_text()

print(f"--- Querying Qwen3 (via API) ---")

#more stable than subprocess
response = ollama.generate(model='qwen3:8b', prompt=prompt)
raw_output = response['response'].strip()

#valid_prefixes = ("create_clock", "set_input_delay", "set_output_delay") # etc...

#sdc_lines = [line.strip() for line in raw_output.splitlines() if line.strip().startswith(valid_prefixes)]

# clean_sdc = "\n".join(sdc_lines)
#OUT_FILE.write_text(clean_sdc + "\n")

OUT_FILE.write_text(raw_output + "\n")


print(f"Generated and saved to {OUT_FILE}")