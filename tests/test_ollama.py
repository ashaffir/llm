#!/usr/bin/env python3
"""
Non-interactive client for Ollama (local LLM).
- Reads post content from a file path.
- Injects it into the fixed analysis prompt.
- Sends to the local LLM and prints the JSON response.

Config (env):
  OLLAMA_BASE_URL (default: http://localhost:11434)
  OLLAMA_MODEL    (default: llama3.1:8b)

Usage:
  python3 analyze_post.py /path/to/post.txt
  python3 analyze_post.py /path/to/post.txt --model qwen2.5:7b
"""

import os
import sys
import json
import argparse
import requests
import threading
import itertools
import time
import subprocess


API_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL    = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

PROMPT_TMPL = """You are a senior financial analyst and real-time trading decision assistant.

Analyze the following text for its potential short-term financial market impact — specifically within the next 24 hours.

Score the event across five dimensions to determine an overall trading confidence level:

- **impact_size** (0–1): How strong the financial effect might be (0 = minimal, 1 = market-moving)
- **time_proximity** (0–1): How soon the event is expected to begin affecting prices (1 = immediate, 0 = distant future)
- **clarity** (0–1): How clear and unambiguous the financial implications are (1 = very clear, 0 = speculative or vague)
- **volatility_sensitivity** (0–1): Whether the affected industry is known to respond quickly to events (1 = high sensitivity, 0 = slow/stable)
- **duration** (0–1): How long the effect will last within the 24h window (1 = hours-long and tradable, 0 = fleeting or negligible)

Then compute:
final_confidence = 0.35 * impact_size + 0.25 * time_proximity + 0.15 * clarity + 0.15 * volatility_sensitivity + 0.10 * duration

Respond only with a valid JSON object in the following format:

{
  "industry": "INDUSTRY_NAME",       // Best-matching impacted industry
  "company": "COMPANY_NAME",         // Use "N/A" if no specific company mentioned
  "symbol": "STOCK_SYMBOL",          // If no company is mentioned, infer the most likely affected symbol from known large-cap leaders in the given industry.
  "direction": "buy",                // One of: 'buy', 'sell', or 'hold'
  "confidence": 0.82,                // Final score (0.0 – 1.0)
  "reason": "Short explanation of the event and impact logic",
  "scores": {
    "impact_size": 0.9,
    "time_proximity": 1.0,
    "clarity": 0.85,
    "volatility_sensitivity": 0.7,
    "duration": 0.6
  }
}

If no specific market impact is likely or the text is irrelevant, return:
{
  "symbol": "N/A",
  "industry": "N/A",
  "direction": "hold",
  "confidence": 0.0,
  "reason": "No clear or tradable market impact identified.",
  "scores": {
    "impact_size": 0.0,
    "time_proximity": 0.0,
    "clarity": 0.0,
    "volatility_sensitivity": 0.0,
    "duration": 0.0
  }
}

## POST TEXT IS HERE ##
"""

class Spinner:
    def __init__(self, text="Waiting for model response"):
        self.text = text
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
    def __enter__(self):
        self._t.start()
        return self
    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        self._t.join()
        sys.stderr.write("\r" + " " * (len(self.text) + 6) + "\r")
        sys.stderr.flush()
    def _run(self):
        for ch in itertools.cycle("|/-\\"):
            if self._stop.is_set():
                break
            sys.stderr.write(f"\r{self.text} {ch}")
            sys.stderr.flush()
            time.sleep(0.1)

def _list_models_via_api(api_base: str) -> list:
    """Query Ollama for available local models."""
    try:
        r = requests.get(f"{api_base}/api/tags", timeout=5)
        r.raise_for_status()
        data = r.json() or {}
        models = data.get("models", [])
        names = []
        for it in models:
            name = it.get("name")
            if name:
                names.append(name)
        return sorted(set(names))
    except Exception:
        return []

def _list_models_via_cli() -> list:
    """Fallback: parse `ollama list` output."""
    try:
        out = subprocess.check_output(["ollama", "list"], text=True, timeout=5)
        names = []
        for line in out.splitlines():
            tok = line.strip().split()
            if tok and ":" in tok[0]:
                names.append(tok[0])
        return sorted(set(names))
    except Exception:
        return []

def choose_model_interactive(api_base: str,
                             preferred=("llama3.1:8b", "qwen2.5:7b")) -> str:
    models = _list_models_via_api(api_base) or _list_models_via_cli()
    if not models:
        print("No Ollama models found. Example pulls:\n"
              "  ollama pull llama3.1:8b\n"
              "  ollama pull qwen2.5:7b", file=sys.stderr)
        sys.exit(1)

    pref = [m for m in preferred if m in models]
    rest = [m for m in models if m not in pref]
    candidates = pref + rest

    print("\nSelect a model to run:", file=sys.stderr)
    for i, name in enumerate(candidates, 1):
        print(f"  [{i}] {name}", file=sys.stderr)

    while True:
        sel = input("Model # [1]: ").strip()
        if sel == "":
            return candidates[0]
        if sel.isdigit():
            idx = int(sel)
            if 1 <= idx <= len(candidates):
                return candidates[idx - 1]
        print("Invalid selection. Try again.", file=sys.stderr)

def _analysis_schema() -> dict:
    """JSON schema to enforce exact output structure."""
    return {
        "type": "object",
        "properties": {
            "industry": {"type": "string"},
            "company": {"type": "string"},
            "symbol": {"type": "string"},
            "direction": {"type": "string", "enum": ["buy", "sell", "hold"]},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "reason": {"type": "string"},
            "scores": {
                "type": "object",
                "properties": {
                    "impact_size": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "time_proximity": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "clarity": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "volatility_sensitivity": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "duration": {"type": "number", "minimum": 0.0, "maximum": 1.0}
                },
                "required": [
                    "impact_size",
                    "time_proximity",
                    "clarity",
                    "volatility_sensitivity",
                    "duration"
                ],
                "additionalProperties": False
            }
        },
        "required": [
            "industry",
            "company",
            "symbol",
            "direction",
            "confidence",
            "reason",
            "scores"
        ],
        "additionalProperties": False
    }

def build_prompt(post_text: str) -> str:
    return PROMPT_TMPL.replace("## POST TEXT IS HERE ##", post_text.strip())

def call_ollama(api_base: str, model: str, prompt: str) -> dict:
    """Call Ollama chat API with structured JSON output."""
    url = f"{api_base}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return ONLY valid JSON per the schema. No markdown, no explanations."},
            {"role": "user", "content": prompt}
        ],
        "format": _analysis_schema(),   # enforce strict JSON structure
        "stream": False,
        "options": {
            "temperature": 0.0,
            "num_ctx": 4096
        }
    }
    with Spinner():
        resp = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=180)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:400]}")
    data = resp.json()
    content = (data.get("message") or {}).get("content", "")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        raise RuntimeError(f"Model returned non-JSON: {content[:400]}")

def main():
    ap = argparse.ArgumentParser(description="Analyze a news post file via a local Ollama model.")
    ap.add_argument("file", help="Path to a text file containing the post content.")
    ap.add_argument("--base-url", default=API_BASE, help="Ollama base URL (default from OLLAMA_BASE_URL env).")
    ap.add_argument("--model", default=None, help="Model ref (e.g., llama3.1:8b or qwen2.5:7b).")
    args = ap.parse_args()

    try:
        with open(args.file, "r", encoding="utf-8") as f:
            post_text = f.read()
    except Exception as e:
        print(f"Error reading file '{args.file}': {e}", file=sys.stderr)
        sys.exit(1)

    if not post_text.strip():
        print("Input file is empty.", file=sys.stderr)
        sys.exit(1)

    prompt = build_prompt(post_text)
    model = args.model or choose_model_interactive(args.base_url)

    try:
        result = call_ollama(args.base_url, model, prompt)
    except Exception as e:
        print(f"Error calling model: {e}", file=sys.stderr)
        print(f"Hints:\n  - Is Ollama running? Try: curl -s {args.base_url}/api/tags\n"
              f"  - Correct base URL? Default is http://localhost:11434\n"
              f"  - Model pulled?    Try: ollama pull {model}", file=sys.stderr)
        sys.exit(2)

    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()

