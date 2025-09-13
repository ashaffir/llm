#!/usr/bin/env python3
"""
Interactive client for Ollama (local LLM).
- Displays installed Ollama models
- Lets user choose which model to use (default: llama3)
- Asks for input text file (default: post.txt)
- Runs analysis and prints JSON response
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
import re

API_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
DEFAULT_MODEL = "llama3"
DEFAULT_FILE = "post.txt"

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
  "confidence": CONFIDENCE,                // Final confidence score (0.0 – 1.0)
  "reason": "Short explanation of the event and impact logic",
  "scores": {
    "impact_size": IMPACT,              // impact size score 0.0 - 1.0
    "time_proximity": PROXIMITY,        // time proximity scode 0.0 - 1.0
    "clarity": CLARITY,                 // clarity of the information  0.0 - 1.0 
    "volatility_sensitivity": VOLATILITY, // volatility score 0.0 - 1.0
    "duration": DURATION                  // effect duration score 0.0 - 1.0
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

def get_installed_models(api_base: str) -> list:
    """Get list of installed Ollama models."""
    models = _list_models_via_api(api_base) or _list_models_via_cli()
    return models

def choose_model_interactive(models: list, default_model: str = DEFAULT_MODEL) -> str:
    """Let user choose from available models."""
    if not models:
        print("No Ollama models found.", file=sys.stderr)
        sys.exit(1)

    print("\nInstalled Ollama models:", file=sys.stderr)
    for i, name in enumerate(models, 1):
        marker = " (default)" if name == default_model else ""
        print(f"  [{i}] {name}{marker}", file=sys.stderr)
    print("  [q] Quit", file=sys.stderr)   # NEW

    while True:
        try:
            sel = input(f"\nSelect model # [1/{default_model}]: ").strip().lower()
            if sel == "q":
                print("Exiting.", file=sys.stderr)
                sys.exit(0)
            if sel == "":
                if default_model in models:
                    return default_model
                return models[0]
            if sel.isdigit():
                idx = int(sel)
                if 1 <= idx <= len(models):
                    return models[idx - 1]
            if sel in models:
                return sel
            print("Invalid selection. Try again.", file=sys.stderr)
        except (EOFError, KeyboardInterrupt):
            print("\nOperation cancelled.", file=sys.stderr)
            sys.exit(0)

def get_input_file(default_file: str = DEFAULT_FILE) -> str:
    """Ask user for input file path."""
    while True:
        try:
            file_path = input(f"Enter input file path [{default_file}]: ").strip()
            if not file_path:
                file_path = default_file
            
            # Check if file exists
            if not os.path.exists(file_path):
                print(f"File '{file_path}' not found. Please try again.", file=sys.stderr)
                continue
            
            return file_path
            
        except (EOFError, KeyboardInterrupt):
            print("\nOperation cancelled.", file=sys.stderr)
            sys.exit(0)

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

def extract_json_from_response(content: str) -> str:
    """
    Try to extract a valid JSON object from the model response.
    Handles models that prepend 'Thinking...', markdown fences, or other commentary.
    Returns the cleaned JSON string (still as text).
    """
    import re, json

    # 1. Strip common "thinking" style prefaces
    cleaned = re.sub(r'(?is)(thinking\.\.\.|done thinking\.)', '', content)
    cleaned = re.sub(r'(?is)(we need to respond.*?)(?=\{)', '', cleaned)

    # 2. Drop anything before the first '{'
    start_idx = cleaned.find('{')
    if start_idx > 0:
        cleaned = cleaned[start_idx:]

    # 3. Direct parse attempt
    try:
        json.loads(cleaned)
        return cleaned
    except json.JSONDecodeError:
        pass

    # 4. Look for common fenced code blocks
    patterns = [
        r'```json\s*(.*?)\s*```',
        r'```\s*(.*?)\s*```',
        r'(\{.*\})',       # greedy
        r'(\{.*?\})',      # non-greedy
    ]
    for pattern in patterns:
        matches = re.findall(pattern, cleaned, re.DOTALL)
        for match in matches:
            if isinstance(match, tuple):
                match = match[0]
            try:
                json.loads(match)
                return match
            except json.JSONDecodeError:
                continue

    # 5. Fallback: substring between first { and last }
    first = cleaned.find('{')
    last = cleaned.rfind('}')
    if first != -1 and last != -1 and last > first:
        candidate = cleaned[first:last+1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # 6. Give up — return raw content
    return cleaned


def call_ollama(api_base: str, model: str, prompt: str) -> dict:
    """
    Call Ollama chat API and return a parsed JSON object.
    - Uses strict JSON Schema for compliant models (llama/qwen/mistral).
    - Falls back to simple "json" format for chatty models (gpt-oss/deepseek/r1),
      then sanitizes with extract_json_from_response().
    """
    url = f"{api_base}/api/chat"

    ml = model.lower()
    gpt_like = any(k in ml for k in ("gpt-oss", "deepseek", "r1"))
    fmt = "json" if gpt_like else _analysis_schema()

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Return ONLY valid JSON per the schema (or pure JSON if schema not supported). "
                    "No markdown, no thoughts, no explanations."
                )
            },
            {"role": "user", "content": prompt}
        ],
        "format": fmt,          # either schema dict or "json"
        "stream": False,
        "options": {
            "temperature": 0.0,
            "num_ctx": 4096
        }
    }

    with Spinner():
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=180
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:400]}")

    data = resp.json()
    content = (data.get("message") or {}).get("content", "")

    # For safety, always run through the extractor (handles GPT-style "Thinking..." etc.)
    json_text = extract_json_from_response(content)

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        # Last-ditch: if there is a '{' later in the string, try trimming before first '{'
        first = json_text.find('{')
        last = json_text.rfind('}')
        if first != -1 and last != -1 and last > first:
            candidate = json_text[first:last+1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        raise RuntimeError(
            f"Model returned non-JSON content. Attempted to parse: {json_text[:200]}..."
            + (f" (truncated from {len(json_text)} chars)" if len(json_text) > 200 else "")
        ) from e

def main():
    print("Ollama Financial Analysis Tool", file=sys.stderr)
    print("=" * 40, file=sys.stderr)

    # Get installed models once
    models = get_installed_models(API_BASE)

    while True:
        # Let user choose model
        selected_model = choose_model_interactive(models, DEFAULT_MODEL)

        # Let user choose input file
        input_file = get_input_file(DEFAULT_FILE)

        # Read the input file
        try:
            with open(input_file, "r", encoding="utf-8") as f:
                post_text = f.read()
        except Exception as e:
            print(f"Error reading file '{input_file}': {e}", file=sys.stderr)
            continue

        if not post_text.strip():
            print("Input file is empty.", file=sys.stderr)
            continue

        # Build prompt and call Ollama
        prompt = build_prompt(post_text)

        print(f"\nRunning analysis with model: {selected_model}", file=sys.stderr)
        print(f"Input file: {input_file}", file=sys.stderr)
        print("-" * 40, file=sys.stderr)

        try:
            result = call_ollama(API_BASE, selected_model, prompt)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"Error calling model: {e}", file=sys.stderr)
            continue

        # Ask if user wants another run
        again = input("\nRun another analysis? (y/n): ").strip().lower()
        if again not in ("y", "yes"):
            print("Exiting.", file=sys.stderr)
            break

if __name__ == "__main__":
    main()
