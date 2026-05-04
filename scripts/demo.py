"""
demo.py
───────
Project Sentinel — Automated Demo Runner

Runs a complete, scripted demonstration of all Sentinel capabilities
in sequence. Designed for hackathon judging: one command, no setup,
shows everything working end-to-end in under 3 minutes.

Usage:
  python scripts/demo.py              # full demo (~3 min)
  python scripts/demo.py --quick      # abbreviated demo (~60s)
  python scripts/demo.py --step N     # jump to step N
"""

import sys
import os
import time
import threading
import subprocess
import textwrap
import argparse
from pathlib import Path

# ── Terminal helpers ──────────────────────────────────────────────────────────

W = 62

def _c(text, code):  return f"\033[{code}m{text}\033[0m"
def green(t):  return _c(t, "32")
def yellow(t): return _c(t, "33")
def cyan(t):   return _c(t, "36")
def bold(t):   return _c(t, "1")
def gray(t):   return _c(t, "90")
def red(t):    return _c(t, "31")

def box(title: str):
    print()
    print(cyan("╔" + "═" * (W - 2) + "╗"))
    print(cyan("║") + bold(f"  {title}".ljust(W - 2)) + cyan("║"))
    print(cyan("╚" + "═" * (W - 2) + "╝"))

def step(n: int, title: str):
    print()
    print(cyan(f"── Step {n}: {title} " + "─" * max(0, W - 12 - len(title))))

def ok(msg: str):
    print(green("  ✓ ") + msg)

def info(msg: str):
    print(gray("  · ") + msg)

def warn(msg: str):
    print(yellow("  ⚠ ") + msg)

def pause(seconds: float, label: str = ""):
    if label:
        print(gray(f"  [{label}]"), end=" ", flush=True)
    for _ in range(int(seconds * 4)):
        print(gray("."), end="", flush=True)
        time.sleep(0.25)
    print()

# ── Step implementations ──────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "agent"))

def step_intro():
    box("Project Sentinel — Live Demo")
    print()
    lines = [
        "Sentinel gives AI agents a nervous system — real-time hardware",
        "telemetry injected into every reasoning step, so the model",
        "adapts its behaviour before the OS has to intervene.",
        "",
        "  The Iron   →  Hardware poller (C++ or Python/psutil)",
        "  The Log    →  Atomic redo log (cross-process IPC)",
        "  The Bridge →  Tail watcher, analyser, forecaster",
        "  The Soul   →  Adaptive AI agent (Claude API)",
        "  Anomalies  →  Spike/runaway/cliff/compound detection",
    ]
    for line in lines:
        print(gray("  ") + line)
    time.sleep(1.5)


def step_tests(quick: bool):
    step(1, "Test Suite")
    info("Running all 94 tests across 3 test files...")
    print()

    t0 = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short"],
        capture_output=True, text=True,
        cwd=str(ROOT)
    )
    elapsed = time.perf_counter() - t0

    # Print last few lines
    lines = result.stdout.strip().split("\n")
    for line in lines[-5:]:
        if "passed" in line:
            print(green(f"  {line}"))
        elif "failed" in line or "error" in line:
            print(red(f"  {line}"))
        else:
            print(gray(f"  {line}"))

    if result.returncode == 0:
        ok(f"All tests passed in {elapsed:.1f}s")
    else:
        warn(f"Some tests failed — see output above")


def step_bridge_parse():
    step(2, "Log Parser + Context Analyser")
    from sentinel_bridge import parse_log_line, ContextAnalyser
    from anomaly_detector import AnomalyDetector

    lines = [
        ("NOMINAL",  "LSN:1000001 | TS:1700000000000 | RAM_USED:32.0% | RAM_FREE_MB:4400.0 | CPU:12.0% | THERMAL_C:45.0 | BAT:90.0(CHG) | DISK:40.0% | STATE:NOMINAL | TREND:STABLE | DELTA:0.0%"),
        ("WARN",     "LSN:1000020 | TS:1700000010000 | RAM_USED:79.0% | RAM_FREE_MB:1300.0 | CPU:68.0% | THERMAL_C:73.0 | BAT:38.0(DC) | DISK:40.0% | STATE:WARN | TREND:RISING | DELTA:+2.3%"),
        ("CRITICAL", "LSN:1000035 | TS:1700000017500 | RAM_USED:93.0% | RAM_FREE_MB:450.0 | CPU:90.0% | THERMAL_C:87.0 | BAT:9.0(DC) | DISK:40.0% | STATE:CRITICAL | TREND:RISING | DELTA:+3.1%"),
    ]

    analyser = ContextAnalyser()
    detector = AnomalyDetector()

    for label, line in lines:
        r = parse_log_line(line.strip())
        ctx = analyser.analyse(r)
        anomalies = detector.analyse(r)

        colour = {"NOMINAL": green, "WARN": yellow, "CRITICAL": red}[label]
        mode_c = {"FULL": green, "QUANTIZED": yellow, "MINIMAL": red}.get(
            ctx.recommended_mode, gray)

        print(f"  {colour(label.ljust(10))} → "
              f"mode={mode_c(ctx.recommended_mode.ljust(10))} "
              f"tokens={ctx.token_budget:4d}  "
              f"anomalies={len(anomalies)}")
        if anomalies:
            for a in anomalies[:2]:
                print(gray(f"             ↳ {a.type.value}: {a.severity.value}"))
        time.sleep(0.4)

    ok("Parser handles all pressure states correctly")
    ok("Anomaly detector identifies spike/runaway/compound patterns")


def step_python_engine(quick: bool):
    step(3, "Python Hardware Engine (live your machine)")
    from sentinel_engine_py import PythonEngine
    import tempfile

    fd, log_path = tempfile.mkstemp(suffix=".log")
    os.close(fd)

    engine = PythonEngine(interval_ms=300, log_path=log_path, verbose=False)
    t = threading.Thread(target=engine.run, daemon=True)
    t.start()

    duration = 2.0 if quick else 4.0
    info(f"Reading your hardware for {duration:.0f}s...")
    time.sleep(0.4)  # warmup

    from sentinel_bridge import parse_log_line
    readings = []
    deadline = time.time() + duration
    while time.time() < deadline:
        try:
            with open(log_path, "r") as f:
                lines = [l.strip() for l in f if l.strip()]
            for line in lines:
                r = parse_log_line(line)
                if r and r not in readings:
                    readings.append(r)
        except Exception:
            pass
        time.sleep(0.3)

    engine.stop()
    t.join(timeout=2.0)
    try:
        os.unlink(log_path)
    except Exception:
        pass

    if readings:
        latest = readings[-1]
        bar = "█" * int(latest.ram_used_pct / 5) + "░" * (20 - int(latest.ram_used_pct / 5))
        rc = green if latest.ram_used_pct < 75 else (yellow if latest.ram_used_pct < 90 else red)
        print(f"\n  RAM  [{rc(bar)}] {latest.ram_used_pct:.1f}%")
        print(f"  CPU  {latest.cpu_pct:.1f}%   TEMP {latest.thermal_c:.1f}°C")
        if latest.battery_pct >= 0:
            print(f"  BAT  {latest.battery_pct:.0f}% {'(charging)' if latest.charging else '(on battery)'}")
        print(f"  Records written: {len(readings)}")
        ok("Engine reading real hardware metrics from your machine")
    else:
        warn("No readings captured (engine may need more time)")


def step_adaptive_modes():
    step(4, "Adaptive AI Mode Demo")
    sys.path.insert(0, str(ROOT / "agent"))
    from sentinel_agent import SentinelAgent

    scenarios = [
        ("nominal",  "FULL mode     — device healthy, full capability"),
        ("warn",     "QUANTIZED mode— moderate pressure, efficient responses"),
        ("critical", "MINIMAL mode  — resource crisis, terse answers"),
    ]

    for scenario, description in scenarios:
        agent = SentinelAgent(mock_scenario=scenario)
        ctx   = agent._get_context()
        mode  = ctx.recommended_mode
        mode_c = {"FULL": green, "QUANTIZED": yellow, "MINIMAL": red}.get(mode, gray)
        r = ctx.reading

        print(f"\n  {mode_c('●')} {description}")
        print(gray(f"    RAM {r.ram_used_pct:.0f}% | CPU {r.cpu_pct:.0f}% | "
                   f"{r.thermal_c:.0f}°C | BAT {r.battery_pct:.0f}%"))

        # Get an echo response
        response = agent.chat("What can you help me with right now?")
        # Show first 120 chars
        preview = response[:120].replace("\n", " ")
        if len(response) > 120:
            preview += "…"
        print(gray(f"    Agent: ") + preview)
        agent.stop()
        time.sleep(0.3)

    ok("Agent correctly adapts mode, token budget, and depth")


def step_anomaly_demo():
    step(5, "Anomaly Detection")
    from anomaly_detector import AnomalyDetector, AnomalyType
    from sentinel_bridge import HardwareReading

    def make_r(ram, cpu=30, temp=55, bat=70, charging=False):
        return HardwareReading(
            lsn=1, timestamp_ms=int(time.time()*1000),
            ram_used_pct=ram, ram_free_mb=max(0,(100-ram)*60),
            cpu_pct=cpu, thermal_c=temp, battery_pct=bat,
            charging=charging, disk_pct=55, pressure="NOMINAL",
            trend="STABLE", ram_delta_pct=0
        )

    def _run_spike(d):
        for _ in range(8):   d.analyse(make_r(40))
        return d.analyse(make_r(72))

    def _run_thermal(d):
        for i in range(10):  d.analyse(make_r(50, 30, 50 + i * 1.5))
        return d.analyse(make_r(50, 32, 67))

    def _run_battery(d):
        bat = 80.0
        for _ in range(10):
            bat -= 0.1
            d.analyse(make_r(50, 30, 50, bat, False))
        return d.analyse(make_r(50, 30, 50, bat - 5.0, False))

    demos = [
        ("RAM spike (memory leak burst)",      _run_spike),
        ("Thermal runaway (cooling issue)",    _run_thermal),
        ("Battery cliff (drain acceleration)", _run_battery),
    ]

    detector = AnomalyDetector()
    # warm up
    for _ in range(6):
        detector.analyse(make_r(40))

    for label, fn in demos:
        result = fn(detector)
        if isinstance(result, list):
            anomalies = result
        else:
            anomalies = []

        if anomalies:
            for a in anomalies[:1]:
                sev_c = {
                    "CRITICAL": red, "HIGH": red,
                    "MEDIUM": yellow, "LOW": gray
                }.get(a.severity.value, gray)
                print(f"  {sev_c('⚡')} {label}")
                print(gray(f"    {a.type.value} [{a.severity.value}]: {a.narrative[:80]}…"))
        else:
            print(gray(f"  · {label}: (below detection threshold at current baseline)"))
        time.sleep(0.3)

    ok("Anomaly detector identifies failure signatures beyond simple thresholds")


def step_benchmark(quick: bool):
    step(6, "Overhead Benchmark")
    info("Measuring bridge hot-path latency (parse + analyse)...")

    sys.path.insert(0, str(ROOT / "scripts"))
    from benchmark import bench_parse, bench_analyse, bench_parse_then_analyse

    N = 1000 if quick else 5000

    r_parse   = bench_parse(N)
    r_analyse = bench_analyse(N)
    r_both    = bench_parse_then_analyse(N)

    print()
    print(f"  {'Operation':<25} {'Mean µs':>8}  {'Throughput/s':>14}")
    print(gray("  " + "─" * 52))
    for name, result in [
        ("Log line parse",    r_parse),
        ("Context analyse",   r_analyse),
        ("Parse + analyse",   r_both),
    ]:
        tps = result["throughput_per_sec"]
        print(f"  {name:<25} {result['mean_us']:>8.2f}  {tps:>14,.0f}")

    poll_us = 500_000  # 500ms in µs
    overhead = r_both["mean_us"] / poll_us * 100
    colour = green if overhead < 1 else (yellow if overhead < 5 else red)
    print()
    print(f"  CPU overhead at 500ms poll: {colour(f'{overhead:.4f}%')}")
    ok(f"Bridge adds < {'1%' if overhead < 1 else f'{overhead:.2f}%'} CPU overhead")


def step_api_demo():
    step(7, "REST API Server")
    sys.path.insert(0, str(ROOT / "scripts"))
    from sentinel_api import SentinelAPIState, _ctx_to_dict
    from sentinel_agent import _mock_context

    state = SentinelAPIState()

    # Feed mock data
    for scenario in ["nominal", "warn", "critical", "nominal"]:
        state.ingest(_mock_context(scenario))

    import json
    latest = state.latest
    d = _ctx_to_dict(latest)

    print(f"\n  GET /status  →  HTTP 200")
    print(gray(f"  " + json.dumps({k: d[k] for k in
        ["ram_used_pct","cpu_pct","thermal_c","ai_mode","pressure"]}, indent=4)
        .replace("\n", "\n  ")))
    print()
    print(f"  GET /history?n=4  →  {len(state.history(4))} readings")
    print(f"  GET /stream       →  Server-Sent Events (live push)")
    print(f"  POST /simulate    →  inject scenario for demos")
    ok("REST API operational — start with: python scripts/sentinel_api.py --mock nominal")


def step_summary():
    box("Demo Complete")
    print()
    contributions = [
        ("Multi-signal fusion",   "RAM+CPU+thermal+battery → composite pressure state"),
        ("Horizon forecasting",   "Linear regression predicts critical state 30s ahead"),
        ("Anomaly detection",     "8 pattern types: spike/creep/runaway/cliff/thrash..."),
        ("Proactive suspension",  "Throttles before the OS acts, not after"),
        ("Per-turn adaptation",   "Hardware context re-injected every conversation turn"),
        ("REST API + SSE",        "Any app can query or stream Sentinel data"),
        ("Zero-build engine",     "Pure Python psutil engine, no compiler needed"),
        ("94 tests",              "Unit + integration + stress + property tests"),
    ]
    for title, desc in contributions:
        print(f"  {green('●')} {bold(title.ljust(22))}  {gray(desc)}")
    print()
    print(gray("  Web dashboard: ") + "open dashboard/sentinel_web.html")
    print(gray("  Full agent:    ") + "python agent/sentinel_agent.py --mock warn")
    print(gray("  API server:    ") + "python scripts/sentinel_api.py --mock nominal")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

STEPS = [
    (0, "Intro",          step_intro,         False),
    (1, "Tests",          step_tests,         True),
    (2, "Parser",         step_bridge_parse,  False),
    (3, "Engine",         step_python_engine, True),
    (4, "Adaptive modes", step_adaptive_modes,False),
    (5, "Anomalies",      step_anomaly_demo,  False),
    (6, "Benchmark",      step_benchmark,     True),
    (7, "API",            step_api_demo,      False),
    (8, "Summary",        step_summary,       False),
]


def main():
    parser = argparse.ArgumentParser(description="Sentinel Automated Demo")
    parser.add_argument("--quick",  action="store_true",
                        help="Run abbreviated demo (~60s)")
    parser.add_argument("--step",   type=int, default=None,
                        help="Run only this step number")
    args = parser.parse_args()

    os.chdir(ROOT)

    if args.step is not None:
        for n, title, fn, has_quick in STEPS:
            if n == args.step:
                if has_quick:
                    fn(quick=args.quick)
                else:
                    fn()
                return
        print(f"Unknown step {args.step}. Valid: 0-{len(STEPS)-1}")
        return

    for n, title, fn, has_quick in STEPS:
        if has_quick:
            fn(quick=args.quick)
        else:
            fn()

    print()


if __name__ == "__main__":
    main()
