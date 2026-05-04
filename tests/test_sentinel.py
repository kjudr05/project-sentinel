"""
test_sentinel.py
────────────────
Project Sentinel — Full Test Suite

Tests every layer:
  - Log parser (unit)
  - Context analyser (unit + property tests)
  - Bridge integration (component)
  - Agent adaptive logic (integration)
  - End-to-end: engine → log → bridge → agent (system)

Run: python -m pytest tests/test_sentinel.py -v
  or: python tests/test_sentinel.py
"""

import sys
import os
import re
import json
import time
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from dataclasses import asdict

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "agent"))

from sentinel_bridge import (
    HardwareReading, HardwareContext, ContextAnalyser,
    SentinelBridge, parse_log_line
)
from sentinel_agent import SentinelAgent, _mock_context


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def make_reading(**kwargs) -> HardwareReading:
    defaults = dict(
        lsn=1000001, timestamp_ms=int(time.time()*1000),
        ram_used_pct=45.0, ram_free_mb=3000.0,
        cpu_pct=20.0, thermal_c=50.0,
        battery_pct=80.0, charging=True,
        disk_pct=55.0, pressure="NOMINAL",
        trend="STABLE", ram_delta_pct=0.0,
    )
    defaults.update(kwargs)
    return HardwareReading(**defaults)

SAMPLE_LOG_LINE = (
    "LSN:1000042 | TS:1700000123456 | RAM_USED:67.3% | RAM_FREE_MB:2048.5 | "
    "CPU:34.1% | THERMAL_C:62.0 | BAT:55.0(DC) | DISK:71.2% | "
    "STATE:NOMINAL | TREND:RISING | DELTA:+0.8%"
)

SAMPLE_LOG_LINE_CRITICAL = (
    "LSN:1000099 | TS:1700000999000 | RAM_USED:93.5% | RAM_FREE_MB:420.0 | "
    "CPU:91.0% | THERMAL_C:88.0 | BAT:8.0(DC) | DISK:55.0% | "
    "STATE:CRITICAL | TREND:RISING | DELTA:+2.1%"
)

SAMPLE_LOG_LINE_CHARGING = (
    "LSN:1000010 | TS:1700000001000 | RAM_USED:40.0% | RAM_FREE_MB:3800.0 | "
    "CPU:12.0% | THERMAL_C:45.0 | BAT:90.0(CHG) | DISK:30.0% | "
    "STATE:NOMINAL | TREND:STABLE | DELTA:-0.1%"
)


# ─── Unit Tests: Log Parser ────────────────────────────────────────────────────

class TestLogParser(unittest.TestCase):

    def test_parse_nominal_line(self):
        r = parse_log_line(SAMPLE_LOG_LINE)
        self.assertIsNotNone(r)
        self.assertEqual(r.lsn, 1000042)
        self.assertEqual(r.timestamp_ms, 1700000123456)
        self.assertAlmostEqual(r.ram_used_pct, 67.3)
        self.assertAlmostEqual(r.ram_free_mb,  2048.5)
        self.assertAlmostEqual(r.cpu_pct,  34.1)
        self.assertAlmostEqual(r.thermal_c, 62.0)
        self.assertAlmostEqual(r.battery_pct, 55.0)
        self.assertFalse(r.charging)
        self.assertAlmostEqual(r.disk_pct, 71.2)
        self.assertEqual(r.pressure, "NOMINAL")
        self.assertEqual(r.trend, "RISING")
        self.assertAlmostEqual(r.ram_delta_pct, 0.8)

    def test_parse_critical_line(self):
        r = parse_log_line(SAMPLE_LOG_LINE_CRITICAL)
        self.assertIsNotNone(r)
        self.assertEqual(r.pressure, "CRITICAL")
        self.assertEqual(r.trend, "RISING")
        self.assertAlmostEqual(r.ram_used_pct, 93.5)
        self.assertAlmostEqual(r.thermal_c, 88.0)

    def test_parse_charging_flag(self):
        r = parse_log_line(SAMPLE_LOG_LINE_CHARGING)
        self.assertIsNotNone(r)
        self.assertTrue(r.charging)
        self.assertAlmostEqual(r.battery_pct, 90.0)

    def test_parse_garbage_returns_none(self):
        self.assertIsNone(parse_log_line(""))
        self.assertIsNone(parse_log_line("not a sentinel log line"))
        self.assertIsNone(parse_log_line("LSN:abc | broken"))

    def test_parse_negative_delta(self):
        line = SAMPLE_LOG_LINE.replace("DELTA:+0.8%", "DELTA:-1.2%")
        r = parse_log_line(line)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r.ram_delta_pct, -1.2)

    def test_ts_property(self):
        r = parse_log_line(SAMPLE_LOG_LINE)
        ts = r.ts
        self.assertRegex(ts, r"\d{2}:\d{2}:\d{2}")

    def test_roundtrip_log_format(self):
        """Parse must survive the exact format produced by the C++ engine."""
        lines = [SAMPLE_LOG_LINE, SAMPLE_LOG_LINE_CRITICAL, SAMPLE_LOG_LINE_CHARGING]
        for line in lines:
            r = parse_log_line(line)
            self.assertIsNotNone(r, f"Failed to parse: {line}")


# ─── Unit Tests: ContextAnalyser ──────────────────────────────────────────────

class TestContextAnalyser(unittest.TestCase):

    def setUp(self):
        self.analyser = ContextAnalyser()

    def test_nominal_mode_full(self):
        r = make_reading(ram_used_pct=30.0, cpu_pct=15.0, thermal_c=45.0,
                         battery_pct=90.0, charging=False, pressure="NOMINAL")
        ctx = self.analyser.analyse(r)
        self.assertEqual(ctx.recommended_mode, "FULL")
        self.assertEqual(ctx.reasoning_depth, "DEEP")
        self.assertEqual(ctx.token_budget, 1024)
        self.assertEqual(ctx.alert_flags, [])

    def test_warn_mode_quantized(self):
        r = make_reading(ram_used_pct=80.0, cpu_pct=60.0, thermal_c=72.0,
                         battery_pct=30.0, charging=False, pressure="WARN")
        ctx = self.analyser.analyse(r)
        self.assertEqual(ctx.recommended_mode, "QUANTIZED")
        self.assertIn("RAM_WARN", ctx.alert_flags)
        self.assertIn("THERMAL_WARN", ctx.alert_flags)

    def test_critical_mode_minimal(self):
        r = make_reading(ram_used_pct=93.0, cpu_pct=90.0, thermal_c=89.0,
                         battery_pct=8.0, charging=False, pressure="CRITICAL")
        ctx = self.analyser.analyse(r)
        self.assertEqual(ctx.recommended_mode, "MINIMAL")
        self.assertEqual(ctx.reasoning_depth, "SHALLOW")
        self.assertLessEqual(ctx.token_budget, 256)
        self.assertIn("RAM_CRITICAL", ctx.alert_flags)
        self.assertIn("THERMAL_CRITICAL", ctx.alert_flags)
        self.assertIn("BATTERY_CRITICAL", ctx.alert_flags)

    def test_battery_critical_alone_triggers_minimal(self):
        r = make_reading(ram_used_pct=40.0, cpu_pct=20.0, thermal_c=50.0,
                         battery_pct=5.0, charging=False, pressure="WARN")
        ctx = self.analyser.analyse(r)
        self.assertIn("BATTERY_CRITICAL", ctx.alert_flags)
        self.assertEqual(ctx.recommended_mode, "MINIMAL")

    def test_battery_ignored_when_charging(self):
        r = make_reading(ram_used_pct=40.0, cpu_pct=20.0, thermal_c=50.0,
                         battery_pct=5.0, charging=True, pressure="NOMINAL")
        ctx = self.analyser.analyse(r)
        self.assertNotIn("BATTERY_CRITICAL", ctx.alert_flags)

    def test_no_battery_device(self):
        r = make_reading(battery_pct=-1.0, charging=True)
        ctx = self.analyser.analyse(r)
        self.assertNotIn("BATTERY_CRITICAL", ctx.alert_flags)
        self.assertNotIn("BATTERY_LOW", ctx.alert_flags)

    def test_forecast_narrative_non_empty(self):
        r = make_reading()
        ctx = self.analyser.analyse(r)
        self.assertIsInstance(ctx.forecast, str)
        self.assertGreater(len(ctx.forecast), 10)

    def test_system_prompt_contains_hardware(self):
        r = make_reading(ram_used_pct=77.0)
        ctx = self.analyser.analyse(r)
        self.assertIn("77", ctx.system_prompt_snippet)
        self.assertIn("Sentinel", ctx.system_prompt_snippet)

    def test_rising_trend_triggers_forecast_warn(self):
        """Fill the window with rising RAM readings to trigger forecast alert."""
        analyser = ContextAnalyser(window_size=10)
        for i in range(10):
            r = make_reading(ram_used_pct=50.0 + i * 4, trend="RISING")
            ctx = analyser.analyse(r)
        # After a strong rise, forecast should be critical
        self.assertIn("RAM_FORECAST_CRITICAL", ctx.alert_flags)

    def test_token_budget_decreases_with_pressure(self):
        ctx_nominal  = ContextAnalyser().analyse(make_reading(ram_used_pct=30.0))
        ctx_warn     = ContextAnalyser().analyse(make_reading(ram_used_pct=80.0, thermal_c=71.0))
        ctx_critical = ContextAnalyser().analyse(make_reading(ram_used_pct=93.0))
        self.assertGreater(ctx_nominal.token_budget, ctx_warn.token_budget)
        self.assertGreater(ctx_warn.token_budget,    ctx_critical.token_budget)

    def test_context_to_json_serialisable(self):
        ctx = ContextAnalyser().analyse(make_reading())
        j = ctx.to_json()
        parsed = json.loads(j)
        self.assertIn("recommended_mode", parsed)
        self.assertIn("alert_flags", parsed)
        self.assertIn("reading", parsed)

    def test_linear_forecast_stable(self):
        analyser = ContextAnalyser(window_size=10)
        for _ in range(10):
            analyser.ingest(make_reading(ram_used_pct=50.0))
        fc = analyser._linear_forecast(analyser._ram_window)
        self.assertAlmostEqual(fc, 50.0, delta=2.0)

    def test_linear_forecast_rising(self):
        analyser = ContextAnalyser(window_size=10)
        for i in range(10):
            analyser.ingest(make_reading(ram_used_pct=40.0 + i * 3))
        fc = analyser._linear_forecast(analyser._ram_window, horizon_samples=6)
        self.assertGreater(fc, 60.0)

    def test_disk_critical_flag(self):
        r = make_reading(disk_pct=97.0)
        ctx = self.analyser.analyse(r)
        self.assertIn("DISK_CRITICAL", ctx.alert_flags)

    def test_cpu_saturated_flag(self):
        r = make_reading(cpu_pct=97.0)
        ctx = self.analyser.analyse(r)
        self.assertIn("CPU_SATURATED", ctx.alert_flags)


# ─── Component Tests: SentinelBridge ──────────────────────────────────────────

class TestSentinelBridge(unittest.TestCase):

    def test_bridge_reads_existing_log(self):
        """Bridge must detect and parse lines appended after it starts."""
        results = []
        event   = threading.Event()

        fd, log_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

        def on_ctx(ctx):
            results.append(ctx)
            event.set()

        bridge = SentinelBridge(log_path, on_ctx, poll_interval=0.02)
        bridge.start()

        time.sleep(0.2)  # let bridge settle

        with open(log_path, "a") as f:
            f.write(SAMPLE_LOG_LINE + "\n")
            f.flush()

        triggered = event.wait(timeout=15.0)
        bridge.stop()
        os.unlink(log_path)

        self.assertTrue(triggered, "Bridge did not fire callback within 3s")
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0].reading.ram_used_pct, 67.3)

    def test_bridge_ignores_garbage_lines(self):
        """Bridge must silently skip unparseable lines."""
        results = []
        event   = threading.Event()

        fd, log_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

        def on_ctx(ctx):
            results.append(ctx)
            event.set()

        bridge = SentinelBridge(log_path, on_ctx, poll_interval=0.02)
        bridge.start()
        time.sleep(0.2)

        with open(log_path, "a") as f:
            f.write("garbage line\n")
            f.write("another bad line\n")
            f.write(SAMPLE_LOG_LINE + "\n")
            f.flush()

        event.wait(timeout=15.0)
        bridge.stop()
        os.unlink(log_path)

        self.assertEqual(len(results), 1)

    def test_bridge_multi_line_stream(self):
        """Bridge must process all valid lines in a burst write."""
        results = []
        done    = threading.Event()

        fd, log_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

        def on_ctx(ctx):
            results.append(ctx)
            if len(results) >= 3:
                done.set()

        bridge = SentinelBridge(log_path, on_ctx, poll_interval=0.02)
        bridge.start()
        time.sleep(0.2)

        with open(log_path, "a") as f:
            for _ in range(3):
                f.write(SAMPLE_LOG_LINE + "\n")
            f.flush()

        done.wait(timeout=12.0)
        bridge.stop()
        os.unlink(log_path)

        self.assertEqual(len(results), 3)

    def test_bridge_latest_property(self):
        """bridge.latest must reflect most recent context."""
        fd, log_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

        event = threading.Event()
        bridge = SentinelBridge(log_path, lambda ctx: event.set(), poll_interval=0.02)
        bridge.start()
        time.sleep(0.2)

        with open(log_path, "a") as f:
            f.write(SAMPLE_LOG_LINE + "\n")
            f.flush()

        event.wait(timeout=15.0)
        bridge.stop()
        os.unlink(log_path)

        self.assertIsNotNone(bridge.latest)
        self.assertAlmostEqual(bridge.latest.reading.ram_used_pct, 67.3)


# ─── Integration Tests: SentinelAgent ────────────────────────────────────────

class TestSentinelAgent(unittest.TestCase):

    def test_mock_nominal_mode(self):
        agent = SentinelAgent(mock_scenario="nominal")
        self.assertIsNotNone(agent._get_context())
        self.assertEqual(agent._get_context().recommended_mode, "FULL")
        agent.stop()

    def test_mock_critical_mode(self):
        agent = SentinelAgent(mock_scenario="critical")
        ctx = agent._get_context()
        self.assertEqual(ctx.recommended_mode, "MINIMAL")
        self.assertIn("RAM_CRITICAL", ctx.alert_flags)
        agent.stop()

    def test_echo_response_nominal(self):
        agent = SentinelAgent(mock_scenario="nominal")
        resp  = agent.chat("Hello, what can you do?")
        self.assertIn("FULL", resp)
        agent.stop()

    def test_echo_response_critical(self):
        agent = SentinelAgent(mock_scenario="critical")
        # Critical + rising triggers proactive suspend message
        # (or minimal echo if not both conditions met)
        resp = agent.chat("Run a complex analysis")
        self.assertIsInstance(resp, str)
        self.assertGreater(len(resp), 10)
        agent.stop()

    def test_conversation_history_accumulates(self):
        agent = SentinelAgent(mock_scenario="nominal")
        agent.chat("First message")
        agent.chat("Second message")
        self.assertEqual(len(agent.history), 4)  # 2 user + 2 assistant
        agent.stop()

    def test_reset_clears_history(self):
        agent = SentinelAgent(mock_scenario="nominal")
        agent.chat("Hello")
        self.assertGreater(len(agent.history), 0)
        agent.reset()
        self.assertEqual(len(agent.history), 0)
        agent.stop()

    def test_status_dict_keys(self):
        agent = SentinelAgent(mock_scenario="nominal")
        s = agent.status()
        required_keys = {"mode", "pressure", "ram_pct", "cpu_pct", "thermal_c",
                         "flags", "forecast"}
        self.assertTrue(required_keys.issubset(set(s.keys())))
        agent.stop()

    def test_proactive_suspend_condition(self):
        """Agent should detect imminent collapse correctly."""
        agent = SentinelAgent(mock_scenario="nominal")
        # Build a mock context that triggers suspend
        ctx = _mock_context("critical")
        ctx.reading.trend = "RISING"
        ctx.alert_flags.append("RAM_FORECAST_CRITICAL")
        result = agent._should_proactively_suspend(ctx)
        self.assertTrue(result)
        agent.stop()

    def test_no_suspend_when_stable(self):
        agent = SentinelAgent(mock_scenario="nominal")
        ctx = _mock_context("nominal")
        result = agent._should_proactively_suspend(ctx)
        self.assertFalse(result)
        agent.stop()

    def test_mock_contexts_all_scenarios(self):
        for scenario in ["nominal", "warn", "critical"]:
            ctx = _mock_context(scenario)
            self.assertIsNotNone(ctx)
            self.assertIsInstance(ctx.system_prompt_snippet, str)
            self.assertGreater(len(ctx.system_prompt_snippet), 50)


# ─── System Test: End-to-End ──────────────────────────────────────────────────

class TestEndToEnd(unittest.TestCase):
    """
    Simulates the full pipeline:
      Python writes fake engine output → Bridge parses → Agent responds.
    Does NOT require the C++ binary or API key.
    """

    def test_full_pipeline(self):
        results = []
        done    = threading.Event()

        fd, log_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

        def on_context(ctx):
            results.append(ctx)
            if len(results) >= 5:
                done.set()

        bridge = SentinelBridge(log_path, on_context, poll_interval=0.02)
        bridge.start()
        time.sleep(0.2)

        # Simulate engine writing a burst of nominal readings
        readings = [
            "LSN:1000001 | TS:1700000000000 | RAM_USED:30.0% | RAM_FREE_MB:4500.0 | CPU:10.0% | THERMAL_C:42.0 | BAT:95.0(CHG) | DISK:40.0% | STATE:NOMINAL | TREND:STABLE | DELTA:0.0%",
            "LSN:1000002 | TS:1700000000500 | RAM_USED:32.0% | RAM_FREE_MB:4400.0 | CPU:12.0% | THERMAL_C:43.0 | BAT:95.0(CHG) | DISK:40.0% | STATE:NOMINAL | TREND:STABLE | DELTA:+2.0%",
            "LSN:1000003 | TS:1700000001000 | RAM_USED:55.0% | RAM_FREE_MB:3000.0 | CPU:45.0% | THERMAL_C:58.0 | BAT:80.0(DC) | DISK:40.0% | STATE:NOMINAL | TREND:RISING | DELTA:+23.0%",
            "LSN:1000004 | TS:1700000001500 | RAM_USED:78.0% | RAM_FREE_MB:1500.0 | CPU:70.0% | THERMAL_C:72.0 | BAT:78.0(DC) | DISK:40.0% | STATE:WARN | TREND:RISING | DELTA:+23.0%",
            "LSN:1000005 | TS:1700000002000 | RAM_USED:91.0% | RAM_FREE_MB:600.0 | CPU:88.0% | THERMAL_C:86.0 | BAT:77.0(DC) | DISK:40.0% | STATE:CRITICAL | TREND:RISING | DELTA:+13.0%",
        ]

        with open(log_path, "a") as f:
            for line in readings:
                f.write(line + "\n")
                f.flush()
                time.sleep(0.15)

        done.wait(timeout=12.0)
        bridge.stop()
        os.unlink(log_path)

        self.assertEqual(len(results), 5)

        # First reading should be FULL, last should be MINIMAL
        self.assertEqual(results[0].recommended_mode, "FULL")
        self.assertEqual(results[-1].recommended_mode, "MINIMAL")

        # LSNs should be sequential
        lsns = [r.reading.lsn for r in results]
        self.assertEqual(lsns, sorted(lsns))

        # Critical reading must include CRITICAL flags
        last = results[-1]
        self.assertIn("RAM_CRITICAL", last.alert_flags)
        self.assertIn("THERMAL_CRITICAL", last.alert_flags)

    def test_pipeline_mode_transitions(self):
        """Verify mode transitions match expected pattern as pressure rises."""
        analyser = ContextAnalyser()
        readings = [
            make_reading(ram_used_pct=30.0, thermal_c=45.0, battery_pct=90.0, charging=True),
            make_reading(ram_used_pct=50.0, thermal_c=55.0, battery_pct=90.0, charging=True),
            make_reading(ram_used_pct=78.0, thermal_c=65.0, battery_pct=90.0, charging=True),
            make_reading(ram_used_pct=93.0, thermal_c=88.0, battery_pct=90.0, charging=True),
        ]
        expected_modes = ["FULL", "FULL", "QUANTIZED", "MINIMAL"]
        for r, expected in zip(readings, expected_modes):
            ctx = analyser.analyse(r)
            self.assertEqual(ctx.recommended_mode, expected,
                             f"RAM={r.ram_used_pct}% → expected {expected}, "
                             f"got {ctx.recommended_mode}")


# ─── Property Tests ───────────────────────────────────────────────────────────

class TestProperties(unittest.TestCase):
    """Invariant/property checks that must hold across all inputs."""

    def test_token_budget_always_positive(self):
        a = ContextAnalyser()
        for ram in range(0, 101, 5):
            ctx = a.analyse(make_reading(ram_used_pct=float(ram)))
            self.assertGreater(ctx.token_budget, 0)

    def test_mode_is_always_valid(self):
        valid_modes = {"FULL", "QUANTIZED", "MINIMAL", "SUSPEND"}
        a = ContextAnalyser()
        for ram in range(0, 101, 10):
            for temp in [40.0, 70.0, 85.0, 90.0]:
                ctx = a.analyse(make_reading(ram_used_pct=float(ram), thermal_c=temp))
                self.assertIn(ctx.recommended_mode, valid_modes)

    def test_reasoning_depth_always_valid(self):
        valid_depths = {"DEEP", "STANDARD", "SHALLOW"}
        a = ContextAnalyser()
        for ram in range(0, 101, 10):
            ctx = a.analyse(make_reading(ram_used_pct=float(ram)))
            self.assertIn(ctx.reasoning_depth, valid_depths)

    def test_system_prompt_always_has_hardware_section(self):
        a = ContextAnalyser()
        for ram in [20.0, 50.0, 80.0, 95.0]:
            ctx = a.analyse(make_reading(ram_used_pct=ram))
            self.assertIn("Device Hardware Context", ctx.system_prompt_snippet)

    def test_alert_flags_are_list(self):
        a = ContextAnalyser()
        ctx = a.analyse(make_reading())
        self.assertIsInstance(ctx.alert_flags, list)

    def test_parse_log_line_idempotent(self):
        r1 = parse_log_line(SAMPLE_LOG_LINE)
        r2 = parse_log_line(SAMPLE_LOG_LINE)
        self.assertEqual(asdict(r1), asdict(r2))

    def test_higher_pressure_never_gets_more_tokens(self):
        """A higher-pressure context must never get MORE tokens than a lower one."""
        a1, a2 = ContextAnalyser(), ContextAnalyser()
        ctx_low  = a1.analyse(make_reading(ram_used_pct=30.0))
        ctx_high = a2.analyse(make_reading(ram_used_pct=93.0))
        self.assertGreaterEqual(ctx_low.token_budget, ctx_high.token_budget)


# ─── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║    Project Sentinel — Test Suite         ║")
    print("╚══════════════════════════════════════════╝\n")
    unittest.main(verbosity=2)
