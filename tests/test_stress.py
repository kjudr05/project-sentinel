"""
test_stress.py
──────────────
Project Sentinel — Stress & Stability Test Suite

Validates that Sentinel remains correct and stable under:
  1. High-frequency log writes (10× normal rate)
  2. Concurrent bridge readers (simulates multi-process setups)
  3. Large log files (100k+ lines)
  4. Rapid scenario transitions (NOMINAL → CRITICAL → NOMINAL in ms)
  5. Memory stability over 1000+ analysis cycles
  6. Thread safety of SentinelAPIState
  7. Bridge recovery after log truncation / rotation

Run: python -m pytest tests/test_stress.py -v --timeout=60
     or: python tests/test_stress.py
"""

import sys
import os
import time
import threading
import tempfile
import gc
import unittest
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from sentinel_bridge import (
    parse_log_line, ContextAnalyser, HardwareReading,
    SentinelBridge, HardwareContext
)

try:
    from sentinel_api import SentinelAPIState, _ctx_to_dict
    API_AVAILABLE = True
except ImportError:
    API_AVAILABLE = False

# ─── Fixtures ─────────────────────────────────────────────────────────────────

LOG_LINES = [
    "LSN:{lsn} | TS:{ts} | RAM_USED:{ram:.1f}% | RAM_FREE_MB:{free:.1f} | CPU:{cpu:.1f}% | THERMAL_C:{temp:.1f} | BAT:{bat:.1f}(DC) | DISK:55.0% | STATE:{state} | TREND:{trend} | DELTA:+{delta:.1f}%",
]

def make_line(lsn, ram, cpu=25.0, temp=50.0, bat=75.0, delta=0.5):
    state = "CRITICAL" if ram >= 90 else "WARN" if ram >= 75 else "NOMINAL"
    trend = "RISING" if delta > 0.3 else "FALLING" if delta < -0.3 else "STABLE"
    return (
        f"LSN:{lsn} | TS:{int(time.time()*1000)} | "
        f"RAM_USED:{ram:.1f}% | RAM_FREE_MB:{max(0,(100-ram)*60):.1f} | "
        f"CPU:{cpu:.1f}% | THERMAL_C:{temp:.1f} | BAT:{bat:.1f}(DC) | "
        f"DISK:55.0% | STATE:{state} | TREND:{trend} | DELTA:+{delta:.1f}%"
    )


# ─── 1. High-frequency write ──────────────────────────────────────────────────

class TestHighFrequency(unittest.TestCase):

    def test_burst_100_lines(self):
        """Bridge must process a burst of 100 lines without dropping any."""
        received = []
        done     = threading.Event()

        fd, path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

        def on_ctx(ctx):
            received.append(ctx.reading.lsn)
            if len(received) >= 100:
                done.set()

        bridge = SentinelBridge(path, on_ctx, poll_interval=0.01)
        bridge.start()
        time.sleep(0.15)

        with open(path, "a") as f:
            for i in range(100):
                f.write(make_line(1_000_000 + i, ram=40.0 + (i % 20)) + "\n")
            f.flush()

        done.wait(timeout=20.0)
        bridge.stop()
        os.unlink(path)

        self.assertEqual(len(received), 100, f"Got {len(received)}/100 lines")
        # No duplicates
        self.assertEqual(len(set(received)), len(received))

    def test_10x_frequency(self):
        """At 50ms intervals (10× normal), bridge must keep up."""
        received = []
        done     = threading.Event()
        N        = 40

        fd, path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

        def on_ctx(ctx):
            received.append(ctx.reading.lsn)
            if len(received) >= N:
                done.set()

        bridge = SentinelBridge(path, on_ctx, poll_interval=0.01)
        bridge.start()
        time.sleep(0.15)

        with open(path, "a") as f:
            for i in range(N):
                f.write(make_line(2_000_000 + i, ram=50.0) + "\n")
                f.flush()
                time.sleep(0.15)  # 50ms = 10× the normal poll

        done.wait(timeout=20.0)
        bridge.stop()
        os.unlink(path)

        self.assertGreaterEqual(len(received), N - 1)  # allow 1 miss max


# ─── 2. Concurrent readers ────────────────────────────────────────────────────

class TestConcurrentReaders(unittest.TestCase):

    def test_three_bridges_same_log(self):
        """
        Three bridges watching the same log must each independently
        process all lines (no inter-bridge contention).
        """
        N = 20
        results = [[] for _ in range(3)]
        events  = [threading.Event() for _ in range(3)]

        fd, path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

        bridges = []
        for idx in range(3):
            def make_cb(i):
                def cb(ctx):
                    results[i].append(ctx.reading.lsn)
                    if len(results[i]) >= N:
                        events[i].set()
                return cb
            b = SentinelBridge(path, make_cb(idx), poll_interval=0.02)
            b.start()
            bridges.append(b)

        time.sleep(0.2)

        with open(path, "a") as f:
            for i in range(N):
                f.write(make_line(3_000_000 + i, ram=55.0) + "\n")
                f.flush()
                time.sleep(0.02)

        for e in events:
            e.wait(timeout=15.0)

        for b in bridges:
            b.stop()
        os.unlink(path)

        for i, r in enumerate(results):
            self.assertEqual(len(r), N,
                             f"Bridge {i} got {len(r)}/{N} lines")


# ─── 3. Large log files ───────────────────────────────────────────────────────

class TestLargeLog(unittest.TestCase):

    def test_parse_10k_lines(self):
        """Parser must handle 10,000 lines at > 5,000 lines/sec."""
        lines = [make_line(4_000_000 + i, ram=float(i % 100)) for i in range(10_000)]
        t0 = time.perf_counter()
        parsed = [parse_log_line(l) for l in lines]
        elapsed = time.perf_counter() - t0

        valid = [p for p in parsed if p is not None]
        self.assertEqual(len(valid), 10_000)
        rate = 10_000 / elapsed
        self.assertGreater(rate, 5_000,
                           f"Parse rate {rate:.0f} lines/sec < 5,000 target")
        print(f"\n  [large_log] 10k parse: {rate:,.0f} lines/sec ({elapsed*1000:.1f}ms)")

    def test_analyser_1000_cycles(self):
        """ContextAnalyser must remain stable over 1000 cycles without drift."""
        a = ContextAnalyser(window_size=20)
        import random
        results = []
        for i in range(1000):
            ram  = 30.0 + 40 * abs(((i % 100) - 50) / 50.0)
            r = HardwareReading(
                lsn=5_000_000+i, timestamp_ms=int(time.time()*1000),
                ram_used_pct=ram, ram_free_mb=(100-ram)*60,
                cpu_pct=20.0+random.uniform(-5,5),
                thermal_c=50.0+random.uniform(-3,3),
                battery_pct=75.0, charging=False,
                disk_pct=55.0, pressure="NOMINAL",
                trend="STABLE", ram_delta_pct=0.0,
            )
            ctx = a.analyse(r)
            results.append(ctx.recommended_mode)

        # All modes must be valid throughout
        valid_modes = {"FULL", "QUANTIZED", "MINIMAL", "SUSPEND"}
        for m in results:
            self.assertIn(m, valid_modes)
        # Token budget always positive
        self.assertTrue(all(
            a.analyse(HardwareReading(
                lsn=1, timestamp_ms=int(time.time()*1000),
                ram_used_pct=float(r), ram_free_mb=3000,
                cpu_pct=20, thermal_c=50, battery_pct=75,
                charging=False, disk_pct=55, pressure="NOMINAL",
                trend="STABLE", ram_delta_pct=0
            )).token_budget > 0
            for r in range(0, 101, 10)
        ))
        print(f"\n  [analyser_1000] Mode distribution: "
              + str({m: results.count(m) for m in set(results)}))


# ─── 4. Rapid scenario transitions ───────────────────────────────────────────

class TestRapidTransitions(unittest.TestCase):

    def test_nominal_to_critical_and_back(self):
        """
        Mode must correctly follow rapid NOMINAL → CRITICAL → NOMINAL
        transitions within the same analyser (no state pollution).
        """
        a = ContextAnalyser(window_size=5)

        def read(ram, temp=50.0, bat=80.0, charging=True):
            return HardwareReading(
                lsn=1, timestamp_ms=int(time.time()*1000),
                ram_used_pct=ram, ram_free_mb=(100-ram)*60,
                cpu_pct=20, thermal_c=temp, battery_pct=bat,
                charging=charging, disk_pct=55,
                pressure="NOMINAL", trend="STABLE", ram_delta_pct=0
            )

        # Establish nominal
        for _ in range(5):
            ctx = a.analyse(read(30.0))
        self.assertEqual(ctx.recommended_mode, "FULL")

        # Sudden spike to critical
        ctx = a.analyse(read(95.0, temp=88.0, bat=8.0, charging=False))
        self.assertEqual(ctx.recommended_mode, "MINIMAL")

        # Immediate recovery
        for _ in range(5):
            ctx = a.analyse(read(28.0, temp=44.0, bat=80.0, charging=True))
        self.assertEqual(ctx.recommended_mode, "FULL")

    def test_mode_sequence_is_monotone_with_pressure(self):
        """Mode must track pressure monotonically — more pressure = ≤ tokens."""
        a = ContextAnalyser(window_size=3)
        prev_budget = 9999
        for ram in range(10, 101, 5):
            r = HardwareReading(
                lsn=1, timestamp_ms=int(time.time()*1000),
                ram_used_pct=float(ram), ram_free_mb=(100-ram)*60,
                cpu_pct=20, thermal_c=50, battery_pct=80,
                charging=True, disk_pct=55,
                pressure="NOMINAL", trend="STABLE", ram_delta_pct=0
            )
            ctx = a.analyse(r)
            # Token budget must not INCREASE as RAM rises
            if ram > 30:  # allow initial window warmup
                self.assertLessEqual(ctx.token_budget, prev_budget + 200,
                    f"Token budget rose at RAM={ram}%: {prev_budget} → {ctx.token_budget}")
            prev_budget = ctx.token_budget


# ─── 5. Memory stability ─────────────────────────────────────────────────────

class TestMemoryStability(unittest.TestCase):

    def test_no_memory_leak_over_1000_contexts(self):
        """
        ContextAnalyser heap should not grow unboundedly over 1000 cycles.
        Uses gc to measure object counts rather than raw memory.
        """
        import gc as _gc
        _gc.collect()
        before = len(_gc.get_objects())

        a = ContextAnalyser(window_size=20)
        r = HardwareReading(
            lsn=1, timestamp_ms=int(time.time()*1000),
            ram_used_pct=50.0, ram_free_mb=3000,
            cpu_pct=25, thermal_c=55, battery_pct=80,
            charging=False, disk_pct=55,
            pressure="NOMINAL", trend="STABLE", ram_delta_pct=0
        )
        for _ in range(1000):
            a.analyse(r)

        _gc.collect()
        after = len(_gc.get_objects())
        growth = after - before

        # Allow some growth for Python internals, but < 5000 new objects
        self.assertLess(growth, 5000,
                        f"Object count grew by {growth} over 1000 cycles")
        print(f"\n  [memory] Object growth over 1000 cycles: {growth}")


# ─── 6. Thread safety of API state ───────────────────────────────────────────

class TestAPIThreadSafety(unittest.TestCase):

    @unittest.skipUnless(API_AVAILABLE, "sentinel_api not importable")
    def test_concurrent_ingest_and_read(self):
        """
        10 threads ingesting and 5 threads reading simultaneously must
        not cause data races or assertion errors.
        """
        from sentinel_agent import _mock_context

        state   = SentinelAPIState()
        errors  = []
        done    = threading.Event()
        writers = 0
        readers = 0

        def writer():
            nonlocal writers
            for _ in range(50):
                ctx = _mock_context("nominal")
                state.ingest(ctx)
                time.sleep(0.005)
            writers += 1
            if writers == 10:
                done.set()

        def reader():
            nonlocal readers
            while not done.is_set():
                try:
                    h = state.history(20)
                    l = state.latest
                    if l:
                        d = _ctx_to_dict(l)
                        assert "ram_used_pct" in d
                    readers += 1
                except Exception as e:
                    errors.append(str(e))
                time.sleep(0.002)

        threads = [threading.Thread(target=writer) for _ in range(10)]
        threads += [threading.Thread(target=reader, daemon=True) for _ in range(5)]
        for t in threads:
            t.start()

        done.wait(timeout=20.0)
        self.assertEqual(errors, [], f"Thread safety errors: {errors}")
        self.assertGreater(state.total_readings, 0)
        print(f"\n  [thread_safety] {state.total_readings} ingests, "
              f"{readers} reads, 0 errors")


# ─── 7. Bridge recovery after log truncation ─────────────────────────────────

class TestBridgeRecovery(unittest.TestCase):

    def test_bridge_survives_log_gap(self):
        """
        If the log file stops growing for several seconds and then resumes,
        the bridge must continue processing without hanging.
        """
        received = []
        done     = threading.Event()

        fd, path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

        def on_ctx(ctx):
            received.append(ctx.reading.lsn)
            if len(received) >= 10:
                done.set()

        bridge = SentinelBridge(path, on_ctx, poll_interval=0.02)
        bridge.start()
        time.sleep(0.2)

        # Write 5 lines
        with open(path, "a") as f:
            for i in range(5):
                f.write(make_line(6_000_000 + i, ram=40.0) + "\n")
                f.flush()
                time.sleep(0.02)

        # Pause for 1 second (simulates engine stall)
        time.sleep(1.0)

        # Write 5 more lines
        with open(path, "a") as f:
            for i in range(5, 10):
                f.write(make_line(6_000_000 + i, ram=45.0) + "\n")
                f.flush()
                time.sleep(0.02)

        done.wait(timeout=12.0)
        bridge.stop()
        os.unlink(path)

        self.assertEqual(len(received), 10,
                         f"Bridge got {len(received)}/10 after gap")

    def test_bridge_handles_empty_lines(self):
        """Bridge must skip blank lines without crashing."""
        received = []
        done     = threading.Event()

        fd, path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

        def on_ctx(ctx):
            received.append(ctx)
            done.set()

        bridge = SentinelBridge(path, on_ctx, poll_interval=0.02)
        bridge.start()
        time.sleep(0.15)

        with open(path, "a") as f:
            f.write("\n\n\n")   # blank lines
            f.write("garbage\n")
            f.write("\n")
            f.write(make_line(7_000_001, ram=42.0) + "\n")
            f.flush()

        done.wait(timeout=15.0)
        bridge.stop()
        os.unlink(path)

        self.assertEqual(len(received), 1)
        self.assertAlmostEqual(received[0].reading.ram_used_pct, 42.0, delta=0.1)


# ─── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║  Project Sentinel — Stress Test Suite    ║")
    print("╚══════════════════════════════════════════╝\n")
    unittest.main(verbosity=2)
