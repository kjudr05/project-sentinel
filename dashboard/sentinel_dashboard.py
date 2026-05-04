"""
sentinel_dashboard.py
─────────────────────
Project Sentinel — Real-Time Terminal Dashboard

A rich live dashboard that shows hardware metrics, AI mode history,
alert timeline, and resource forecasts — all in the terminal.

No external TUI framework needed: pure ANSI escape sequences keep
the dependency surface tiny and the startup time under 50ms.

Run: python sentinel_dashboard.py [--log PATH] [--mock SCENARIO]
"""

import sys
import os
import time
import threading
import argparse
import shutil
from pathlib import Path
from collections import deque
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))
from sentinel_bridge import SentinelBridge, HardwareContext, HardwareReading
from sentinel_agent import _mock_context


# ─── ANSI helpers ─────────────────────────────────────────────────────────────

def _clear():
    os.system("cls" if os.name == "nt" else "clear")

def _move(row: int, col: int):
    print(f"\033[{row};{col}H", end="")

def _hide_cursor():
    print("\033[?25l", end="", flush=True)

def _show_cursor():
    print("\033[?25h", end="", flush=True)

def _color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"

RED    = "31"; YELLOW = "33"; GREEN = "32"; CYAN = "36"
WHITE  = "37"; GRAY   = "90"; BOLD  = "1";  MAGENTA = "35"

def _bar(pct: float, width: int = 20, color: str = GREEN) -> str:
    filled = int(pct / 100 * width)
    bar    = "█" * filled + "░" * (width - filled)
    return _color(bar, color)

def _pressure_color(pressure: str) -> str:
    return {"NOMINAL": GREEN, "WARN": YELLOW, "CRITICAL": RED}.get(pressure, WHITE)

def _mode_color(mode: str) -> str:
    return {"FULL": GREEN, "QUANTIZED": YELLOW, "MINIMAL": RED, "SUSPEND": MAGENTA}.get(mode, WHITE)


# ─── Dashboard State ──────────────────────────────────────────────────────────

class DashboardState:
    def __init__(self):
        self.history: deque  = deque(maxlen=60)   # 60 samples = 30s at 500ms
        self.mode_log: deque = deque(maxlen=8)
        self.alert_log: deque = deque(maxlen=6)
        self.latest: Optional[HardwareContext] = None
        self.lock = threading.Lock()
        self.sample_count = 0

    def ingest(self, ctx: HardwareContext):
        with self.lock:
            self.latest = ctx
            self.history.append(ctx)
            self.sample_count += 1

            # Log mode changes
            if not self.mode_log or self.mode_log[-1][1] != ctx.recommended_mode:
                ts = ctx.reading.ts
                self.mode_log.append((ts, ctx.recommended_mode, ctx.reading.ram_used_pct))

            # Log new alerts
            for flag in ctx.alert_flags:
                if not self.alert_log or self.alert_log[-1][1] != flag:
                    ts = ctx.reading.ts
                    self.alert_log.append((ts, flag))


# ─── Spark Line Generator ─────────────────────────────────────────────────────

_SPARK = " ▁▂▃▄▅▆▇█"

def _sparkline(values: List[float], width: int = 30) -> str:
    if not values:
        return " " * width
    tail   = list(values)[-width:]
    lo, hi = min(tail), max(tail)
    span   = hi - lo if hi != lo else 1.0
    chars  = [_SPARK[min(8, int((v - lo) / span * 8))] for v in tail]
    return "".join(chars).ljust(width)


# ─── Renderer ─────────────────────────────────────────────────────────────────

def _render(state: DashboardState, width: int, height: int):
    """Render one frame of the dashboard."""
    with state.lock:
        ctx  = state.latest
        hist = list(state.history)
        mlog = list(state.mode_log)
        alog = list(state.alert_log)

    if ctx is None:
        print("  Waiting for hardware data...", flush=True)
        return

    r   = ctx.reading
    out = []
    W   = min(width, 100)

    # ── Header ──────────────────────────────────────────────────────────────
    title  = "  ▐ Project Sentinel — Hardware-Aware AI Dashboard ▌"
    ts_str = f"  {r.ts}  LSN:{r.lsn}  Samples:{state.sample_count}"
    out.append(_color("═" * W, CYAN))
    out.append(_color(title.ljust(W), BOLD))
    out.append(_color(ts_str.ljust(W), GRAY))
    out.append(_color("═" * W, CYAN))
    out.append("")

    # ── Metric Bars ──────────────────────────────────────────────────────────
    def metric_row(label: str, pct: float, unit: str, lo: float, hi: float) -> str:
        if pct >= hi:
            col = RED
        elif pct >= lo:
            col = YELLOW
        else:
            col = GREEN
        bar = _bar(pct, 24, col)
        return (f"  {_color(label.ljust(10), WHITE)} {bar} "
                f"{_color(f'{pct:6.1f}{unit}', col)}")

    out.append(metric_row("RAM",     r.ram_used_pct, "%", 75,  90))
    out.append(metric_row("CPU",     r.cpu_pct,      "%", 70,  90))
    out.append(metric_row("TEMP",    r.thermal_c,    "°C",70,  85))
    if r.battery_pct >= 0:
        out.append(metric_row("BATTERY", r.battery_pct,  "% ",20,  10) +
                   ("  ⚡" if r.charging else "  🔋"))
    out.append(metric_row("DISK",    r.disk_pct,     "%", 85,  95))
    out.append("")

    # ── Sparklines ───────────────────────────────────────────────────────────
    ram_vals  = [c.reading.ram_used_pct for c in hist]
    cpu_vals  = [c.reading.cpu_pct      for c in hist]
    temp_vals = [c.reading.thermal_c    for c in hist]

    out.append(f"  {_color('RAM history  ', GRAY)} "
               f"{_color(_sparkline(ram_vals, 44), CYAN)}")
    out.append(f"  {_color('CPU history  ', GRAY)} "
               f"{_color(_sparkline(cpu_vals, 44), GREEN)}")
    out.append(f"  {_color('TEMP history ', GRAY)} "
               f"{_color(_sparkline(temp_vals, 44), YELLOW)}")
    out.append("")

    # ── AI Mode + Pressure ────────────────────────────────────────────────────
    pc  = _pressure_color(r.pressure)
    mc  = _mode_color(ctx.recommended_mode)
    row = (f"  Pressure: {_color(r.pressure.ljust(10), pc)}"
           f"  Trend: {_color(r.trend.ljust(10), CYAN)}"
           f"  AI Mode: {_color(ctx.recommended_mode.ljust(10), mc)}"
           f"  Token budget: {_color(str(ctx.token_budget), WHITE)}")
    out.append(row)
    out.append("")

    # ── Forecast ─────────────────────────────────────────────────────────────
    forecast_lines = _wrap(ctx.forecast, W - 4)
    out.append(f"  {_color('Forecast:', GRAY)}")
    for line in forecast_lines:
        out.append(f"    {_color(line, WHITE)}")
    out.append("")

    # ── Active Alerts ─────────────────────────────────────────────────────────
    if ctx.alert_flags:
        out.append(f"  {_color('⚠  Active alerts:', RED)}")
        for flag in ctx.alert_flags:
            out.append(f"    {_color('•', RED)} {flag}")
    else:
        out.append(f"  {_color('✓  No active alerts', GREEN)}")
    out.append("")

    # ── Mode Change Timeline ──────────────────────────────────────────────────
    out.append(f"  {_color('Mode timeline:', GRAY)}")
    for ts, mode, ram in reversed(mlog[-5:]):
        mc2 = _mode_color(mode)
        out.append(f"    {_color(ts, GRAY)}  {_color(mode.ljust(10), mc2)}  RAM:{ram:.0f}%")
    out.append("")

    # ── Alert Timeline ────────────────────────────────────────────────────────
    if alog:
        out.append(f"  {_color('Alert log:', GRAY)}")
        for ts, flag in reversed(alog[-4:]):
            out.append(f"    {_color(ts, GRAY)}  {_color(flag, YELLOW)}")
    out.append("")

    # ── Footer ────────────────────────────────────────────────────────────────
    out.append(_color("─" * W, GRAY))
    out.append(f"  {_color('Ctrl+C to exit', GRAY)}")

    # Render to terminal
    sys.stdout.write("\033[H")   # cursor home
    print("\n".join(out), flush=True)


def _wrap(text: str, width: int) -> List[str]:
    words = text.split()
    lines, current = [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return lines or [""]


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sentinel Live Dashboard")
    parser.add_argument("--log",  default="../logs/sentinel_redo.log")
    parser.add_argument("--mock", choices=["nominal", "warn", "critical"])
    args = parser.parse_args()

    state = DashboardState()

    if args.mock:
        # Simulate streaming data in mock mode
        def _mock_feeder():
            import random
            scenario = args.mock
            while True:
                ctx = _mock_context(scenario)
                # add small random jitter for realistic feel
                ctx.reading.ram_used_pct += random.uniform(-1.5, 1.5)
                ctx.reading.cpu_pct      += random.uniform(-3.0, 3.0)
                ctx.reading.ram_used_pct  = max(0, min(100, ctx.reading.ram_used_pct))
                ctx.reading.cpu_pct       = max(0, min(100, ctx.reading.cpu_pct))
                state.ingest(ctx)
                time.sleep(0.5)
        t = threading.Thread(target=_mock_feeder, daemon=True)
        t.start()
    else:
        bridge = SentinelBridge(args.log, state.ingest)
        bridge.start()

    _hide_cursor()
    _clear()

    try:
        while True:
            w, h = shutil.get_terminal_size((100, 40))
            _render(state, w, h)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        _show_cursor()
        print()


if __name__ == "__main__":
    main()
