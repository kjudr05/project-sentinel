"""
sentinel_benchmark.py
─────────────────────
Project Sentinel — Overhead & Latency Benchmark

Measures and reports:
  1. C++ engine parse throughput (lines/sec)
  2. Bridge analysis latency (µs per reading)
  3. Context build latency (µs)
  4. Rolling forecast latency (µs)
  5. End-to-end pipeline latency (engine write → agent context ready)
  6. CPU overhead estimate vs a 500ms polling interval
  7. Memory footprint of the bridge process

Produces a machine-readable JSON report + a human-readable summary.
Run: python scripts/benchmark.py [--iterations N] [--output FILE]
"""

import sys
import os
import time
import json
import gc
import statistics
import argparse
import tracemalloc
from pathlib import Path
from typing import List, Dict, Any

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))
from sentinel_bridge import (
    parse_log_line, ContextAnalyser, HardwareReading,
    SentinelBridge
)

import tempfile, threading

# ─── Sample data ─────────────────────────────────────────────────────────────

SAMPLE_LINES = [
    "LSN:1000001 | TS:1700000000000 | RAM_USED:42.3% | RAM_FREE_MB:3584.0 | CPU:18.5% | THERMAL_C:55.0 | BAT:82.0(DC) | DISK:61.0% | STATE:NOMINAL | TREND:STABLE | DELTA:+0.1%",
    "LSN:1000002 | TS:1700000000500 | RAM_USED:78.1% | RAM_FREE_MB:1382.0 | CPU:67.3% | THERMAL_C:71.0 | BAT:45.0(DC) | DISK:61.0% | STATE:WARN | TREND:RISING | DELTA:+2.4%",
    "LSN:1000003 | TS:1700000001000 | RAM_USED:93.7% | RAM_FREE_MB:401.0 | CPU:89.0% | THERMAL_C:87.0 | BAT:9.0(DC) | DISK:61.0% | STATE:CRITICAL | TREND:RISING | DELTA:+3.1%",
    "LSN:1000004 | TS:1700000001500 | RAM_USED:30.0% | RAM_FREE_MB:4400.0 | CPU:8.0% | THERMAL_C:43.0 | BAT:99.0(CHG) | DISK:61.0% | STATE:NOMINAL | TREND:FALLING | DELTA:-2.0%",
]

def make_reading(ram: float = 50.0, cpu: float = 25.0, temp: float = 55.0) -> HardwareReading:
    import time as _t
    return HardwareReading(
        lsn=1000001, timestamp_ms=int(_t.time()*1000),
        ram_used_pct=ram, ram_free_mb=(100-ram)*60,
        cpu_pct=cpu, thermal_c=temp,
        battery_pct=75.0, charging=False,
        disk_pct=55.0, pressure="NOMINAL",
        trend="STABLE", ram_delta_pct=0.0,
    )

# ─── Benchmark helpers ────────────────────────────────────────────────────────

def bench(fn, iterations: int) -> Dict[str, float]:
    """Run fn() N times and return latency stats in microseconds."""
    gc.disable()
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        fn()
        times.append((time.perf_counter_ns() - t0) / 1_000.0)  # → µs
    gc.enable()
    return {
        "iterations": iterations,
        "mean_us":    statistics.mean(times),
        "median_us":  statistics.median(times),
        "p95_us":     sorted(times)[int(0.95 * len(times))],
        "p99_us":     sorted(times)[int(0.99 * len(times))],
        "min_us":     min(times),
        "max_us":     max(times),
        "stdev_us":   statistics.stdev(times) if len(times) > 1 else 0.0,
        "throughput_per_sec": 1_000_000.0 / statistics.mean(times),
    }

# ─── Individual benchmarks ────────────────────────────────────────────────────

def bench_parse(iterations: int) -> Dict[str, Any]:
    """How fast can we parse a log line from the C++ engine?"""
    lines = SAMPLE_LINES * (iterations // len(SAMPLE_LINES) + 1)
    idx   = [0]

    def fn():
        parse_log_line(lines[idx[0] % len(lines)])
        idx[0] += 1

    result = bench(fn, iterations)
    result["name"] = "log_parse"
    result["description"] = "Regex parse of one redo-log line"
    return result


def bench_analyse(iterations: int) -> Dict[str, Any]:
    """How fast is the ContextAnalyser.analyse() call?"""
    analyser  = ContextAnalyser(window_size=20)
    reading   = make_reading()

    def fn():
        analyser.analyse(reading)

    result = bench(fn, iterations)
    result["name"] = "context_analyse"
    result["description"] = "Full ContextAnalyser.analyse() including forecast"
    return result


def bench_forecast(iterations: int) -> Dict[str, Any]:
    """Isolate the linear-regression forecast."""
    analyser = ContextAnalyser(window_size=20)
    # Pre-fill window
    for i in range(20):
        analyser.ingest(make_reading(ram=40.0 + i * 2))

    def fn():
        analyser._linear_forecast(analyser._ram_window, horizon_samples=6)

    result = bench(fn, iterations)
    result["name"] = "linear_forecast"
    result["description"] = "Linear regression over 20-sample window"
    return result


def bench_parse_then_analyse(iterations: int) -> Dict[str, Any]:
    """Full pipeline: parse + analyse (what the bridge does per line)."""
    analyser  = ContextAnalyser(window_size=20)
    lines     = SAMPLE_LINES * (iterations // len(SAMPLE_LINES) + 1)
    idx       = [0]

    def fn():
        r = parse_log_line(lines[idx[0] % len(lines)])
        if r:
            analyser.analyse(r)
        idx[0] += 1

    result = bench(fn, iterations)
    result["name"] = "parse_plus_analyse"
    result["description"] = "Parse + full context analysis (bridge hot path)"
    return result


def bench_memory() -> Dict[str, Any]:
    """Peak memory usage of a ContextAnalyser with a full 20-sample window."""
    tracemalloc.start()
    snapshot_before = tracemalloc.take_snapshot()

    analyser = ContextAnalyser(window_size=20)
    for i in range(20):
        analyser.analyse(make_reading(ram=float(i * 4), cpu=float(i * 2)))

    snapshot_after = tracemalloc.take_snapshot()
    tracemalloc.stop()

    stats = snapshot_after.compare_to(snapshot_before, "lineno")
    total_bytes = sum(s.size_diff for s in stats if s.size_diff > 0)

    return {
        "name": "memory_footprint",
        "description": "Peak heap delta for ContextAnalyser (20 samples)",
        "total_bytes":  total_bytes,
        "total_kb":     total_bytes / 1024.0,
    }


def bench_e2e_pipeline(n_lines: int = 50) -> Dict[str, Any]:
    """
    True end-to-end: write lines to a temp log, measure time until
    bridge fires callback for the last line.

    Cross-platform fix:
    - Removed hardcoded Linux-only /tmp
    - Uses system default temp directory automatically
    """
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".log",
        delete=False
    ) as f:
        log_path = f.name

    latencies = []
    done = threading.Event()
    write_ts = [0.0]

    def on_ctx(ctx):
        recv_ts = time.perf_counter_ns()
        latency_us = (recv_ts - write_ts[0]) / 1_000.0
        latencies.append(latency_us)

        if len(latencies) >= n_lines:
            done.set()

    bridge = SentinelBridge(log_path, on_ctx, poll_interval=0.01)
    bridge.start()
    time.sleep(0.05)  # let bridge settle

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            for i, line in enumerate(
                SAMPLE_LINES * (n_lines // len(SAMPLE_LINES) + 1)
            ):
                if i >= n_lines:
                    break

                write_ts[0] = time.perf_counter_ns()
                f.write(line + "\n")
                f.flush()

                # Ensure immediate disk write
                os.fsync(f.fileno())

                # Stagger writes to avoid batching
                time.sleep(0.015)

        done.wait(timeout=10.0)

    finally:
        bridge.stop()

        # Safe cleanup
        try:
            if os.path.exists(log_path):
                os.unlink(log_path)
        except PermissionError:
            pass

    if not latencies:
        return {
            "name": "e2e_pipeline",
            "error": "No callbacks received"
        }

    return {
        "name": "e2e_pipeline",
        "description": "Write → bridge callback latency (file IPC)",
        "n_samples": len(latencies),
        "mean_us": statistics.mean(latencies),
        "median_us": statistics.median(latencies),
        "p95_us": sorted(latencies)[int(0.95 * len(latencies))],
        "p99_us": sorted(latencies)[int(0.99 * len(latencies))],
        "min_us": min(latencies),
        "max_us": max(latencies),
    }

def compute_cpu_overhead(hot_path_mean_us: float,
                          poll_interval_ms: int = 500) -> Dict[str, float]:
    """
    Estimate CPU overhead of the bridge as a fraction of the poll interval.
    The engine writes every 500ms. The bridge does parse+analyse per line.
    """
    poll_us    = poll_interval_ms * 1_000.0
    overhead   = hot_path_mean_us / poll_us
    return {
        "poll_interval_us":  poll_us,
        "hot_path_us":       hot_path_mean_us,
        "overhead_fraction": overhead,
        "overhead_pct":      overhead * 100.0,
    }

# ─── Report Renderer ─────────────────────────────────────────────────────────

def _bar(val: float, max_val: float, width: int = 30, fill: str = "█") -> str:
    n = int(val / max_val * width) if max_val > 0 else 0
    return fill * n + "░" * (width - n)

def print_report(results: Dict[str, Any]):
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║       Project Sentinel — Overhead Benchmark          ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    micro_benchmarks = [
        results.get("log_parse"),
        results.get("linear_forecast"),
        results.get("context_analyse"),
        results.get("parse_plus_analyse"),
    ]

    print("  ── Micro-benchmarks (latency per operation) ─────────")
    print(f"  {'Name':<25} {'Mean µs':>9} {'P95 µs':>9} {'P99 µs':>9}  {'Throughput/s':>14}")
    print("  " + "─" * 72)

    max_mean = max((b["mean_us"] for b in micro_benchmarks if b), default=1)
    for b in micro_benchmarks:
        if not b:
            continue
        bar = _bar(b["mean_us"], max_mean, 10)
        print(f"  {b['name']:<25} {b['mean_us']:>9.2f} {b['p95_us']:>9.2f} "
              f"{b['p99_us']:>9.2f}  {b['throughput_per_sec']:>14,.0f}  {bar}")

    print()
    e2e = results.get("e2e_pipeline", {})
    if "mean_us" in e2e:
        print("  ── End-to-End Pipeline Latency ──────────────────────")
        print(f"  Write → bridge callback:  mean {e2e['mean_us']:,.0f} µs "
              f"({e2e['mean_us']/1000:.1f} ms)")
        print(f"  P95:  {e2e['p95_us']:,.0f} µs    P99: {e2e['p99_us']:,.0f} µs")
        print(f"  Min:  {e2e['min_us']:,.0f} µs    Max: {e2e['max_us']:,.0f} µs")
        print()

    cpu = results.get("cpu_overhead", {})
    if cpu:
        pct = cpu["overhead_pct"]
        colour = "\033[32m" if pct < 1 else ("\033[33m" if pct < 5 else "\033[31m")
        print("  ── CPU Overhead Estimate ─────────────────────────────")
        print(f"  Poll interval:   {cpu['poll_interval_us']:,.0f} µs  (500 ms)")
        print(f"  Bridge hot path: {cpu['hot_path_us']:.2f} µs  (parse + analyse)")
        print(f"  Overhead:        {colour}{pct:.4f}%\033[0m  "
              f"{'✓ Well within budget' if pct < 1 else '⚠ Above 1% target'}")
        print()

    mem = results.get("memory_footprint", {})
    if mem:
        print("  ── Memory Footprint ──────────────────────────────────")
        print(f"  ContextAnalyser (20 samples): {mem['total_kb']:.1f} KB heap delta")
        print()

    print("  ── Summary ───────────────────────────────────────────")
    pp = results.get("parse_plus_analyse", {})
    if pp:
        tps = pp["throughput_per_sec"]
        max_freq = tps / 1.0   # bridge can handle this many lines/sec
        print(f"  Bridge can sustain: {tps:,.0f} lines/sec")
        print(f"  At 500ms poll:      {int(tps * 0.5)} lines/burst supported")
        print(f"  Headroom vs demand: {tps / 2.0:,.0f}× faster than required")
    print()

# ─── Entry Point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sentinel Overhead Benchmark")
    parser.add_argument("--iterations", type=int, default=10_000,
                        help="Micro-benchmark iterations (default: 10,000)")
    parser.add_argument("--e2e-lines", type=int, default=30,
                        help="End-to-end lines to send (default: 30)")
    parser.add_argument("--output", default=None,
                        help="Write JSON report to this file")
    args = parser.parse_args()

    N = args.iterations
    print(f"  Running {N:,} iterations per micro-benchmark...")
    print(f"  End-to-end test: {args.e2e_lines} lines through full pipeline\n")

    results = {}

    print("  [1/6] Parsing ...")
    results["log_parse"]          = bench_parse(N)

    print("  [2/6] Forecasting ...")
    results["linear_forecast"]    = bench_forecast(N)

    print("  [3/6] Analysing ...")
    results["context_analyse"]    = bench_analyse(N)

    print("  [4/6] Parse + Analyse (hot path) ...")
    results["parse_plus_analyse"] = bench_parse_then_analyse(N)

    print("  [5/6] End-to-end pipeline latency ...")
    results["e2e_pipeline"]       = bench_e2e_pipeline(args.e2e_lines)

    print("  [6/6] Memory footprint ...")
    results["memory_footprint"]   = bench_memory()

    # Compute overhead
    pp_mean = results["parse_plus_analyse"]["mean_us"]
    results["cpu_overhead"] = compute_cpu_overhead(pp_mean, poll_interval_ms=500)

    print_report(results)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"  JSON report written to: {args.output}\n")

    return results


if __name__ == "__main__":
    main()
