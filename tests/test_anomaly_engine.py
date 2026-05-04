"""
test_anomaly_engine.py
──────────────────────
Tests for:
  - AnomalyDetector (all anomaly types)
  - Python engine (log format compatibility, metric reading)
  - sentinel_engine_py.py output format
"""

import sys
import os
import re
import time
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from anomaly_detector import (
    AnomalyDetector, Anomaly, AnomalyType, Severity, RollingStat
)
from sentinel_bridge import parse_log_line, HardwareReading


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def make_r(ram=40.0, cpu=20.0, temp=50.0, bat=75.0,
           charging=False, disk=55.0, pressure="NOMINAL") -> HardwareReading:
    return HardwareReading(
        lsn=1, timestamp_ms=int(time.time() * 1000),
        ram_used_pct=ram, ram_free_mb=max(0, (100-ram)*60),
        cpu_pct=cpu, thermal_c=temp, battery_pct=bat,
        charging=charging, disk_pct=disk, pressure=pressure,
        trend="STABLE", ram_delta_pct=0.0,
    )

def warm_up(detector: AnomalyDetector, n: int = 8,
            ram=40.0, cpu=20.0, temp=50.0):
    """Feed n stable readings to establish a baseline."""
    for i in range(n):
        detector.analyse(make_r(
            ram=ram + (i % 2) * 0.3,  # tiny jitter
            cpu=cpu, temp=temp
        ))


# ─── RollingStat unit tests ───────────────────────────────────────────────────

class TestRollingStat(unittest.TestCase):

    def test_mean_single_value(self):
        s = RollingStat(10)
        s.push(50.0)
        self.assertAlmostEqual(s.mean, 50.0)

    def test_mean_multiple(self):
        s = RollingStat(10)
        for v in [10, 20, 30]:
            s.push(v)
        self.assertAlmostEqual(s.mean, 20.0)

    def test_window_evicts_old(self):
        s = RollingStat(3)
        for v in [10, 20, 30, 100]:
            s.push(v)
        # Window: [20, 30, 100] — 10 is evicted
        self.assertAlmostEqual(s.mean, 50.0)

    def test_z_score_zero_for_stable(self):
        s = RollingStat(10)
        for _ in range(10):
            s.push(50.0)
        self.assertAlmostEqual(s.z_score, 0.0, delta=0.01)

    def test_z_score_high_for_outlier(self):
        s = RollingStat(20)
        for _ in range(19):
            s.push(40.0)
        s.push(100.0)
        self.assertGreater(s.z_score, 2.0)

    def test_trend_slope_rising(self):
        s = RollingStat(10)
        for i in range(10):
            s.push(float(i * 5))
        self.assertGreater(s.trend_slope, 0)

    def test_trend_slope_stable(self):
        s = RollingStat(10)
        for _ in range(10):
            s.push(50.0)
        self.assertAlmostEqual(s.trend_slope, 0.0, delta=0.01)

    def test_time_to_threshold_rising(self):
        s = RollingStat(10)
        for i in range(10):
            s.push(40.0 + i * 2.0)   # rises 2%/sample
        tta = s.time_to_threshold(80.0)
        self.assertIsNotNone(tta)
        self.assertGreater(tta, 0)

    def test_time_to_threshold_none_if_stable(self):
        s = RollingStat(10)
        for _ in range(10):
            s.push(40.0)
        self.assertIsNone(s.time_to_threshold(80.0))

    def test_variance_ratio_detects_oscillation(self):
        s = RollingStat(20)
        # Baseline with small natural variance (avoids division-by-zero)
        for i in range(15):
            s.push(48.0 + (i % 3))   # slight jitter: 48, 49, 50, 48, 49...
        # Highly oscillating recent values
        for v in [20, 90, 15, 95, 10]:
            s.push(v)
        self.assertGreater(s.variance_ratio, 3.0)

    def test_delta_z_score_spike(self):
        s = RollingStat(10)
        for _ in range(8):
            s.push(40.0)
        s.push(41.0)   # normal delta
        s.push(65.0)   # large delta
        self.assertGreater(s.delta_z_score, 1.5)

    def test_ready_after_5_samples(self):
        s = RollingStat(10)
        for i in range(4):
            s.push(float(i))
            self.assertFalse(s.ready)
        s.push(4.0)
        self.assertTrue(s.ready)


# ─── AnomalyDetector tests ────────────────────────────────────────────────────

class TestAnomalyDetector(unittest.TestCase):

    def setUp(self):
        self.d = AnomalyDetector()

    def _warm(self, ram=40.0, cpu=20.0, temp=50.0, n=8):
        warm_up(self.d, n, ram, cpu, temp)

    # ── RAM Spike ──────────────────────────────────────────────────────────────

    def test_ram_spike_detected(self):
        self._warm(ram=40.0)
        anomalies = self.d.analyse(make_r(ram=70.0))
        types = [a.type for a in anomalies]
        self.assertIn(AnomalyType.RAM_SPIKE, types)

    def test_ram_spike_severity_high_for_large_jump(self):
        self._warm(ram=40.0)
        anomalies = self.d.analyse(make_r(ram=60.0))
        spikes = [a for a in anomalies if a.type == AnomalyType.RAM_SPIKE]
        if spikes:
            self.assertIn(spikes[0].severity,
                          (Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL))

    def test_no_spike_for_small_change(self):
        self._warm(ram=50.0)
        anomalies = self.d.analyse(make_r(ram=51.5))
        spikes = [a for a in anomalies if a.type == AnomalyType.RAM_SPIKE]
        self.assertEqual(spikes, [])

    def test_spike_has_narrative(self):
        self._warm(ram=40.0)
        anomalies = self.d.analyse(make_r(ram=75.0))
        spikes = [a for a in anomalies if a.type == AnomalyType.RAM_SPIKE]
        if spikes:
            self.assertGreater(len(spikes[0].narrative), 10)
            self.assertGreater(len(spikes[0].ai_hint), 5)

    # ── RAM Creep ──────────────────────────────────────────────────────────────

    def test_ram_creep_detected(self):
        d = AnomalyDetector()
        # Feed 20 readings rising at 1%/sample → at sample 20, ram≈70%
        # Threshold is 85%, slope≈1/sample → TTA≈15 samples → within 60s horizon
        for i in range(20):
            d.analyse(make_r(ram=50.0 + i * 1.0))
        anomalies = d.analyse(make_r(ram=70.5))
        types = [a.type for a in anomalies]
        self.assertIn(AnomalyType.RAM_CREEP, types)

    def test_no_creep_for_stable(self):
        self._warm(ram=50.0, n=20)
        anomalies = self.d.analyse(make_r(ram=50.5))
        creeps = [a for a in anomalies if a.type == AnomalyType.RAM_CREEP]
        self.assertEqual(creeps, [])

    # ── Thermal Runaway ────────────────────────────────────────────────────────

    def test_thermal_runaway_detected(self):
        d = AnomalyDetector()
        # Stable CPU, rising temperature (thermal runaway signature)
        for i in range(10):
            d.analyse(make_r(ram=50, cpu=30, temp=55.0 + i * 1.5))
        anomalies = d.analyse(make_r(ram=50, cpu=31, temp=55.0 + 11 * 1.5))
        types = [a.type for a in anomalies]
        self.assertIn(AnomalyType.THERMAL_RUNAWAY, types)

    def test_no_runaway_when_cpu_also_rising(self):
        d = AnomalyDetector()
        # CPU rising much faster than temp = expected proportional heating
        # temp_slope ≈ 0.5°C/sample, cpu_slope ≈ 6%/sample → ratio 0.08 < threshold 0.3
        for i in range(10):
            d.analyse(make_r(ram=50, cpu=20 + i * 6, temp=50 + i * 0.5))
        anomalies = d.analyse(make_r(ram=50, cpu=80, temp=55))
        runaway = [a for a in anomalies if a.type == AnomalyType.THERMAL_RUNAWAY]
        self.assertEqual(runaway, [])

    # ── Battery Cliff ──────────────────────────────────────────────────────────

    def test_battery_cliff_detected(self):
        d = AnomalyDetector()
        bat = 80.0
        # Normal slow drain
        for _ in range(10):
            bat -= 0.1
            d.analyse(make_r(bat=bat, charging=False))
        # Sudden large drain
        bat -= 5.0
        anomalies = d.analyse(make_r(bat=bat, charging=False))
        types = [a.type for a in anomalies]
        self.assertIn(AnomalyType.BATTERY_CLIFF, types)

    def test_no_cliff_when_charging(self):
        d = AnomalyDetector()
        for _ in range(10):
            d.analyse(make_r(bat=80, charging=True))
        anomalies = d.analyse(make_r(bat=70, charging=True))
        cliff = [a for a in anomalies if a.type == AnomalyType.BATTERY_CLIFF]
        self.assertEqual(cliff, [])

    def test_no_cliff_for_no_battery(self):
        d = AnomalyDetector()
        for _ in range(10):
            d.analyse(make_r(bat=-1, charging=True))
        anomalies = d.analyse(make_r(bat=-1, charging=True))
        cliff = [a for a in anomalies if a.type == AnomalyType.BATTERY_CLIFF]
        self.assertEqual(cliff, [])

    # ── CPU Thrash ─────────────────────────────────────────────────────────────

    def test_cpu_thrash_detected(self):
        d = AnomalyDetector()
        # Establish stable mid-level baseline
        for _ in range(10):
            d.analyse(make_r(cpu=50.0))
        # Inject oscillation
        for v in [10, 95, 5, 98, 8, 92]:
            d.analyse(make_r(cpu=v))
        anomalies = d.analyse(make_r(cpu=90))
        types = [a.type for a in anomalies]
        self.assertIn(AnomalyType.CPU_THRASH, types)

    def test_no_thrash_for_stable_cpu(self):
        d = AnomalyDetector()
        for _ in range(20):
            d.analyse(make_r(cpu=45.0))
        anomalies = d.analyse(make_r(cpu=47.0))
        thrash = [a for a in anomalies if a.type == AnomalyType.CPU_THRASH]
        self.assertEqual(thrash, [])

    # ── Compound Pressure ──────────────────────────────────────────────────────

    def test_compound_pressure_detected(self):
        d = AnomalyDetector()
        self._warm_det(d)
        # All signals bad but none individually crossing single threshold
        anomalies = d.analyse(make_r(
            ram=73, cpu=67, temp=69, bat=22, charging=False, disk=87
        ))
        types = [a.type for a in anomalies]
        self.assertIn(AnomalyType.COMPOUND_PRESSURE, types)

    def _warm_det(self, d, n=8):
        for _ in range(n):
            d.analyse(make_r())

    # ── Recovery Detection ─────────────────────────────────────────────────────

    def test_recovery_detected_after_critical(self):
        d = AnomalyDetector()
        # Simulate critical state
        for _ in range(6):
            r = make_r(ram=93, cpu=90, temp=87, bat=8, charging=False)
            r.pressure = "CRITICAL"
            d.analyse(r)
        # Sudden recovery (e.g. user closed heavy app)
        anomalies = d.analyse(make_r(ram=35, cpu=15, temp=48))
        types = [a.type for a in anomalies]
        self.assertIn(AnomalyType.RECOVERY_DETECTED, types)

    def test_no_recovery_without_prior_critical(self):
        d = AnomalyDetector()
        self._warm_det(d, ram=50)
        anomalies = d.analyse(make_r(ram=35))
        recovery = [a for a in anomalies if a.type == AnomalyType.RECOVERY_DETECTED]
        self.assertEqual(recovery, [])

    # ── Properties ────────────────────────────────────────────────────────────

    def test_no_anomalies_during_warmup(self):
        """Should not fire during first 6 samples (insufficient history)."""
        d = AnomalyDetector()
        for i in range(5):
            anomalies = d.analyse(make_r(ram=float(20 + i * 10)))
            self.assertEqual(anomalies, [],
                             f"Got anomalies during warmup sample {i}: {anomalies}")

    def test_anomaly_has_required_fields(self):
        d = AnomalyDetector()
        self._warm_det(d)
        d.analyse(make_r(ram=40))  # stable
        # Inject a spike
        anomalies = d.analyse(make_r(ram=80))
        for a in anomalies:
            self.assertIsInstance(a.type,      AnomalyType)
            self.assertIsInstance(a.severity,  Severity)
            self.assertIsInstance(a.narrative, str)
            self.assertIsInstance(a.metric,    str)
            self.assertGreater(a.timestamp_ms, 0)

    def test_summary_keys(self):
        d = AnomalyDetector()
        s = d.summary()
        self.assertIn("samples", s)
        self.assertIn("ram",     s)
        self.assertIn("cpu",     s)
        self.assertIn("temp",    s)

    def test_severity_ordering(self):
        """Critical anomalies must appear before lower-severity ones."""
        d = AnomalyDetector()
        self._warm_det(d, ram=40)
        # Inject spike (should be CRITICAL/HIGH) + compound
        anomalies = d.analyse(make_r(
            ram=80, cpu=68, temp=68, bat=18, charging=False
        ))
        if len(anomalies) > 1:
            sev_order = {Severity.CRITICAL: 0, Severity.HIGH: 1,
                         Severity.MEDIUM: 2, Severity.LOW: 3}
            for i in range(len(anomalies) - 1):
                self.assertLessEqual(
                    sev_order[anomalies[i].severity],
                    sev_order[anomalies[i+1].severity],
                    "Anomalies not sorted by severity"
                )

    def test_is_critical_property(self):
        a_critical = Anomaly(
            type=AnomalyType.RAM_SPIKE, severity=Severity.CRITICAL,
            metric="ram", value=95, baseline=40, deviation=3,
            narrative="test", ai_hint=""
        )
        a_low = Anomaly(
            type=AnomalyType.RECOVERY_DETECTED, severity=Severity.LOW,
            metric="ram", value=35, baseline=50, deviation=-1,
            narrative="test", ai_hint=""
        )
        self.assertTrue(a_critical.is_critical)
        self.assertFalse(a_low.is_critical)

    def _warm_det(self, d, ram=40.0, n=8):
        for _ in range(n):
            d.analyse(make_r(ram=ram))


# ─── Python Engine output format tests ───────────────────────────────────────

class TestPythonEngine(unittest.TestCase):

    def test_format_entry_parseable_by_bridge(self):
        """Engine output must be parseable by the bridge's regex."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from sentinel_engine_py import format_entry

        line = format_entry(
            lsn=2_000_001, ts_ms=1700000000000,
            ram_pct=55.3, ram_free_mb=2800.0,
            cpu_pct=34.1, thermal_c=62.0,
            bat_pct=75.0, charging=False,
            disk_pct=61.0,
            state="NOMINAL", trend="STABLE", delta=0.5
        )
        line = line.strip()
        r = parse_log_line(line)
        self.assertIsNotNone(r, f"Bridge failed to parse engine output:\n{line}")
        self.assertEqual(r.lsn, 2_000_001)
        self.assertAlmostEqual(r.ram_used_pct, 55.3)
        self.assertAlmostEqual(r.cpu_pct, 34.1)
        self.assertFalse(r.charging)

    def test_format_entry_charging_flag(self):
        from sentinel_engine_py import format_entry
        line = format_entry(
            lsn=2, ts_ms=int(time.time()*1000),
            ram_pct=40.0, ram_free_mb=3800.0,
            cpu_pct=10.0, thermal_c=45.0,
            bat_pct=90.0, charging=True,
            disk_pct=50.0,
            state="NOMINAL", trend="STABLE", delta=0.0
        ).strip()
        r = parse_log_line(line)
        self.assertIsNotNone(r)
        self.assertTrue(r.charging)

    def test_format_entry_no_battery(self):
        from sentinel_engine_py import format_entry
        line = format_entry(
            lsn=3, ts_ms=int(time.time()*1000),
            ram_pct=40.0, ram_free_mb=3800.0,
            cpu_pct=10.0, thermal_c=45.0,
            bat_pct=-1.0, charging=True,
            disk_pct=50.0,
            state="NOMINAL", trend="STABLE", delta=0.0
        ).strip()
        r = parse_log_line(line)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r.battery_pct, -1.0)

    def test_engine_writes_to_log(self):
        """Engine must write parseable lines to the log file."""
        from sentinel_engine_py import PythonEngine

        fd, log_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

        engine = PythonEngine(interval_ms=100, log_path=log_path,
                              verbose=False, extended=False)

        t = threading.Thread(target=engine.run, daemon=True)
        t.start()
        time.sleep(1.2)  # warmup (2 silent samples) + at least 4 logged samples
        engine.stop()
        t.join(timeout=3.0)

        with open(log_path, "r") as f:
            lines = [l.strip() for l in f if l.strip()]

        os.unlink(log_path)

        self.assertGreater(len(lines), 0, "Engine wrote no lines")
        for line in lines:
            r = parse_log_line(line)
            self.assertIsNotNone(r, f"Engine output not parseable:\n{line}")
            self.assertGreaterEqual(r.lsn, 2_000_000)
            self.assertGreaterEqual(r.ram_used_pct, 0)
            self.assertLessEqual(r.ram_used_pct, 100)

    def test_engine_lsn_monotonically_increasing(self):
        """LSN values must be strictly increasing."""
        from sentinel_engine_py import PythonEngine

        fd, log_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

        engine = PythonEngine(interval_ms=80, log_path=log_path,
                              verbose=False, extended=False)
        t = threading.Thread(target=engine.run, daemon=True)
        t.start()
        time.sleep(0.5)
        engine.stop()
        t.join(timeout=2.0)

        with open(log_path, "r") as f:
            lines = [l.strip() for l in f if l.strip()]

        os.unlink(log_path)
        lsns = [parse_log_line(l).lsn for l in lines if parse_log_line(l)]
        for i in range(1, len(lsns)):
            self.assertGreater(lsns[i], lsns[i-1],
                               f"LSN not monotonic: {lsns[i-1]} → {lsns[i]}")


# ─── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔══════════════════════════════════════════════╗")
    print("║  Sentinel — Anomaly & Engine Test Suite      ║")
    print("╚══════════════════════════════════════════════╝\n")
    unittest.main(verbosity=2)
