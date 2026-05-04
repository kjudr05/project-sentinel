# Changelog

All notable changes to Project Sentinel are documented here.

---

## [1.4.0] — Phase 5 (Current)

### Added
- `LICENSE` — Apache 2.0
- `.github/workflows/ci.yml` — GitHub Actions CI across Python 3.10/3.11/3.12 on Ubuntu + Windows
- `docs/ARCHITECTURE.svg` — full architecture diagram for README
- `scripts/setup_windows.bat` — one-click Windows setup (Python + Node + OpenClaw + tests)
- `scripts/setup_unix.sh` — one-click macOS/Linux setup
- `scripts/sentinel_api.py` — three new endpoints:
  - `GET /anomalies` — current anomaly events with type, severity, narrative
  - `GET /predict` — 30-second resource forecast via linear regression
  - `GET /summary` — session statistics (peaks, means, mode/pressure distribution)
- `tests/test_openclaw_skill.py` — 20 tests covering the OpenClaw skill entry point
- `CHANGELOG.md` — this file

### Improved
- `README.md` — Video Demo section added, architecture diagram embedded
- `AI_DISCLOSURE.md` — complete disclosure of AI tools used in development

---

## [1.3.0] — Phase 4

### Added
- `agent/sentinel_memory.py` — session hardware event memory
  - Tracks mode changes, threshold crossings, anomaly events, session peaks
  - Answers hardware questions directly from memory (no API call)
  - `SessionMemory`, `HardwareEvent`, `MemoryMixin` classes
- `scripts/visualise_log.py` — static HTML report generator from any `.log` file
  - Metric timeline chart, anomaly overlays, mode bar, statistical summary
  - Works with `--demo` (synthetic) or `--input` (real log)
- `SUBMISSION.md` — hackathon one-pager with problem, contributions, results, setup
- `AI_DISCLOSURE.md` — AI tools usage disclosure (required by hackathon)
- `tests/test_memory_visualiser.py` — 39 tests (memory: 23, visualiser: 8, agent+memory: 5, events: 3)

### Fixed
- `SessionMemory._add()` — dedup key mismatch between `_add()` and `_should_record()` caused duplicate events

### Test count: 94 → 133

---

## [1.2.0] — Phase 3

### Added
- `bridge/anomaly_detector.py` — 8-pattern statistical anomaly detector
  - `RAM_SPIKE`, `RAM_CREEP`, `THERMAL_RUNAWAY`, `BATTERY_CLIFF`
  - `CPU_THRASH`, `COMPOUND_PRESSURE`, `MEMORY_OSCILLATION`, `RECOVERY_DETECTED`
  - Z-score, delta z-score, variance ratio, linear regression, IQR methods
- `scripts/sentinel_engine_py.py` — pure-Python psutil hardware engine
  - No C++ compiler needed; identical log format to C++ engine
  - Works on Windows, macOS, Linux, Android (Termux)
- `scripts/demo.py` — one-command automated demo runner (8 steps, `--quick` for ~60s)
- `tests/test_anomaly_engine.py` — 38 tests (RollingStat: 12, AnomalyDetector: 20, PythonEngine: 6)

### Improved
- `bridge/sentinel_bridge.py` — AnomalyDetector wired in; anomalies attached to HardwareContext
- `agent/sentinel_agent.py` — SessionMemory integrated; `memory_summary()` and `memory_answer()` added

### Test count: 56 → 94

---

## [1.1.0] — Phase 2

### Added
- `scripts/sentinel_api.py` — zero-dependency REST API server (stdlib `http.server`)
  - `GET /health`, `/status`, `/metrics`, `/mode`, `/history`, `/stream` (SSE)
  - `POST /simulate` — inject mock scenario for demos
- `scripts/benchmark.py` — overhead benchmark with real latency numbers
  - Result: 0.017% CPU overhead, 12,000+ lines/sec, <10ms e2e latency, <4KB heap
- `scripts/replay.py` — session replay tool
  - Built-in scenarios: `nominal`, `warn`, `crisis`, `recovery`, `full_cycle`
- `bridge/samsung_metrics.py` — Samsung-specific sysfs reader
  - Exynos thermal zone mapping (zones 1–15 for Galaxy S-series)
  - Knox warranty/MDM state, NPU utilisation, GPU load, battery cycle count
- `dashboard/sentinel_web.html` — self-contained live web dashboard
  - SSE streaming, animated gauges, sparklines, mode timeline, alert badges
  - Auto-connects to API; falls back to built-in animated demo
- `configs/engine.conf` — runtime-configurable thresholds (no recompile)
- `requirements.txt`, `setup.py` — proper Python packaging
- `tests/test_stress.py` — 11 stress tests (burst, concurrent readers, large log, memory, thread safety)

### Fixed (Windows compatibility)
- `NamedTemporaryFile` replaced with `mkstemp()` (avoids Windows file lock)
- `_tail_loop` — inode rotation detection skipped on Windows (`os.name == 'nt'`)
- Files opened with `newline=""` for CRLF handling on Windows
- All test timeouts increased (3s→8s, 5s→12s) for Windows file system latency

### Test count: 45 → 56

---

## [1.0.0] — Phase 1 (Initial Release)

### Added
- `engine/sentinel_engine.cpp` — cross-platform C++ hardware poller
  - `GlobalMemoryStatusEx` (Windows), `/proc/meminfo` + hwmon (Linux)
  - LSN-stamped redo log, 500ms polling, atomic append
  - Multi-signal composite pressure state (NOMINAL/WARN/CRITICAL)
- `bridge/sentinel_bridge.py` — log watcher + context analyser
  - `parse_log_line()` — regex parser for redo log format
  - `ContextAnalyser` — rolling window, linear regression, mode selection
  - `SentinelBridge` — threaded tail loop with rotation detection
  - `HardwareContext` — structured output with system prompt snippet
- `agent/sentinel_agent.py` — adaptive AI agent (Claude API)
  - Dynamic system prompt injection per turn
  - FULL/QUANTIZED/MINIMAL/SUSPEND mode selection
  - Echo mode for testing without API key
  - `--mock nominal|warn|critical` for demo without hardware
- `dashboard/sentinel_dashboard.py` — ANSI terminal dashboard
  - Live gauge bars, sparklines, mode timeline, alert log, forecast
- `docs/DESIGN.md` — architecture deep-dive (IPC rationale, policy engine, performance budget)
- `tests/test_sentinel.py` — 45 tests
  - `TestLogParser` (7), `TestContextAnalyser` (15), `TestSentinelBridge` (4)
  - `TestSentinelAgent` (10), `TestEndToEnd` (2), `TestProperties` (7)

### Architecture decisions
- **Redo log as IPC** — durable, debuggable, zero-dependency between processes
- **Append-only** — crash-safe; bridge seeks to EOF on startup (never replays stale data)
- **Token budget enforcement** — `max_tokens` parameter mechanically enforces the budget
- **Proactive throttle** — linear regression forecasts critical state before OS acts

---

## Summary

| Phase | Tests | Key addition |
|---|---|---|
| 1.0.0 | 45 | Core architecture (engine, bridge, agent) |
| 1.1.0 | 56 | API server, benchmark, Samsung metrics, web dashboard |
| 1.2.0 | 94 | Anomaly detection, Python engine, automated demo |
| 1.3.0 | 133 | Session memory, log visualiser, submission docs |
| 1.4.0 | 153 | OpenClaw integration, CI, architecture diagram, new API endpoints |
