# 🛡️ Project Sentinel

### Hardware-Aware Adaptive AI for Samsung Galaxy Devices

**Samsung PRISM (AX) Hackathon — Problem Statement #3 / Theme 2: Daily Utility (Smartphones)**

> *“The best AI on a device isn't the most powerful — it's the one that knows when to be quiet.”*

---

## The Problem We Solved

On-device AI agents are **hardware-blind**. They execute inference and reasoning tasks without awareness of the device’s physical state — RAM pressure, thermal throttling, battery health, or storage constraints.

This creates three major real-world failure modes:

* **Thermal collapse** — the SoC throttles mid-inference, producing stuttered or incomplete responses
* **Memory kill** — the OOM killer terminates the AI process with no warning
* **Battery drain cliff** — sustained inference accelerates shutdown nonlinearly

Existing systems react *after* the OS intervenes.
**Sentinel reacts before failure happens.**

---

## Solution Overview

**Sentinel** is a hardware-to-AI bridge that gives AI agents a real-time nervous system.

A lightweight telemetry engine continuously monitors hardware metrics every 500ms and writes them to an atomic redo log. A Python bridge tails this log, performs forecasting + anomaly detection, and injects structured hardware context directly into the AI system prompt before every conversation turn.

```text
C++ / Python Engine          redo log             Python Bridge               AI Agent
(polls every 500ms)  ──────────────────▶  (tail + analyse + forecast)  ──▶  (Claude / Ollama / OpenClaw)
RAM · CPU · thermal          atomic append        anomaly detection            adaptive mode
battery · disk · NPU         LSN-stamped          linear regression            token budget
```

---

## Core Innovation

Most AI systems treat hardware like a static resource pool.

**Sentinel treats the device as a living metabolism** — dynamic, constrained, and constantly changing.

```text
┌─────────────────┐     redo log      ┌──────────────────┐     context dict     ┌────────────────────┐
│  Iron (C++)     │ ────────────────▶ │ Handshake (Py)   │ ───────────────────▶ │ Soul (AI Agent)    │
│ Hardware poll   │    atomic append  │ Tail + analyse   │    system prompt     │ Claude / Ollama    │
│ 500ms interval  │                   │ Forecast engine  │    injection         │ Adaptive mode      │
└─────────────────┘                   └──────────────────┘                      └────────────────────┘
```

---

## Five Novel Contributions

### 1. Multi-signal fusion

RAM, CPU, thermal, battery, disk, and optional NPU metrics are fused into one composite `pressure_state`.

### 2. Horizon forecasting

Linear regression over rolling windows predicts critical resource pressure ahead of time, enabling proactive throttling.

### 3. Statistical anomaly detection

Eight hardware failure signatures are detected:

* `RAM_SPIKE`
* `RAM_CREEP`
* `THERMAL_RUNAWAY`
* `BATTERY_CLIFF`
* `CPU_THRASH`
* `COMPOUND_PRESSURE`
* `MEMORY_OSCILLATION`
* `RECOVERY_DETECTED`

### 4. Per-turn context injection

Hardware state is refreshed before every AI call, not just at startup.

### 5. Session hardware memory

The AI remembers hardware stress events across a conversation session.

---

## Adaptive Behaviour Model

| Device State      |      Mode | Reasoning Depth | Max Tokens | Behaviour              |
| ----------------- | --------: | --------------: | ---------: | ---------------------- |
| Healthy           |      FULL |            DEEP |       1024 | Full-quality reasoning |
| Moderate pressure | QUANTIZED |        STANDARD |        512 | Efficient response     |
| High pressure     | QUANTIZED |        STANDARD |        400 | Proactive throttle     |
| Critical          |   MINIMAL |         SHALLOW |        256 | Survival mode          |
| Critical + Rising |   SUSPEND |               — |          — | Voluntary pause        |

---

## Measured Results

| Metric                   |                     Value | Method                        |
| ------------------------ | ------------------------: | ----------------------------- |
| Bridge CPU overhead      |  **0.017%** at 500ms poll | `python scripts/benchmark.py` |
| Parse throughput         |     **12,000+ lines/sec** | 10K-line microbenchmark       |
| Write → callback latency |                 **<10ms** | File IPC timing               |
| Heap footprint           |                 **<4 KB** | tracemalloc                   |
| Test coverage            | **94+ tests, 0 failures** | Unit + stress + integration   |

---

## Redo Log Format

```text
LSN:1000042 | TS:1700000123456 | RAM_USED:67.3% | RAM_FREE_MB:2048.5 | CPU:34.1% | THERMAL_C:62.0 | BAT:55.0(DC) | DISK:71.2% | STATE:NOMINAL | TREND:RISING | DELTA:+0.8%
```

### Fields:

* **LSN** — Log Sequence Number
* **TS** — Timestamp
* **STATE** — NOMINAL / WARN / CRITICAL
* **TREND** — RISING / STABLE / FALLING
* **DELTA** — Resource velocity

---

## Samsung Galaxy Integration Path

| Component          | Samsung API / Path                            |
| ------------------ | --------------------------------------------- |
| Thermal zones      | `/sys/class/thermal/thermal_zone{3,7,9,15}`   |
| Battery health     | `/sys/class/power_supply/battery/cycle_count` |
| NPU utilisation    | `/sys/devices/platform/npu/utilization`       |
| Knox policy        | Knox / sec sysfs                              |
| Adaptive mode push | Knox MDM → `configs/engine.conf`              |
| On-device fallback | Llama / Gemma in MINIMAL mode                 |

---

## Repository Structure

```text
sentinel/
├── openclaw.json
├── openclaw/workspace/
│   ├── SOUL.md
│   ├── IDENTITY.md
│   ├── AGENTS.md
│   ├── HEARTBEAT.md
│   └── skills/sentinel-hardware/
│       ├── SKILL.md
│       └── run.py
├── engine/
│   └── sentinel_engine.cpp
├── scripts/
│   ├── sentinel_engine_py.py
│   ├── sentinel_api.py
│   ├── demo.py
│   ├── benchmark.py
│   ├── replay.py
│   ├── visualise_log.py
│   └── sentinel_monitor.py
├── bridge/
│   ├── sentinel_bridge.py
│   ├── anomaly_detector.py
│   └── samsung_metrics.py
├── agent/
│   ├── sentinel_agent.py
│   └── sentinel_memory.py
├── dashboard/
│   ├── sentinel_web.html
│   └── sentinel_dashboard.py
├── tests/
├── docs/
│   └── DESIGN.md
├── AI_DISCLOSURE.md
└── README.md
```

---

## Setup Instructions

### Prerequisites

| Requirement | Version |
| ----------- | ------- |
| Python      | 3.10+   |
| Node.js     | 22+     |
| OpenClaw    | Latest  |
| Git         | Any     |

### Clone Repo

```bash
git clone https://github.com/kjudr05/project-sentinel.git
cd project-sentinel
```

### Install Dependencies

```bash
pip install anthropic psutil pytest
```

### Configure OpenClaw

```bash
openclaw onboard --anthropic-api-key "sk-ant-YOUR-KEY"
```

---

## Usage Modes

### Quick Demo (No API Key)

```bash
python scripts/demo.py --quick
```

### AI Agent Mock Modes

```bash
python agent/sentinel_agent.py --mock nominal
python agent/sentinel_agent.py --mock warn
python agent/sentinel_agent.py --mock critical
```

### Live Dashboard

```bash
python scripts/sentinel_api.py --mock warn
```

Then open:

```text
dashboard/sentinel_web.html
```

### Full OpenClaw Mode

```bash
python scripts/sentinel_engine_py.py
openclaw gateway --config openclaw.json
openclaw dashboard
```

---

## Testing

```bash
python -m pytest tests/ -v
```

### Coverage Includes:

* Unit tests
* Integration tests
* Stress tests
* Property tests
* Forecasting validation
* Anomaly detection validation

---

## Platform Support

### Linux / Samsung Galaxy Linux userspace

* `/proc/meminfo`
* `/proc/stat`
* `hwmon`
* `thermal_zone`

### Android (Termux / NDK)

* Native sysfs paths
* Battery + thermal direct polling

### Windows (Galaxy Book)

* `GlobalMemoryStatusEx`
* `GetSystemTimes`
* `GetSystemPowerStatus`

---

## Samsung PRISM Relevance

### What competitors miss:

* Post-failure throttling
* Single-signal checks
* No prediction
* No AI adaptation

### What Sentinel adds:

* Sub-second telemetry
* Multi-signal state fusion
* Predictive throttling
* Adaptive AI system prompts
* Graceful degradation

---

## Future Roadmap

* Android NDK wrapper
* Knox-native enterprise deployment
* Samsung Galaxy Store optimization
* NPU-aware scheduling
* Offline local LLM fallback

---

## License

**Apache 2.0**

---

## Team

**Kavya Jain (kjudr05)**
Samsung PRISM AX Hackathon 2026
