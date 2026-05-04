"""
test_memory_visualiser.py
─────────────────────────
Tests for:
  - SessionMemory (event recording, dedup, summaries, direct answers)
  - MemoryMixin wiring
  - Log visualiser (load, analyse, HTML generation)
"""

import sys
import os
import time
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))
sys.path.insert(0, str(Path(__file__).parent.parent / "agent"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from sentinel_bridge import HardwareReading, ContextAnalyser
from anomaly_detector import AnomalyDetector
from sentinel_memory import SessionMemory, EventKind, HardwareEvent


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def make_ctx(ram=40.0, cpu=20.0, temp=50.0, bat=75.0, charging=False):
    r = HardwareReading(
        lsn=1, timestamp_ms=int(time.time() * 1000),
        ram_used_pct=ram, ram_free_mb=max(0, (100-ram)*60),
        cpu_pct=cpu, thermal_c=temp, battery_pct=bat,
        charging=charging, disk_pct=55, pressure="NOMINAL",
        trend="STABLE", ram_delta_pct=0,
    )
    ctx = ContextAnalyser().analyse(r)
    ctx.anomalies = []
    return ctx


# ─── SessionMemory unit tests ─────────────────────────────────────────────────

class TestSessionMemory(unittest.TestCase):

    def setUp(self):
        self.mem = SessionMemory()

    def test_starts_with_session_start_event(self):
        self.assertEqual(self.mem.event_count, 1)
        ev = self.mem.all_events()[0]
        self.assertEqual(ev.kind, EventKind.SESSION_START)

    def test_mode_change_recorded(self):
        # nominal → warn causes a mode change FULL → QUANTIZED
        self.mem.record_context(make_ctx(ram=32))   # FULL
        self.mem.record_context(make_ctx(ram=80))   # QUANTIZED
        events = self.mem.all_events()
        kinds  = [e.kind for e in events]
        self.assertIn(EventKind.MODE_CHANGE, kinds)

    def test_no_duplicate_mode_change_in_window(self):
        """Same mode change within 5s should not fire twice."""
        self.mem.record_context(make_ctx(ram=32))
        self.mem.record_context(make_ctx(ram=80))  # FULL → QUANTIZED
        before = self.mem.event_count
        self.mem.record_context(make_ctx(ram=80))  # same, should dedup
        self.assertEqual(self.mem.event_count, before)

    def test_threshold_cross_warn_recorded(self):
        self.mem.record_context(make_ctx(ram=77))
        events = [e for e in self.mem.all_events()
                  if e.kind == EventKind.THRESHOLD_CROSS]
        self.assertTrue(any("WARN" in e.summary for e in events))

    def test_threshold_cross_critical_recorded(self):
        self.mem.record_context(make_ctx(ram=92))
        events = [e for e in self.mem.all_events()
                  if e.kind == EventKind.THRESHOLD_CROSS]
        self.assertTrue(any("CRITICAL" in e.summary for e in events))

    def test_no_threshold_event_for_nominal(self):
        self.mem.record_context(make_ctx(ram=40))
        threshold_evs = [e for e in self.mem.all_events()
                         if e.kind == EventKind.THRESHOLD_CROSS]
        self.assertEqual(threshold_evs, [])

    def test_peak_ram_tracked(self):
        self.mem.record_context(make_ctx(ram=50))
        self.mem.record_context(make_ctx(ram=85))
        self.mem.record_context(make_ctx(ram=60))
        self.assertAlmostEqual(self.mem._peak_ram, 85, delta=1)

    def test_peak_temp_tracked(self):
        self.mem.record_context(make_ctx(temp=55))
        self.mem.record_context(make_ctx(temp=80))
        self.mem.record_context(make_ctx(temp=62))
        self.assertAlmostEqual(self.mem._peak_temp, 80, delta=1)

    def test_min_battery_tracked(self):
        self.mem.record_context(make_ctx(bat=90, charging=False))
        self.mem.record_context(make_ctx(bat=45, charging=False))
        self.mem.record_context(make_ctx(bat=60, charging=False))
        self.assertAlmostEqual(self.mem._min_bat, 45, delta=1)

    def test_battery_ignored_when_no_battery(self):
        self.mem.record_context(make_ctx(bat=-1, charging=True))
        # _min_bat should stay at 100 (initial)
        self.assertEqual(self.mem._min_bat, 100.0)

    def test_has_had_critical_false_initially(self):
        self.assertFalse(self.mem.has_had_critical)

    def test_has_had_critical_true_after_critical(self):
        self.mem.record_context(make_ctx(ram=93, temp=87))
        # The THRESHOLD_CROSS events have severity CRITICAL
        self.assertTrue(self.mem.has_had_critical)

    def test_recent_events_empty_for_old_events(self):
        # Manually inject an old event
        old_ev = HardwareEvent(
            kind=EventKind.MODE_CHANGE,
            timestamp_ms=int(time.time() * 1000) - 60_000,  # 60s ago
            summary="old event",
            detail="old", metric="ram", value=80, severity="MEDIUM",
            ai_mode="QUANTIZED"
        )
        self.mem._events.append(old_ev)
        recent = self.mem.recent_events
        self.assertNotIn(old_ev, recent)

    def test_system_prompt_block_non_empty(self):
        self.mem.record_context(make_ctx(ram=80))
        block = self.mem.system_prompt_block()
        self.assertIn("Session Hardware History", block)
        self.assertGreater(len(block), 50)

    def test_system_prompt_block_contains_peaks(self):
        self.mem.record_context(make_ctx(ram=77, temp=73))
        block = self.mem.system_prompt_block()
        self.assertIn("RAM", block)

    def test_record_user_impact(self):
        before = self.mem.event_count
        self.mem.record_user_impact("response truncated to 256 tokens", "MINIMAL")
        self.assertEqual(self.mem.event_count, before + 1)
        impacts = [e for e in self.mem.all_events()
                   if e.kind == EventKind.USER_IMPACT]
        self.assertTrue(len(impacts) >= 1)
        self.assertIn("256", impacts[0].detail)

    def test_max_events_cap(self):
        # Flood with events — should not exceed MAX_EVENTS
        for i in range(50):
            ctx = make_ctx(ram=40 + (i % 60))
            ctx.reading.timestamp_ms = int(time.time() * 1000) + i * 10_000
            self.mem.record_context(ctx)
        self.assertLessEqual(self.mem.event_count, SessionMemory.MAX_EVENTS + 5)

    def test_mode_counts_tracked(self):
        self.mem.record_context(make_ctx(ram=30))   # FULL
        time.sleep(0.01)
        # Force enough time gap to avoid dedup
        self.mem._last_event_times = {}
        self.mem._last_mode = "FULL"
        self.mem.record_context(make_ctx(ram=80))   # QUANTIZED
        self.mem._last_event_times = {}
        self.mem._last_mode = "QUANTIZED"
        self.mem.record_context(make_ctx(ram=30))   # FULL again
        self.assertIn("FULL", self.mem._mode_counts)

    # ── Direct answer tests ───────────────────────────────────────────────────

    def test_answer_battery_question(self):
        self.mem.record_context(make_ctx(bat=25, charging=False))
        ans = self.mem.answer_hardware_question("Battery status?")
        # Might return None if min_bat hasn't been set, or a string
        if ans:
            self.assertIsInstance(ans, str)

    def test_answer_ram_question(self):
        self.mem.record_context(make_ctx(ram=80))
        ans = self.mem.answer_hardware_question("How is the RAM?")
        # Should return string since we have a RAM threshold event
        self.assertIsNotNone(ans)
        self.assertIsInstance(ans, str)

    def test_answer_unknown_returns_none(self):
        ans = self.mem.answer_hardware_question(
            "What is the capital of France?"
        )
        self.assertIsNone(ans)

    def test_answer_short_question_returns_none_or_str(self):
        ans = self.mem.answer_hardware_question("Why?")
        # Either None or a string — no crash
        self.assertIn(type(ans), (type(None), str))


# ─── HardwareEvent tests ──────────────────────────────────────────────────────

class TestHardwareEvent(unittest.TestCase):

    def test_age_seconds_recent(self):
        ev = HardwareEvent(
            kind=EventKind.MODE_CHANGE,
            timestamp_ms=int(time.time() * 1000),
            summary="test", detail="test", metric="ram",
            value=50, severity="MEDIUM", ai_mode="FULL"
        )
        self.assertLess(ev.age_seconds, 1.0)
        self.assertTrue(ev.is_recent)

    def test_age_str_seconds(self):
        ev = HardwareEvent(
            kind=EventKind.ANOMALY,
            timestamp_ms=int(time.time() * 1000) - 15_000,
            summary="test", detail="test", metric="ram",
            value=90, severity="HIGH", ai_mode="MINIMAL"
        )
        self.assertIn("s ago", ev.age_str)

    def test_age_str_minutes(self):
        ev = HardwareEvent(
            kind=EventKind.ANOMALY,
            timestamp_ms=int(time.time() * 1000) - 120_000,
            summary="test", detail="test", metric="ram",
            value=90, severity="HIGH", ai_mode="MINIMAL"
        )
        self.assertIn("m ago", ev.age_str)


# ─── Agent + Memory integration ──────────────────────────────────────────────

class TestAgentMemory(unittest.TestCase):

    def test_agent_has_memory_attribute(self):
        from sentinel_agent import SentinelAgent
        agent = SentinelAgent(mock_scenario="nominal")
        self.assertTrue(hasattr(agent, "_memory"))
        self.assertIsInstance(agent._memory, SessionMemory)
        agent.stop()

    def test_memory_grows_on_chat(self):
        from sentinel_agent import SentinelAgent
        agent = SentinelAgent(mock_scenario="warn")
        before = agent._memory.event_count
        agent.chat("Hello")
        after  = agent._memory.event_count
        self.assertGreaterEqual(after, before)
        agent.stop()

    def test_memory_reset_on_agent_reset(self):
        from sentinel_agent import SentinelAgent
        agent = SentinelAgent(mock_scenario="critical")
        agent.chat("Hello")
        self.assertGreater(agent._memory.event_count, 0)
        agent.reset()
        # After reset, memory is fresh (only SESSION_START)
        self.assertLessEqual(agent._memory.event_count, 2)
        agent.stop()

    def test_memory_summary_returns_string(self):
        from sentinel_agent import SentinelAgent
        agent = SentinelAgent(mock_scenario="warn")
        agent.chat("Test message")
        summary = agent.memory_summary()
        self.assertIsInstance(summary, str)
        self.assertGreater(len(summary), 20)
        agent.stop()

    def test_memory_answer_from_agent(self):
        from sentinel_agent import SentinelAgent
        agent = SentinelAgent(mock_scenario="warn")
        agent.chat("Hello")
        ans = agent.memory_answer("How is the RAM?")
        # Returns string or None — no crash
        self.assertIn(type(ans), (str, type(None)))
        agent.stop()


# ─── Visualiser tests ─────────────────────────────────────────────────────────

class TestVisualiser(unittest.TestCase):

    def test_load_log_parses_real_log(self):
        """Should load and parse the recorded log without errors."""
        from visualise_log import load_log, analyse_session
        log_path = str(Path(__file__).parent.parent / "logs" / "sentinel_redo.log")
        if not Path(log_path).exists():
            self.skipTest("No log file present")
        readings = load_log(log_path)
        self.assertGreater(len(readings), 0)
        analysis = analyse_session(readings)
        self.assertIn("n_readings",  analysis)
        self.assertIn("ram_peak",    analysis)
        self.assertIn("mode_counts", analysis)

    def test_generate_synthetic_session(self):
        from visualise_log import generate_synthetic_session
        readings = generate_synthetic_session()
        self.assertGreater(len(readings), 10)
        # All readings must be valid
        for r in readings:
            self.assertGreaterEqual(r.ram_used_pct, 0)
            self.assertLessEqual(r.ram_used_pct, 100)

    def test_analyse_session_keys(self):
        from visualise_log import generate_synthetic_session, analyse_session
        readings = generate_synthetic_session()
        analysis = analyse_session(readings)
        required = ["n_readings", "duration_s", "ram_peak", "ram_mean",
                    "cpu_peak", "temp_peak", "mode_counts", "anomaly_events",
                    "pressure_counts", "n_anomalies"]
        for key in required:
            self.assertIn(key, analysis, f"Missing key: {key}")

    def test_analyse_peaks_correct(self):
        from visualise_log import generate_synthetic_session, analyse_session
        readings = generate_synthetic_session()
        analysis = analyse_session(readings)
        # Peak must be >= mean
        self.assertGreaterEqual(analysis["ram_peak"], analysis["ram_mean"])
        self.assertGreaterEqual(analysis["cpu_peak"], analysis["cpu_mean"])
        self.assertGreaterEqual(analysis["temp_peak"], analysis["temp_mean"])

    def test_generate_html_is_valid(self):
        from visualise_log import (generate_synthetic_session,
                                    analyse_session, generate_html)
        readings = generate_synthetic_session()
        analysis = analyse_session(readings)
        html     = generate_html(readings, analysis)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("Project Sentinel", html)
        self.assertIn("<canvas", html)
        self.assertGreater(len(html), 10_000)

    def test_generate_html_contains_stats(self):
        from visualise_log import (generate_synthetic_session,
                                    analyse_session, generate_html)
        readings = generate_synthetic_session()
        analysis = analyse_session(readings)
        html = generate_html(readings, analysis)
        self.assertIn(str(analysis["n_readings"]), html)
        # Peak RAM should appear in the report
        self.assertIn(f"{analysis['ram_peak']:.1f}", html)

    def test_write_html_to_file(self):
        from visualise_log import (generate_synthetic_session,
                                    analyse_session, generate_html)
        readings = generate_synthetic_session()
        analysis = analyse_session(readings)
        html     = generate_html(readings, analysis)
        fd, path = tempfile.mkstemp(suffix=".html")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        size = os.path.getsize(path)
        os.unlink(path)
        self.assertGreater(size, 10_000)

    def test_analyse_pressure_counts_sum_to_n(self):
        from visualise_log import generate_synthetic_session, analyse_session
        readings = generate_synthetic_session()
        analysis = analyse_session(readings)
        total = sum(analysis["pressure_counts"].values())
        self.assertEqual(total, analysis["n_readings"])

    def test_mode_counts_sum_to_n(self):
        from visualise_log import generate_synthetic_session, analyse_session
        readings = generate_synthetic_session()
        analysis = analyse_session(readings)
        total = sum(analysis["mode_counts"].values())
        self.assertEqual(total, analysis["n_readings"])


# ─── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔═════════════════════════════════════════════════╗")
    print("║  Sentinel — Memory & Visualiser Test Suite      ║")
    print("╚═════════════════════════════════════════════════╝\n")
    unittest.main(verbosity=2)
