---
name: sentinel-hardware
version: 1.0.0
description: >
  Real-time hardware telemetry skill for Samsung Galaxy devices.
  Reads the Sentinel redo log, analyses RAM/CPU/thermal/battery state,
  forecasts resource pressure, detects anomalies, and injects hardware
  context into the agent system prompt before every turn.
author: Project Sentinel (Samsung PRISM AX Hackathon)
license: Apache-2.0

requires:
  runtime: python3
  min_python: "3.10"
  packages:
    - psutil>=5.9.0

env:
  SENTINEL_LOG_PATH:
    description: Path to the sentinel redo log written by the engine
    default: "../logs/sentinel_redo.log"
  SENTINEL_POLL_MS:
    description: How often to poll the log (milliseconds)
    default: "500"
  SENTINEL_MOCK:
    description: Use mock hardware data (nominal/warn/critical) — for testing
    default: ""

triggers:
  - on: session_start
    run: init
  - on: before_turn
    run: inject_context
  - on: after_turn
    run: log_event
---

# Sentinel Hardware Skill

## What This Skill Does

This skill bridges the C++ or Python hardware engine with the OpenClaw agent. Before every conversation turn, it:

1. Reads the latest entry from `sentinel_redo.log`
2. Runs it through the ContextAnalyser (rolling forecast + mode selection)
3. Runs it through the AnomalyDetector (8 failure-signature patterns)
4. Injects a structured `## Device Hardware Context` block into the system prompt
5. Sets the agent's `max_tokens` to the computed token budget
6. Logs notable events to the session memory file

## Log Format

The skill reads lines in this format (written by the C++ or Python engine):

```
LSN:1000042 | TS:1700000123456 | RAM_USED:67.3% | RAM_FREE_MB:2048.5 | CPU:34.1% | THERMAL_C:62.0 | BAT:55.0(DC) | DISK:71.2% | STATE:WARN | TREND:RISING | DELTA:+0.8%
```

## Injected System Prompt Block

```
## Device Hardware Context [Sentinel v1.0]
Current time: 14:32:07
RAM usage:    67.3% (2048 MB free)
CPU load:     34.1%
Temperature:  62.0°C
Battery:      55% (on battery)
Pressure:     WARN (trend: RISING)
Active alerts: RAM_WARN
Forecast: Memory is projected to climb from 67% to 79% over the next ~3 seconds.

## Adaptive Operating Instructions
Mode:            QUANTIZED
Reasoning depth: STANDARD
Response budget: ≤512 tokens
```

## Anomaly Patterns Detected

| Pattern | Trigger | Severity |
|---|---|---|
| RAM_SPIKE | RAM jumps > 10% in one sample | HIGH/CRITICAL |
| RAM_CREEP | Linear regression forecasts critical in < 30s | MEDIUM/HIGH |
| THERMAL_RUNAWAY | Temp rising faster than CPU load justifies | MEDIUM/CRITICAL |
| BATTERY_CLIFF | Drain rate z-score > 2.0 | MEDIUM/HIGH |
| CPU_THRASH | CPU variance ratio > 4× baseline | MEDIUM |
| COMPOUND_PRESSURE | 3+ metrics degrading simultaneously | HIGH/CRITICAL |
| MEMORY_OSCILLATION | RAM variance ratio > 3× (GC storm) | MEDIUM |
| RECOVERY_DETECTED | Resources improve after CRITICAL state | LOW |

## Running the Skill Standalone

```bash
# With live engine running
python skills/sentinel-hardware/run.py

# Mock mode for testing (no engine needed)
SENTINEL_MOCK=warn python skills/sentinel-hardware/run.py

# Demo all scenarios
python skills/sentinel-hardware/run.py --demo
```

## Files

```
skills/sentinel-hardware/
├── SKILL.md          This file
├── run.py            Skill entry point (called by OpenClaw)
├── bridge.py         Log watcher + ContextAnalyser (thin wrapper)
└── mock.py           Mock hardware data for testing
```
