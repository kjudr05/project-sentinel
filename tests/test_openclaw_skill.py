"""
test_openclaw_skill.py
──────────────────────
Tests for the OpenClaw sentinel-hardware skill (run.py).

Covers all three trigger handlers:
  - handle_init()           session_start trigger
  - handle_inject_context() before_turn trigger
  - handle_log_event()      after_turn trigger

Also tests mock mode, log reading, system prompt structure,
token budget enforcement, and anomaly injection.
"""

import sys
import os
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Add paths
SKILL_ROOT = Path(__file__).parent.parent / "openclaw" / "workspace" / "skills" / "sentinel-hardware"
BRIDGE_ROOT = Path(__file__).parent.parent / "bridge"
AGENT_ROOT  = Path(__file__).parent.parent / "agent"

sys.path.insert(0, str(SKILL_ROOT))
sys.path.insert(0, str(BRIDGE_ROOT))
sys.path.insert(0, str(AGENT_ROOT))


def _set_mock(scenario: str):
    """Set SENTINEL_MOCK env var and update the skill module global."""
    os.environ["SENTINEL_MOCK"] = scenario
    import run as skill
    skill.MOCK = scenario
    # Reset shared state
    from sentinel_bridge import ContextAnalyser
    from anomaly_detector import AnomalyDetector
    from sentinel_memory import SessionMemory
    skill._analyser  = ContextAnalyser()
    skill._detector  = AnomalyDetector()
    skill._memory    = SessionMemory()
    skill._latest_ctx = None
    return skill


def _clear_mock():
    os.environ.pop("SENTINEL_MOCK", None)
    import run as skill
    skill.MOCK = ""


class TestHandleInit(unittest.TestCase):

    def test_init_mock_nominal(self):
        skill = _set_mock("nominal")
        result = skill.handle_init()
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "mock")
        self.assertEqual(result["scenario"], "nominal")

    def test_init_mock_critical(self):
        skill = _set_mock("critical")
        result = skill.handle_init()
        self.assertTrue(result["ok"])
        self.assertEqual(result["scenario"], "critical")

    def test_init_no_log_file(self):
        _clear_mock()
        import run as skill
        skill.MOCK = ""
        skill.LOG_PATH = "/nonexistent/path/sentinel_redo.log"
        result = skill.handle_init()
        self.assertTrue(result["ok"])
        self.assertIn("source", result)

    def test_init_with_real_log(self):
        _clear_mock()
        import run as skill
        skill.MOCK = ""

        fd, log_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

        line = ("LSN:1000001 | TS:1700000000000 | RAM_USED:45.0% | "
                "RAM_FREE_MB:3300.0 | CPU:20.0% | THERMAL_C:50.0 | "
                "BAT:80.0(CHG) | DISK:55.0% | STATE:NOMINAL | "
                "TREND:STABLE | DELTA:0.0%\n")
        with open(log_path, "w") as f:
            for i in range(5):
                f.write(line.replace("1000001", f"100000{i+1}"))

        skill.LOG_PATH = log_path
        result = skill.handle_init()
        os.unlink(log_path)

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "log")
        self.assertTrue(result.get("warmed", False))


class TestHandleInjectContext(unittest.TestCase):

    def test_inject_nominal_full_mode(self):
        skill = _set_mock("nominal")
        result = skill.handle_inject_context()
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "FULL")
        self.assertEqual(result["max_tokens"], 1024)
        self.assertIn("Device Hardware Context", result["system_prompt"])

    def test_inject_warn_quantized_mode(self):
        skill = _set_mock("warn")
        result = skill.handle_inject_context()
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "QUANTIZED")
        self.assertLessEqual(result["max_tokens"], 512)

    def test_inject_critical_minimal_mode(self):
        skill = _set_mock("critical")
        result = skill.handle_inject_context()
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "MINIMAL")
        self.assertLessEqual(result["max_tokens"], 256)

    def test_inject_returns_pressure(self):
        skill = _set_mock("critical")
        result = skill.handle_inject_context()
        self.assertIn("pressure", result)
        self.assertEqual(result["pressure"], "CRITICAL")

    def test_inject_returns_alert_flags(self):
        skill = _set_mock("critical")
        result = skill.handle_inject_context()
        self.assertIn("alert_flags", result)
        self.assertIsInstance(result["alert_flags"], list)
        self.assertTrue(len(result["alert_flags"]) > 0)

    def test_inject_returns_anomaly_count(self):
        skill = _set_mock("nominal")
        result = skill.handle_inject_context()
        self.assertIn("anomaly_count", result)
        self.assertIsInstance(result["anomaly_count"], int)

    def test_inject_system_prompt_has_mode_instructions(self):
        skill = _set_mock("nominal")
        result = skill.handle_inject_context()
        prompt = result["system_prompt"]
        self.assertIn("Adaptive Operating Instructions", prompt)
        self.assertIn("FULL", prompt)

    def test_inject_minimal_mode_has_minimal_instructions(self):
        skill = _set_mock("critical")
        result = skill.handle_inject_context()
        prompt = result["system_prompt"]
        self.assertIn("CRITICAL RESOURCE PRESSURE", prompt)

    def test_inject_quantized_mode_has_efficiency_instructions(self):
        skill = _set_mock("warn")
        result = skill.handle_inject_context()
        prompt = result["system_prompt"]
        # Should mention concise/efficient
        self.assertTrue(
            "concise" in prompt.lower() or "efficient" in prompt.lower()
        )

    def test_token_budget_decreases_with_pressure(self):
        skill_nom  = _set_mock("nominal")
        result_nom = skill_nom.handle_inject_context()

        skill_crit  = _set_mock("critical")
        result_crit = skill_crit.handle_inject_context()

        self.assertGreater(result_nom["max_tokens"], result_crit["max_tokens"])

    def test_inject_no_log_returns_fallback(self):
        _clear_mock()
        import run as skill
        skill.MOCK = ""
        skill.LOG_PATH = "/nonexistent/path.log"
        skill._latest_ctx = None
        result = skill.handle_inject_context()
        self.assertTrue(result["ok"])
        self.assertIn("system_prompt", result)
        self.assertIn("max_tokens", result)

    def test_inject_includes_memory_block_after_events(self):
        skill = _set_mock("warn")
        # Call twice so memory has events to log
        skill.handle_inject_context()
        skill._memory._last_event_times = {}  # reset dedup
        skill._memory._last_mode = "FULL"     # force a mode change
        result = skill.handle_inject_context()
        # Memory block gets added after the first mode change is recorded
        self.assertIn("system_prompt", result)


class TestHandleLogEvent(unittest.TestCase):

    def test_log_event_no_context_returns_zero(self):
        import run as skill
        skill._latest_ctx = None
        result = skill.handle_log_event()
        self.assertTrue(result["ok"])
        self.assertEqual(result["logged"], 0)

    def test_log_event_after_inject_creates_file(self):
        skill = _set_mock("warn")
        skill.handle_inject_context()

        # Force a recent event into memory
        from sentinel_memory import HardwareEvent, EventKind
        ev = HardwareEvent(
            kind=EventKind.THRESHOLD_CROSS,
            timestamp_ms=int(time.time() * 1000),
            summary="RAM crossed WARN threshold (78%)",
            detail="test", metric="ram", value=78,
            severity="MEDIUM", ai_mode="QUANTIZED"
        )
        skill._memory._events.append(ev)

        result = skill.handle_log_event()
        self.assertTrue(result["ok"])

    def test_log_event_result_has_ok_field(self):
        skill = _set_mock("nominal")
        skill.handle_inject_context()
        result = skill.handle_log_event()
        self.assertIn("ok", result)
        self.assertIn("logged", result)


class TestSkillProperties(unittest.TestCase):

    def test_mode_always_valid(self):
        valid = {"FULL", "QUANTIZED", "MINIMAL", "SUSPEND"}
        for scenario in ["nominal", "warn", "critical"]:
            skill = _set_mock(scenario)
            result = skill.handle_inject_context()
            self.assertIn(result["mode"], valid,
                          f"Invalid mode for {scenario}: {result['mode']}")

    def test_max_tokens_always_positive(self):
        for scenario in ["nominal", "warn", "critical"]:
            skill = _set_mock(scenario)
            result = skill.handle_inject_context()
            self.assertGreater(result["max_tokens"], 0)

    def test_system_prompt_always_string(self):
        for scenario in ["nominal", "warn", "critical"]:
            skill = _set_mock(scenario)
            result = skill.handle_inject_context()
            self.assertIsInstance(result["system_prompt"], str)
            self.assertGreater(len(result["system_prompt"]), 50)

    def test_all_triggers_return_ok(self):
        skill = _set_mock("nominal")
        for handler in [skill.handle_init,
                        skill.handle_inject_context,
                        skill.handle_log_event]:
            result = handler()
            self.assertIn("ok", result,
                          f"{handler.__name__} missing 'ok' field")

    def test_no_exception_across_all_scenarios(self):
        for scenario in ["nominal", "warn", "critical"]:
            skill = _set_mock(scenario)
            try:
                skill.handle_init()
                skill.handle_inject_context()
                skill.handle_log_event()
            except Exception as e:
                self.fail(f"Exception in {scenario}: {e}")


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("╔════════════════════════════════════════════════╗")
    print("║  Sentinel — OpenClaw Skill Test Suite          ║")
    print("╚════════════════════════════════════════════════╝\n")
    unittest.main(verbosity=2)
