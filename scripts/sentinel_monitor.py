"""
sentinel_monitor.py
───────────────────
Project Sentinel — All-in-One Monitor

Single command that starts all Sentinel components simultaneously:
  1. Python hardware engine (polls your real hardware every 500ms)
  2. REST API server (http://localhost:7474)
  3. Opens the web dashboard in your browser

This is the "just works" entry point for demos and judging.

Usage:
  python scripts/sentinel_monitor.py              # live hardware
  python scripts/sentinel_monitor.py --mock warn  # simulated hardware
  python scripts/sentinel_monitor.py --no-browser # headless (server only)
"""

import sys
import os
import time
import signal
import threading
import argparse
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))

# ── Colour helpers ────────────────────────────────────────────────────────────
def _c(t, code): return f"\033[{code}m{t}\033[0m"
def green(t):  return _c(t, "32")
def yellow(t): return _c(t, "33")
def cyan(t):   return _c(t, "36")
def gray(t):   return _c(t, "90")
def bold(t):   return _c(t, "1")

# ── Shared stop event ─────────────────────────────────────────────────────────
_stop = threading.Event()

def _handle_signal(sig, frame):
    print(f"\n\n  {yellow('Stopping all Sentinel components...')}")
    _stop.set()

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Component 1: Hardware engine ──────────────────────────────────────────────

def run_engine(mock: str, log_path: str):
    if mock:
        # Replay mock data continuously into the log
        from sentinel_agent import _mock_context
        from sentinel_bridge import ContextAnalyser
        import random, re

        def _ctx_to_log_line(lsn, ctx):
            r = ctx.reading
            chg = "CHG" if r.charging else "DC"
            ts = int(time.time() * 1000)
            return (
                f"LSN:{lsn} | TS:{ts} | RAM_USED:{r.ram_used_pct:.1f}% | "
                f"RAM_FREE_MB:{r.ram_free_mb:.1f} | CPU:{r.cpu_pct:.1f}% | "
                f"THERMAL_C:{r.thermal_c:.1f} | BAT:{r.battery_pct:.1f}({chg}) | "
                f"DISK:{r.disk_pct:.1f}% | STATE:{r.pressure} | "
                f"TREND:{r.trend} | DELTA:{r.ram_delta_pct:+.1f}%\n"
            )

        scenarios = {"nominal": 0, "warn": 1, "critical": 2}
        lsn = 2_000_000
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", buffering=1) as f:
            while not _stop.is_set():
                ctx = _mock_context(mock)
                ctx.reading.ram_used_pct += random.uniform(-1.5, 1.5)
                ctx.reading.cpu_pct      += random.uniform(-3, 3)
                ctx.reading.ram_used_pct  = max(0, min(100, ctx.reading.ram_used_pct))
                ctx.reading.cpu_pct       = max(0, min(100, ctx.reading.cpu_pct))
                lsn += 1
                f.write(_ctx_to_log_line(lsn, ctx))
                _stop.wait(0.5)
    else:
        from sentinel_engine_py import PythonEngine
        engine = PythonEngine(
            interval_ms=500,
            log_path=log_path,
            verbose=False,
        )
        t = threading.Thread(target=engine.run, daemon=True)
        t.start()
        _stop.wait()
        engine.stop()


# ── Component 2: API server ───────────────────────────────────────────────────

def run_api(log_path: str, mock: str, port: int):
    from sentinel_api import SentinelAPIState, make_server, SentinelBridge
    from sentinel_agent import _mock_context

    state = SentinelAPIState()

    if mock:
        def _mock_feeder():
            import random
            while not _stop.is_set():
                ctx = _mock_context(mock)
                ctx.reading.ram_used_pct += random.uniform(-2, 2)
                ctx.reading.cpu_pct      += random.uniform(-4, 4)
                ctx.reading.ram_used_pct  = max(0, min(100, ctx.reading.ram_used_pct))
                ctx.reading.cpu_pct       = max(0, min(100, ctx.reading.cpu_pct))
                state.ingest(ctx)
                _stop.wait(0.5)
        threading.Thread(target=_mock_feeder, daemon=True).start()
    else:
        bridge = SentinelBridge(log_path, state.ingest)
        bridge.start()

    server = make_server(port, state)
    server.timeout = 0.5

    while not _stop.is_set():
        server.handle_request()

    server.server_close()


# ── Status display ────────────────────────────────────────────────────────────

def run_status_display(log_path: str, mock: str, port: int):
    """Print a live status line in the terminal while everything runs."""
    from sentinel_bridge import parse_log_line, ContextAnalyser
    analyser = ContextAnalyser()

    time.sleep(1.5)  # let engine warm up

    counter = 0
    while not _stop.is_set():
        try:
            if mock:
                # read from API
                import urllib.request, json
                with urllib.request.urlopen(
                    f"http://localhost:{port}/status", timeout=1
                ) as r:
                    d = json.loads(r.read())
                ram  = d.get("ram_used_pct", 0)
                cpu  = d.get("cpu_pct", 0)
                temp = d.get("thermal_c", 0)
                mode = d.get("ai_mode", "?")
                pres = d.get("pressure", "?")
            else:
                # read from log
                log = Path(log_path)
                if not log.exists():
                    time.sleep(1); continue
                with open(log, "r", newline="", errors="ignore") as f:
                    lines = f.readlines()
                r = None
                for line in reversed(lines):
                    r = parse_log_line(line.strip())
                    if r: break
                if not r: time.sleep(0.5); continue
                ctx  = analyser.analyse(r)
                ram  = r.ram_used_pct
                cpu  = r.cpu_pct
                temp = r.thermal_c
                mode = ctx.recommended_mode
                pres = r.pressure

            mode_c = {"FULL": "32", "QUANTIZED": "33",
                      "MINIMAL": "31", "SUSPEND": "35"}.get(mode, "37")
            pres_c = {"NOMINAL": "32", "WARN": "33", "CRITICAL": "31"}.get(pres, "37")
            bar = "█" * int(ram / 5) + "░" * (20 - int(ram / 5))

            print(
                f"\r  RAM [\033[{pres_c}m{bar}\033[0m] "
                f"{ram:5.1f}% | CPU {cpu:5.1f}% | {temp:5.1f}°C | "
                f"Mode: \033[{mode_c}m{mode}\033[0m  ",
                end="", flush=True
            )
        except Exception:
            pass
        _stop.wait(0.5)
        counter += 1

    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sentinel All-in-One Monitor — starts engine + API + dashboard"
    )
    parser.add_argument("--mock",       choices=["nominal", "warn", "critical"],
                        help="Use simulated hardware (no engine needed)")
    parser.add_argument("--port",       type=int, default=7474,
                        help="API server port (default: 7474)")
    parser.add_argument("--log",        default=str(ROOT / "logs" / "sentinel_redo.log"),
                        help="Log file path")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't open the dashboard in browser")
    args = parser.parse_args()

    # Banner
    print()
    print(cyan("  ╔══════════════════════════════════════════╗"))
    print(cyan("  ║  ") + bold("Project Sentinel — All-in-One Monitor") + cyan("  ║"))
    print(cyan("  ╚══════════════════════════════════════════╝"))
    print()
    src = f"mock:{args.mock}" if args.mock else "live hardware"
    print(f"  {gray('Source:')}  {src}")
    print(f"  {gray('Log:')}     {args.log}")
    print(f"  {gray('API:')}     http://localhost:{args.port}")
    dashboard = str(ROOT / "dashboard" / "sentinel_web.html")
    print(f"  {gray('UI:')}      {dashboard}")
    print(f"\n  {gray('Ctrl+C to stop all components')}\n")

    # Start components in threads
    threads = [
        threading.Thread(
            target=run_engine,
            args=(args.mock or "", args.log),
            daemon=True, name="engine"
        ),
        threading.Thread(
            target=run_api,
            args=(args.log, args.mock or "", args.port),
            daemon=True, name="api"
        ),
        threading.Thread(
            target=run_status_display,
            args=(args.log, args.mock or "", args.port),
            daemon=True, name="display"
        ),
    ]

    for t in threads:
        t.start()

    # Give engine a moment to write first entry
    time.sleep(1.2)

    print(f"  {green('✓')} Engine running  ({src})")
    print(f"  {green('✓')} API server      http://localhost:{args.port}/status")

    # Open dashboard
    if not args.no_browser:
        webbrowser.open(dashboard)
        print(f"  {green('✓')} Dashboard       opened in browser")

    print()

    # Wait for stop signal
    _stop.wait()

    print(f"  {yellow('All components stopped.')}")


if __name__ == "__main__":
    main()
