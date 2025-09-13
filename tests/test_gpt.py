#!/usr/bin/env python3
"""
Non-interactive client for Docker Model Runner (OpenAI-compatible).
- Reads post content from a file path.
- Injects it into the fixed analysis prompt.
- Sends to the local LLM and prints the JSON response.

Config:
  DMR_BASE_URL (default: http://localhost:12434/engines/v1)
  DMR_MODEL    (default: ai/gpt-oss)

Usage:
  python3 analyze_post.py /path/to/post.txt
"""

import os, sys, json, argparse, requests, threading, itertools, time
import subprocess


API_BASE = os.getenv("DMR_BASE_URL", "http://localhost:12434/engines/v1")
MODEL    = os.getenv("DMR_MODEL", "ai/gpt-oss")

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
  "company": "COMPANY_NAME",       // Use "N/A" if no specific company mentioned 
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
            if self._stop.is_set(): break
            sys.stderr.write(f"\r{self.text} {ch}")
            sys.stderr.flush()
            time.sleep(0.1)

def _list_models_via_api(api_base) -> list:  # NEW
    """Query DMR for available models."""
    try:
        r = requests.get(f"{api_base}/models", timeout=5)
        r.raise_for_status()
        data = r.json()
        items = data.get("data") or data.get("models") or []
        names = []
        for it in items:
            name = it.get("id") or it.get("name")
            if name:
                names.append(name)
        return sorted(set(names))
    except Exception:
        return []

def _list_models_via_cli() -> list:  # NEW
    """Fallback: parse `docker model list` output if API is unavailable."""
    try:
        out = subprocess.check_output(["docker", "model", "list"], text=True, timeout=5)
        names = []
        for line in out.splitlines():
            # Expect lines like: ai/gpt-oss:20b   <size>   <created> ...
            tok = line.strip().split()
            if tok and ("/" in tok[0] or ":" in tok[0]):
                names.append(tok[0])
        return sorted(set(names))
    except Exception:
        return []

def choose_model_interactive(api_base: str, preferred=("ai/gpt-oss:20b", "ai/qwen3:8B-Q4_K_M")) -> str:  # NEW
    models = _list_models_via_api(api_base)
    if not models:
        models = _list_models_via_cli()
    if not models:
        print("No models found. Pull one first (e.g., ai/gpt-oss:20b or ai/qwen3:8B-Q4_K_M).", file=sys.stderr)
        sys.exit(1)

    # Put preferred models (if present) at the top
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


def build_prompt(post_text: str) -> str:
    return PROMPT_TMPL.replace("## POST TEXT IS HERE ##", post_text.strip())

def call_dmr(api_base: str, model: str, prompt: str) -> dict:
    url = f"{api_base}/chat/completions"
    payload = {
        "model": model,
        "messages": [
                {"role": "system", "content": "Return ONLY valid JSON per the schema. No markdown, no explanations."},
                {"role": "user", "content": prompt}
            ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "max_tokens": 600,
    }
    with Spinner():
        resp = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=180)
    if resp.status_code != 200:
        raise RuntimeError(f"DMR HTTP {resp.status_code}: {resp.text[:400]}")
    data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # If the model returned non-JSON, emit as-is but mark failure.
        raise RuntimeError(f"Model returned non-JSON: {content[:400]}")

def main():
    ap = argparse.ArgumentParser(description="Analyze a news post file via Docker Model Runner.")
    ap.add_argument("file", help="Path to a text file containing the post content.")
    ap.add_argument("--base-url", default=API_BASE, help="DMR base URL (default from DMR_BASE_URL env).")
    ap.add_argument("--model", default=None, help="Model ref (overrides interactive picker).")  # CHANGED: default=None
    args = ap.parse_args()

    try:
        with open(args.file, "r", encoding="utf-8") as f:
            post_text = f.read()
    except Exception as e:
        print(f"Error reading file '{args.file}': {e}", file=sys.stderr)
        sys.exit(1)

    if not post_text.strip():
        print("Input file is empty.", file=sys.stderr); sys.exit(1)

    prompt = build_prompt(post_text)

    model = args.model or choose_model_interactive(args.base_url)

    try:
        result = call_dmr(args.base_url, model, prompt)  # CHANGED: use selected model
    except Exception as e:
        print(f"Error calling model: {e}", file=sys.stderr)
        print(f"Hints:\n  - Is DMR running? Try: curl -s {args.base_url}/models"
              f"\n  - Correct endpoint base? Should include /engines/v1"
              f"\n  - Model pulled? Try: docker model pull {model}", file=sys.stderr)  # CHANGED: model
        sys.exit(2)

    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()

