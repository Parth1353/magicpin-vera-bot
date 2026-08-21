#!/usr/bin/env python3
"""Run the official `judge_simulator.py` without hand-editing its CONFIGURATION block.

The pack asks you to paste your key into the file. That is fine once, but it makes the key
easy to commit by accident and awkward to switch providers. This copies the simulator to a
temporary file, patches the config constants from environment variables or flags, and runs
it unmodified otherwise — so it is still the judge's own code doing the scoring.

    export LLM_API_KEY=sk-...
    python tools/run_judge.py --provider anthropic --scenario full_evaluation
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT.parent
SIMULATOR = PACK / "judge_simulator.py"

SCENARIOS = ("all", "warmup", "phase2_short", "auto_reply_hell", "intent_transition",
             "hostile", "full_evaluation")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.getenv("BOT_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--provider", default=os.getenv("LLM_PROVIDER", "anthropic"))
    parser.add_argument("--model", default=os.getenv("LLM_MODEL", ""))
    parser.add_argument("--key", default=os.getenv("LLM_API_KEY", ""))
    parser.add_argument("--scenario", default="full_evaluation", choices=SCENARIOS)
    parser.add_argument("--ollama-url", default=os.getenv("OLLAMA_URL", ""),
                        help="only for --provider ollama; overrides the simulator default")
    args = parser.parse_args()

    if not SIMULATOR.exists():
        print(f"judge_simulator.py not found at {SIMULATOR}")
        return 1
    if args.provider != "ollama" and not args.key:
        print("No API key. Pass --key or set LLM_API_KEY (the judge needs a model to score).")
        return 1

    source = SIMULATOR.read_text()
    for pattern, value in (
        (r'^BOT_URL = .*$', f'BOT_URL = {args.url!r}'),
        (r'^LLM_PROVIDER = .*$', f'LLM_PROVIDER = {args.provider!r}'),
        (r'^LLM_API_KEY = .*$', f'LLM_API_KEY = {args.key!r}'),
        (r'^LLM_MODEL = .*$', f'LLM_MODEL = {args.model!r}'),
        (r'^TEST_SCENARIO = .*$', f'TEST_SCENARIO = {args.scenario!r}'),
    ):
        source = re.sub(pattern, value, source, count=1, flags=re.M)
    if args.ollama_url:
        source = re.sub(r'^OLLAMA_URL = .*$', f'OLLAMA_URL = {args.ollama_url!r}',
                        source, count=1, flags=re.M)
    # keep the simulator's own dataset path working from a temp location
    source = source.replace('DATASET_DIR = Path(__file__).parent / "dataset"',
                            f'DATASET_DIR = Path({str(PACK)!r}) / "dataset"')

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "judge_simulator_configured.py"
        script.write_text(source)
        print(f"running the official simulator · {args.scenario} · {args.provider} · {args.url}\n")
        return subprocess.call([sys.executable, str(script)])


if __name__ == "__main__":
    raise SystemExit(main())
