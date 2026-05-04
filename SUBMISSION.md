# Project Sentinel
## Samsung PRISM (AX) Hackathon — Problem Statement #3

---

### The Problem We Solved

On-device AI agents are **hardware-blind**. They issue inference requests at whatever intensity the model demands, without knowing whether the device can sustain that intensity at this moment. The result is three failure modes every Galaxy user has experienced:

- **Thermal collapse** — the SoC throttles mid-inference, producing stuttered or incomplete responses
- **Memory kill** — the OOM killer terminates the AI process with no warning to the user  
- **Battery drain cliff** — sustained inference pulls peak current, accelerating shutdown nonlinearly

Existing solutions react *after* the OS acts. We react *before*.

---

### What We Built

**Sentinel** is a hardware-to-AI bridge: a lightweight telemetry layer that gives the AI agent real-time awareness of the device's physiological state, injected into the system prompt on every conversation turn.

```
C++ / Python engine          redo log            Python bridge              AI agent
(polls every 500ms)  ──────────────────▶  (tail + analyse + forecast)  ──▶  (Claude / Ollama)
RAM · CPU · thermal          atomic append        Z-score anomaly detection    adaptive mode
battery · disk · NPU         LSN-stamped          linear regression forecast   token budget
```

---

### Five Novel Contributions

**1. Multi-signal fusion** — RAM, CPU, thermal, battery, and disk are combined into a single composite `pressure_state` rather than evaluated in isolation. A 78% RAM reading means nothing without knowing the thermal headroom and battery state.

**2. Horizon forecasting** — linear regression over a 10-sample rolling window predicts resource state 3 seconds ahead. The AI throttles *before* the OS does. We call this the "proactive throttle" — no prior on-device AI system does this.

**3. Statistical anomaly detection** — eight failure-signature patterns detected via Z-score, variance ratio, and rate-of-change analysis: `RAM_SPIKE`, `RAM_CREEP`, `THERMAL_RUNAWAY`, `BATTERY_CLIFF`, `CPU_THRASH`, `COMPOUND_PRESSURE`, `MEMORY_OSCILLATION`, `RECOVERY_DETECTED`. Threshold-crossing alone cannot distinguish a stable 80% from a rapidly-rising 80%.

**4. Per-turn context injection** — hardware state is re-sampled and re-injected into the system prompt *before every API call*, not just at session start. A conversation that begins under nominal conditions automatically tightens as the device heats up.

**5. Session hardware memory** — the agent maintains a structured log of hardware events *within the conversation*, so it can answer questions like "when did my device start getting hot?" and explain why it gave a shorter answer three turns ago.

---

### Measured Results

| Metric | Value | Method |
|---|---|---|
| Bridge CPU overhead | **0.017%** at 500ms poll | `python scripts/benchmark.py` |
| Parse throughput | **12,000+ lines/sec** | 10,000-iteration microbenchmark |
| Write→callback latency | **< 10ms** | End-to-end timing through file IPC |
| Bridge heap footprint | **< 4 KB** | tracemalloc over 1,000 cycles |
| Test coverage | **94 tests, 0 failures** | Unit + integration + stress + property |

---

### Running the Full Demo

```bash
# Install (Windows / macOS / Linux)
pip install anthropic psutil pytest

# One-command demo (no API key, no compiler, ~60 seconds)
python scripts/demo.py --quick

# Full test suite
python -m pytest tests/ -v

# Live web dashboard (open in browser)
dashboard/sentinel_web.html

# AI agent with simulated hardware pressure
python agent/sentinel_agent.py --mock critical
```

---

### Samsung Galaxy Integration Path

| Component | Samsung API / Path |
|---|---|
| Thermal zones | Exynos `/sys/class/thermal/thermal_zone{3,7,9,15}` |
| Battery health | `/sys/class/power_supply/battery/cycle_count` |
| NPU utilisation | `/sys/devices/platform/npu/utilization` |
| Knox policy | `/sys/class/sec/sec_afc/drk` (warranty bit) |
| Adaptive mode push | Knox MDM → `configs/engine.conf` |
| On-device model | Sentinel MINIMAL mode routes to on-device Llama/Gemma |

---

### File Structure

```
sentinel/
├── engine/sentinel_engine.cpp      C++ hardware poller (cross-platform)
├── scripts/sentinel_engine_py.py   Python psutil engine (no compiler needed)
├── bridge/sentinel_bridge.py       Log watcher, ContextAnalyser, forecaster
├── bridge/anomaly_detector.py      8-pattern statistical anomaly detector
├── bridge/samsung_metrics.py       Samsung-specific sysfs + Knox reader
├── agent/sentinel_agent.py         Adaptive AI agent (Claude API)
├── agent/sentinel_memory.py        Session hardware event memory
├── dashboard/sentinel_web.html     Live web dashboard (SSE, auto-demo)
├── scripts/sentinel_api.py         REST API server (/status /stream /anomalies)
├── scripts/demo.py                 One-command automated demo
├── scripts/benchmark.py            Overhead benchmark with real numbers
├── scripts/replay.py               Session replay (5 built-in scenarios)
├── tests/test_sentinel.py          45 unit + integration tests
├── tests/test_stress.py            11 stress + concurrency tests
└── tests/test_anomaly_engine.py    38 anomaly + engine tests
```

---

*Apache 2.0 License · Python 3.10+ · Windows / macOS / Linux / Android (Termux)*
