"""
run.py
──────
Sentinel Hardware Skill — OpenClaw Entry Point

Called by the OpenClaw gateway on three triggers:
  - session_start  → initialise bridge, warm up analyser
  - before_turn    → inject hardware context into system prompt
  - after_turn     → log notable events to memory file

OpenClaw calls this script with the trigger as the first argument:
  python run.py init
  python run.py inject_context
  python run.py log_event

Context is passed via environment variables and stdin (JSON).
Results are written to stdout as JSON consumed by the gateway.

Standalone usage:
  python run.py --demo          # cycle through all scenarios
  SENTINEL_MOCK=warn python run.py inject_context
"""

import sys
import os
import json
import time
import argparse
from pathlib import Path
from datetime import datetime

# Add the main sentinel bridge to path
SENTINEL_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(SENTINEL_ROOT / "bridge"))
sys.path.insert(0, str(SENTINEL_ROOT / "agent"))

from sentinel_bridge import SentinelBridge, ContextAnalyser, parse_log_line, HardwareReading
from anomaly_detector import AnomalyDetector, Severity
from sentinel_memory import SessionMemory

# ── Config from environment ───────────────────────────────────────────────────

LOG_PATH   = os.environ.get("SENTINEL_LOG_PATH",
             str(SENTINEL_ROOT / "logs" / "sentinel_redo.log"))
POLL_MS    = int(os.environ.get("SENTINEL_POLL_MS", "500"))
MOCK       = os.environ.get("SENTINEL_MOCK", "")   # "nominal" | "warn" | "critical" | ""

# ── Shared state (persists across trigger calls in same process) ──────────────

_analyser  = ContextAnalyser()
_detector  = AnomalyDetector()
_memory    = SessionMemory()
_latest_ctx = None
_bridge     = None


# ── Mock hardware context ─────────────────────────────────────────────────────

def _mock_ctx(scenario: str = "nominal"):
    """Return a mock HardwareContext for testing without live engine."""
    sys.path.insert(0, str(SENTINEL_ROOT / "agent"))
    from sentinel_agent import _mock_context
    ctx = _mock_context(scenario)
    ctx.anomalies = _detector.analyse(ctx.reading)
    return ctx


# ── Trigger handlers ──────────────────────────────────────────────────────────

def handle_init() -> dict:
    """
    Called on session_start. Initialises the bridge and warms up
    the analyser with the last few log entries if available.
    """
    global _bridge, _latest_ctx

    if MOCK:
        _latest_ctx = _mock_ctx(MOCK)
        return {"ok": True, "source": "mock", "scenario": MOCK}

    log = Path(LOG_PATH)
    if not log.exists():
        return {"ok": True, "source": "none", "note": "Log not found — engine not running"}

    # Read last 10 lines to warm up the rolling window
    try:
        with open(log, "r", newline="", errors="ignore") as f:
            lines = f.readlines()
        for line in lines[-10:]:
            r = parse_log_line(line.strip())
            if r:
                ctx = _analyser.analyse(r)
                ctx.anomalies = _detector.analyse(r)
                _latest_ctx = ctx
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok":       True,
        "source":   "log",
        "log_path": LOG_PATH,
        "warmed":   _latest_ctx is not None,
    }


def handle_inject_context() -> dict:
    """
    Called before_turn. Returns the hardware context block to inject
    into the system prompt, plus the token budget for max_tokens.
    """
    global _latest_ctx

    # Get latest context
    if MOCK:
        ctx = _mock_ctx(MOCK)
    else:
        ctx = _read_latest_from_log() or _latest_ctx

    if ctx is None:
        return {
            "ok":             True,
            "system_prompt":  "## Device Hardware Context\nTelemetry unavailable — engine not running.\n",
            "max_tokens":     1024,
            "mode":           "FULL",
        }

    _latest_ctx = ctx

    # Record in session memory
    new_events = _memory.record_context(ctx)

    # Build anomaly note
    anom_note = ""
    critical_anoms = [a for a in (ctx.anomalies or [])
                      if a.severity in (Severity.HIGH, Severity.CRITICAL)]
    if critical_anoms:
        anom_note = "\n**Anomaly detected:** " + critical_anoms[0].narrative

    # Compose the full system prompt injection
    system_block = ctx.system_prompt_snippet
    if anom_note:
        system_block += anom_note
    if _memory.event_count > 1:
        system_block += "\n\n" + _memory.system_prompt_block()

    return {
        "ok":             True,
        "system_prompt":  system_block,
        "max_tokens":     ctx.token_budget,
        "mode":           ctx.recommended_mode,
        "pressure":       ctx.reading.pressure,
        "alert_flags":    ctx.alert_flags,
        "anomaly_count":  len(ctx.anomalies or []),
    }


def handle_log_event() -> dict:
    """
    Called after_turn. Appends notable events to the daily memory file.
    """
    if _latest_ctx is None:
        return {"ok": True, "logged": 0}

    # Write to memory file
    today = datetime.now().strftime("%Y-%m-%d")
    mem_dir = Path(__file__).parent.parent / "memory"
    mem_dir.mkdir(exist_ok=True)
    mem_file = mem_dir / f"{today}.md"

    events = _memory.recent_events
    if not events:
        return {"ok": True, "logged": 0}

    with open(mem_file, "a") as f:
        for ev in events:
            f.write(f"- [{ev.age_str}] {ev.summary}\n")

    return {"ok": True, "logged": len(events), "file": str(mem_file)}


def _read_latest_from_log():
    """Read the most recent valid line from the redo log."""
    try:
        log = Path(LOG_PATH)
        if not log.exists():
            return None
        with open(log, "r", newline="", errors="ignore") as f:
            lines = f.readlines()
        for line in reversed(lines):
            r = parse_log_line(line.strip())
            if r:
                ctx = _analyser.analyse(r)
                ctx.anomalies = _detector.analyse(r)
                return ctx
    except Exception:
        pass
    return None


# ── Demo mode ─────────────────────────────────────────────────────────────────

def run_demo():
    """Cycle through all hardware scenarios and print injected context."""
    print("\n" + "═" * 60)
    print("  Sentinel Hardware Skill — OpenClaw Demo")
    print("═" * 60)

    for scenario in ["nominal", "warn", "critical"]:
        os.environ["SENTINEL_MOCK"] = scenario
        globals()["MOCK"] = scenario

        result = handle_inject_context()
        mode   = result.get("mode", "?")
        budget = result.get("max_tokens", "?")
        flags  = result.get("alert_flags", [])

        colours = {"FULL": "\033[32m", "QUANTIZED": "\033[33m",
                   "MINIMAL": "\033[31m"}
        c = colours.get(mode, "")
        print(f"\n  {c}● Scenario: {scenario.upper()} → Mode: {mode} | Budget: {budget} tokens\033[0m")
        if flags:
            print(f"  Flags: {', '.join(flags)}")
        # Print first 4 lines of system prompt
        for line in result["system_prompt"].split("\n")[:6]:
            print(f"  {line}")
        time.sleep(0.3)

    print("\n" + "═" * 60 + "\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sentinel OpenClaw Skill")
    parser.add_argument("trigger", nargs="?", default="inject_context",
                        choices=["init", "inject_context", "log_event"],
                        help="Trigger to handle")
    parser.add_argument("--demo", action="store_true",
                        help="Run demo cycle across all scenarios")
    args = parser.parse_args()

    if args.demo:
        run_demo()
        sys.exit(0)

    handlers = {
        "init":           handle_init,
        "inject_context": handle_inject_context,
        "log_event":      handle_log_event,
    }

    result = handlers[args.trigger]()
    print(json.dumps(result, indent=2))
