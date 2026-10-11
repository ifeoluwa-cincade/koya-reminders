"""Low-overhead `claude -p` wrapper for bulk one-shot classification/extraction
calls (audits, name fixes, founder lookups, etc.) -- NOT for real coding tasks.

Every `claude -p` call auto-loads global + project CLAUDE.md files by default,
since that's genuinely useful for real coding work. For a script firing
thousands of tiny isolated calls, that context is dead weight: measured
2026-08-20 at ~40K extra tokens and ~65% higher notional cost PER CALL,
for zero benefit (the classification prompt doesn't need Testament/Biography
rules, writing style, sheet-formatting conventions, etc).

Fix: run the subprocess from a neutral scratch directory (so CLAUDE.md
auto-discovery finds nothing walking up from cwd) with a minimal custom
--system-prompt (replaces the default entirely). Verified: cost per call
$0.0875 -> $0.0304 (65% cut), same output quality. Doesn't reduce wall-clock
latency per call (that's dominated by process startup, not token count) --
use parallelism (ThreadPoolExecutor) for speed, this module for quota headroom.

Drop-in for any script currently doing its own subprocess.run(["claude","-p",...]).
"""
import json
import subprocess
import tempfile
from pathlib import Path

_SCRATCH_DIR = Path(tempfile.gettempdir()) / "claude_lite_scratch"
_SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SYSTEM_PROMPT = "You are a precise JSON-only classifier. Respond only with the requested JSON, nothing else."


def call_claude_lite(prompt, json_schema=None, model="haiku", timeout_sec=90,
                      max_retries=2, system_prompt=None):
    """Same contract as a typical bulk-script call_claude(): returns the
    validated structured_output dict, raises on failure after retries.

    json_schema: JSON Schema dict -- if given, uses --json-schema and reads
    outer['structured_output']. If None, reads outer['result'] as plain text
    (caller is responsible for parsing it, e.g. json.loads after stripping
    markdown fences).
    """
    cmd = [
        "claude", "-p",
        "--no-session-persistence",
        "--model", model,
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--strict-mcp-config",
        "--system-prompt", system_prompt or DEFAULT_SYSTEM_PROMPT,
    ]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]

    last_exc = None
    for _ in range(max_retries + 1):
        try:
            result = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True,
                timeout=timeout_sec, encoding="utf-8", cwd=str(_SCRATCH_DIR),
            )
            if result.returncode != 0:
                raise RuntimeError(f"claude exit {result.returncode}")
            outer = json.loads(result.stdout)
            if outer.get("is_error"):
                raise RuntimeError(f"claude error: {json.dumps(outer)[:200]}")
            if json_schema:
                so = outer.get("structured_output")
                if not so:
                    raise RuntimeError(f"no structured_output: {result.stdout[:200]}")
                return so
            text = (outer.get("result") or "").strip()
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            return json.loads(text)
        except Exception as e:
            last_exc = e
    raise last_exc or RuntimeError("unknown failure")
