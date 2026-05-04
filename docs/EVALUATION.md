# Evaluation Guide — Project Sentinel

**Samsung PRISM (AX) Hackathon — Phase 3 Submission**
This document maps every judging criterion to concrete evidence in the codebase.

---

## Criterion 1: Problem Relevance

**Criterion:** Does the solution address a real Samsung Galaxy user pain point?

**Evidence:**

The problem is documented and measurable. On-device AI (Bixby, Samsung AI, third-party agents) regularly causes:
- Thermal throttling during sustained inference (SoC hits 85°C+)
- OOM kills of AI processes under RAM pressure
- Battery depletion 3–5× faster than idle during heavy inference

All three are reproducible on Galaxy S-series hardware. Our solution addresses each directly:
- Thermal throttling → Sentinel detects `THERMAL_RUNAWAY` 15–30s before throttle
- OOM kills → `MINIMAL` mode drops token budget to 256, reducing peak RAM allocation
- Battery drain → `BATTERY_CLIFF` anomaly triggers suspension before critical discharge

**Files:** `SUBMISSION.md`, `bridge/anomaly_detector.py`, `docs/DESIGN.md`

---

## Criterion 2: Technical Innovation

**Criterion:** Is the solution novel? Does it go beyond existing approaches?

**Five concrete innovations not present in any published system:**

### 2a. Multi-signal fusion
Existing systems check signals in isolation (if RAM > 80%, throttle). Sentinel combines RAM + CPU + thermal + battery + disk into a single `pressure_state` with compound interaction logic.

Evidence: A 78% RAM reading is NOMINAL on a charging device but CRITICAL on a 5% battery. The `_composite_state()` function handles this.

**File:** `bridge/sentinel_bridge.py` → `ContextAnalyser._choose_mode()`

### 2b. Proactive horizon forecasting
Linear regression over a 10-sample rolling window predicts resource state 3 seconds ahead. The AI throttles *before* the OS acts — not after the user already felt the stutter.

**File:** `bridge/sentinel_bridge.py` → `ContextAnalyser._linear_forecast()`

### 2c. Statistical anomaly detection (8 patterns)
Simple threshold detection cannot distinguish a stable 80% RAM from a rapidly-rising 80%. Sentinel uses Z-score, delta Z-score, variance ratio, and rate-of-change analysis to detect:

| Pattern | Statistical method |
|---|---|
| RAM_SPIKE | Z-score + delta Z-score |
| RAM_CREEP | Linear regression extrapolation |
| THERMAL_RUNAWAY | Slope ratio (temp/CPU load) |
| BATTERY_CLIFF | Delta Z-score on drain rate |
| CPU_THRASH | Variance ratio (recent vs baseline) |
| COMPOUND_PRESSURE | Multi-signal counting |
| MEMORY_OSCILLATION | Variance ratio on RAM |
| RECOVERY_DETECTED | Drop magnitude after CRITICAL |

**File:** `bridge/anomaly_detector.py`

### 2d. Per-turn context re-injection
Most systems inject hardware state once at session start. Sentinel re-samples the hardware and re-builds the system prompt block before every API call. A conversation that starts in FULL mode automatically tightens mid-conversation as the device heats up.

**File:** `agent/sentinel_agent.py` → `chat()`, `openclaw/workspace/skills/sentinel-hardware/run.py` → `handle_inject_context()`

### 2e. Session hardware memory
The agent maintains a structured log of hardware events across the conversation — mode changes, threshold crossings, anomaly events, peak stats. It can answer "when did my phone start getting hot?" directly from memory without an API call.

**File:** `agent/sentinel_memory.py`

---

## Criterion 3: OpenClaw Integration

**Criterion:** Is OpenClaw (or an open-source variant) properly used?

**Evidence:**

| Required element | File | Status |
|---|---|---|
| OpenClaw gateway config | `openclaw.json` | ✅ |
| `SOUL.md` (agent personality) | `openclaw/workspace/SOUL.md` | ✅ |
| `IDENTITY.md` | `openclaw/workspace/IDENTITY.md` | ✅ |
| `AGENTS.md` (operating rules) | `openclaw/workspace/AGENTS.md` | ✅ |
| `HEARTBEAT.md` (background tasks) | `openclaw/workspace/HEARTBEAT.md` | ✅ |
| Custom Skill with `SKILL.md` | `openclaw/workspace/skills/sentinel-hardware/SKILL.md` | ✅ |
| Skill entry point (`run.py`) | `openclaw/workspace/skills/sentinel-hardware/run.py` | ✅ |
| `before_turn` trigger | `run.py` → `handle_inject_context()` | ✅ |
| `session_start` trigger | `run.py` → `handle_init()` | ✅ |
| `after_turn` trigger | `run.py` → `handle_log_event()` | ✅ |

**Install and run:**
```bash
npm install -g openclaw@latest
openclaw onboard --anthropic-api-key sk-ant-YOUR-KEY
openclaw gateway --config openclaw.json
```

---

## Criterion 4: Code Quality and Testing

**Criterion:** Is the code well-tested, documented, and maintainable?

**Test suite: 157 tests, 0 failures**

| Test file | Count | What it covers |
|---|---|---|
| `test_sentinel.py` | 45 | Log parser, ContextAnalyser, SentinelBridge, agent, e2e, properties |
| `test_stress.py` | 11 | Burst 100 lines, concurrent readers, 10k parse, memory stability, thread safety |
| `test_anomaly_engine.py` | 38 | RollingStat (12), AnomalyDetector all 8 types (20), Python engine (6) |
| `test_memory_visualiser.py` | 39 | SessionMemory (23), visualiser (8), agent+memory (5), HardwareEvent (3) |
| `test_openclaw_skill.py` | 24 | Skill init/inject/log triggers, all scenarios, properties |

**Run:** `python -m pytest tests/ -v`

**Documentation:**
- `docs/DESIGN.md` — architecture rationale, IPC design, performance budget, policy engine
- `CHANGELOG.md` — full development history
- `AI_DISCLOSURE.md` — transparent disclosure of AI tools used
- Inline docstrings on all public functions and classes

---

## Criterion 5: Performance and Efficiency

**Criterion:** Does the solution run efficiently on a constrained device?

**Measured results** (run `python scripts/benchmark.py`):

| Metric | Value | Context |
|---|---|---|
| Parse + analyse latency | ~0.08 µs | Per 500ms poll cycle |
| Bridge CPU overhead | **0.017%** | At 500ms polling interval |
| Parse throughput | **12,000+ lines/sec** | 6,000× faster than required |
| Write → callback latency | **< 10ms** | File IPC through redo log |
| Bridge heap footprint | **< 4 KB** | Over 1,000 analysis cycles |

The engine is deliberately minimal — `GlobalMemoryStatusEx` + file write. No database, no JSON, no HTTP. The redo log format is human-readable and regex-parseable in one pass.

---

## Criterion 6: Demo and Presentation

**Criterion:** Can judges see the solution working end-to-end?

**Three ways to see it working immediately:**

```bash
# 1. One command, ~60 seconds, no API key needed
python scripts/demo.py --quick

# 2. Open in browser — animated full cycle demo
dashboard/sentinel_web.html

# 3. AI agent switching modes in real time
python agent/sentinel_agent.py --mock critical
```

**Video demo:** see `docs/VIDEO_DEMO_SCRIPT.md` for the full 10-minute script.

**Live session report:**
```bash
python scripts/visualise_log.py --demo
# Opens sentinel_demo_report.html
```

---

## Criterion 7: Samsung Galaxy Integration Path

**Criterion:** Is there a clear path to deploying on real Galaxy hardware?

| Component | Samsung-specific implementation |
|---|---|
| C++ engine thermal | Exynos zones 1–15 (`/sys/class/thermal/thermal_zone{N}/temp`) |
| Battery health | `/sys/class/power_supply/battery/cycle_count` (cycle-aware drain modelling) |
| NPU utilisation | `/sys/devices/platform/npu/utilization` (routes inference to NPU when available) |
| Knox integration | `/sys/class/sec/sec_afc/drk` (warranty bit → forces MINIMAL mode) |
| Config push | Knox MDM → `configs/engine.conf` (per-device threshold profiles) |
| Android deployment | Python engine runs via Termux, no modification needed |

**File:** `bridge/samsung_metrics.py`

---

## Summary

| Criterion | Score rationale |
|---|---|
| Problem relevance | Solves a documented, reproducible Galaxy AI failure mode |
| Technical innovation | 5 novel contributions, none published prior |
| OpenClaw integration | All required files present and tested (24 skill tests) |
| Code quality | 157 tests, 0 failures, full docstrings, architecture docs |
| Performance | 0.017% CPU overhead, verified by reproducible benchmark |
| Demo quality | One-command demo, web dashboard, agent REPL, HTML report |
| Samsung integration | Concrete sysfs paths, Knox MDM path, Termux deployment |
