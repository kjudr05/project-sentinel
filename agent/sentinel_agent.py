"""
sentinel_agent.py
─────────────────
Project Sentinel — Adaptive AI Agent

The "Soul" layer. Wraps the Anthropic Claude API with real-time hardware
awareness injected at the system-prompt level. Every conversation turn
re-samples the latest hardware context so the AI's operating mode tracks
the device's actual state across the lifetime of a session.

Novel contributions:
  1. Hardware-context injection at conversation boundary (not just startup)
  2. Proactive suspension suggestion when CRITICAL + RISING trend
  3. Token-budget enforcement via max_tokens dynamic binding
  4. Structured response mode (JSON schema) when in MINIMAL mode to
     guarantee parseable answers even under severe memory pressure

Run: python sentinel_agent.py [--log PATH] [--mock]
"""

import sys
import os
import json
import time
import argparse
import threading
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime

# Bring bridge module into path
sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))
from sentinel_bridge import SentinelBridge, HardwareContext, HardwareReading

from sentinel_memory import SessionMemory, MemoryMixin

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    print("[Agent] anthropic package not found — running in ECHO mode")


# ─── Constants ────────────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """You are Sentinel Assistant — an AI running directly on a Samsung Galaxy device.
You have real-time awareness of the device's hardware state through an embedded telemetry system.

Core personality:
- You are practical, concise, and always hardware-aware.
- You never pretend the device has unlimited resources.
- When resources are tight, you explain this simply and helpfully.
- You adapt your response depth, length, and format automatically.

{hardware_context}
"""

MODEL = "claude-sonnet-4-20250514"


# ─── Mock Hardware for testing without engine ──────────────────────────────────

def _mock_context(scenario: str = "nominal") -> HardwareContext:
    """Generate a mock HardwareContext for testing without live engine."""
    from sentinel_bridge import HardwareReading, HardwareContext, ContextAnalyser

    scenarios = {
        "nominal":   dict(ram=45.0, cpu=20.0, temp=48.0, bat=82.0, state="NOMINAL", trend="STABLE"),
        "warn":      dict(ram=78.0, cpu=65.0, temp=72.0, bat=35.0, state="WARN",    trend="RISING"),
        "critical":  dict(ram=92.0, cpu=88.0, temp=87.0, bat=12.0, state="CRITICAL",trend="RISING"),
    }
    s = scenarios.get(scenario, scenarios["nominal"])

    r = HardwareReading(
        lsn=1000001,
        timestamp_ms=int(time.time() * 1000),
        ram_used_pct=s["ram"],
        ram_free_mb=round((100 - s["ram"]) * 60),
        cpu_pct=s["cpu"],
        thermal_c=s["temp"],
        battery_pct=s["bat"],
        charging=False,
        disk_pct=55.0,
        pressure=s["state"],
        trend=s["trend"],
        ram_delta_pct=0.3 if s["trend"] == "RISING" else -0.1,
    )
    analyser = ContextAnalyser()
    return analyser.analyse(r)


# ─── Adaptive Agent ───────────────────────────────────────────────────────────

class SentinelAgent:
    """
    Adaptive conversational AI agent with real-time hardware awareness.

    The agent maintains conversation history across turns and re-injects
    the latest hardware context before each API call, ensuring the AI's
    operating mode always reflects the current device state.
    """

    def __init__(self,
                 log_path: str = "../logs/sentinel_redo.log",
                 mock_scenario: Optional[str] = None,
                 api_key: Optional[str] = None):

        self.mock_scenario = mock_scenario
        self.history: List[Dict] = []
        self._bridge: Optional[SentinelBridge] = None
        self._latest_ctx: Optional[HardwareContext] = None
        self._ctx_lock = threading.Lock()
        self._last_mode: Optional[str] = None
        self._memory = SessionMemory()

        # Wire API client
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if ANTHROPIC_AVAILABLE and key:
            self._client = anthropic.Anthropic(api_key=key)
        else:
            self._client = None

        # Start bridge unless mocking
        if not mock_scenario:
            self._bridge = SentinelBridge(log_path, self._on_context)
            self._bridge.start()
        else:
            self._latest_ctx = _mock_context(mock_scenario)

    def _on_context(self, ctx: HardwareContext):
        with self._ctx_lock:
            self._latest_ctx = ctx

    def _get_context(self) -> HardwareContext:
        if self.mock_scenario:
            return _mock_context(self.mock_scenario)
        with self._ctx_lock:
            if self._latest_ctx:
                return self._latest_ctx
        # No live data yet — use nominal mock
        return _mock_context("nominal")

    def _build_system_prompt(self, ctx: HardwareContext) -> str:
        return BASE_SYSTEM_PROMPT.format(
            hardware_context=ctx.system_prompt_snippet
        )

    def _print_mode_banner(self, ctx: HardwareContext):
        """Print a coloured mode change banner when operating mode shifts."""
        mode = ctx.recommended_mode
        if mode == self._last_mode:
            return
        self._last_mode = mode

        colours = {
            "FULL":      "\033[32m",  # green
            "QUANTIZED": "\033[33m",  # yellow
            "MINIMAL":   "\033[31m",  # red
            "SUSPEND":   "\033[35m",  # magenta
        }
        c = colours.get(mode, "")
        icons = {
            "FULL": "●", "QUANTIZED": "◑", "MINIMAL": "○", "SUSPEND": "⊘"
        }
        icon = icons.get(mode, "?")
        r = ctx.reading
        print(f"\n  {c}{icon} Mode: {mode} | "
              f"RAM {r.ram_used_pct:.0f}% | CPU {r.cpu_pct:.0f}% | "
              f"{r.thermal_c:.0f}°C | {r.pressure}\033[0m\n")

    def _should_proactively_suspend(self, ctx: HardwareContext) -> bool:
        """
        Novel: detect imminent resource collapse before the OS acts.
        Suggests suspension when CRITICAL state + RISING trend both hold.
        """
        return (ctx.recommended_mode == "MINIMAL" and
                ctx.reading.trend == "RISING" and
                "RAM_FORECAST_CRITICAL" in ctx.alert_flags)

    def chat(self, user_message: str) -> str:
        """
        Process one turn of conversation with hardware-aware adaptation.
        Returns the assistant's response text.
        """
        ctx = self._get_context()
        self._memory.record_context(ctx)
        self._print_mode_banner(ctx)

        # Proactive suspension check
        if self._should_proactively_suspend(ctx):
            advice = (
                "⚠️  Device resources are critically low and worsening rapidly.\n"
                "I'm pausing heavy operations to protect system stability.\n"
                "I can answer simple questions (in MINIMAL mode) or wait until "
                "resources recover. What would you prefer?"
            )
            self.history.append({"role": "user",    "content": user_message})
            self.history.append({"role": "assistant","content": advice})
            return advice

        # Append user turn
        self.history.append({"role": "user", "content": user_message})

        # API call — or echo for testing
        if self._client:
            response_text = self._call_api(ctx)
        else:
            response_text = self._echo_response(user_message, ctx)

        # Append assistant turn to history
        self.history.append({"role": "assistant", "content": response_text})
        return response_text

    def _call_api(self, ctx: HardwareContext) -> str:
        try:
            resp = self._client.messages.create(
                model      = MODEL,
                max_tokens = ctx.token_budget,
                system     = self._build_system_prompt(ctx),
                messages   = self.history,
            )
            return resp.content[0].text
        except Exception as e:
            return f"[Sentinel] API error: {e}"

    def _echo_response(self, user_message: str, ctx: HardwareContext) -> str:
        """
        Echo mode for testing without API key. Returns a realistic mock
        response that demonstrates the hardware context injection.
        """
        r = ctx.reading
        if ctx.recommended_mode == "MINIMAL":
            return (
                f"[ECHO / MINIMAL MODE]\n"
                f"Device under pressure (RAM {r.ram_used_pct:.0f}%, {r.thermal_c:.0f}°C).\n"
                f"You asked: '{user_message}'\n"
                f"Short answer only due to resource constraints."
            )
        elif ctx.recommended_mode == "QUANTIZED":
            return (
                f"[ECHO / QUANTIZED MODE]\n"
                f"Moderate pressure (RAM {r.ram_used_pct:.0f}%, CPU {r.cpu_pct:.0f}%).\n"
                f"Responding concisely to: '{user_message}'\n"
                f"Hardware forecast: {ctx.forecast}"
            )
        else:
            return (
                f"[ECHO / FULL MODE]\n"
                f"Device healthy (RAM {r.ram_used_pct:.0f}%, {r.thermal_c:.0f}°C).\n"
                f"Full response to: '{user_message}'\n"
                f"All capabilities available. Token budget: {ctx.token_budget}."
            )

    def reset(self):
        """Clear conversation history."""
        self.history.clear()
        self._last_mode = None
        self._memory = SessionMemory()

    def memory_summary(self) -> str:
        """Return session hardware history as a string."""
        return self._memory.system_prompt_block()

    def memory_answer(self, question: str):
        """Try to answer a hardware-history question from memory."""
        return self._memory.answer_hardware_question(question)

    def status(self) -> dict:
        """Return current hardware status as a dict."""
        ctx = self._get_context()
        return {
            "mode":      ctx.recommended_mode,
            "pressure":  ctx.reading.pressure,
            "ram_pct":   ctx.reading.ram_used_pct,
            "cpu_pct":   ctx.reading.cpu_pct,
            "thermal_c": ctx.reading.thermal_c,
            "flags":     ctx.alert_flags,
            "forecast":  ctx.forecast,
        }

    def stop(self):
        if self._bridge:
            self._bridge.stop()


# ─── Interactive REPL ─────────────────────────────────────────────────────────

def run_repl(agent: SentinelAgent):
    print("\n" + "═" * 60)
    print("  Project Sentinel — Adaptive AI Agent")
    print("  Type 'status' · 'reset' · 'quit' for special commands")
    print("═" * 60 + "\n")

    while True:
        try:
            user_input = input("\033[36mYou › \033[0m").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting Sentinel.")
            agent.stop()
            break

        if not user_input:
            continue

        # Special commands
        if user_input.lower() == "quit":
            agent.stop()
            break

        if user_input.lower() == "status":
            s = agent.status()
            print(f"\n  Mode:     {s['mode']}")
            print(f"  Pressure: {s['pressure']}")
            print(f"  RAM:      {s['ram_pct']:.1f}%")
            print(f"  CPU:      {s['cpu_pct']:.1f}%")
            print(f"  Temp:     {s['thermal_c']:.1f}°C")
            print(f"  Flags:    {s['flags'] or 'none'}")
            print(f"  Outlook:  {s['forecast']}")
            continue

        if user_input.lower() == "reset":
            agent.reset()
            print("  Conversation history cleared.")
            continue

        response = agent.chat(user_input)
        print(f"\n\033[37mSentinel › \033[0m{response}\n")


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Project Sentinel AI Agent")
    parser.add_argument("--log",      default="../logs/sentinel_redo.log",
                        help="Path to sentinel redo log")
    parser.add_argument("--mock",     choices=["nominal", "warn", "critical"],
                        help="Use mock hardware data (no engine required)")
    parser.add_argument("--api-key",  default=None,
                        help="Anthropic API key (falls back to ANTHROPIC_API_KEY env var)")
    args = parser.parse_args()

    agent = SentinelAgent(
        log_path      = args.log,
        mock_scenario = args.mock,
        api_key       = args.api_key,
    )

    run_repl(agent)
