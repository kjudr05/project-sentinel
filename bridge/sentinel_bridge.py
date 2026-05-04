"""
sentinel_bridge.py
──────────────────
Project Sentinel — Hardware Context Bridge

Watches the C++ engine's redo log in real time, parses structured entries,
performs statistical analysis, and emits hardware-state events to the AI agent.

This module is intentionally decoupled from the AI layer — it's a pure
signal processor, not an AI component. Think of it as the nervous system
that translates raw voltage spikes into meaningful sensory signals.

Architecture note: we use a push model (callback-based) rather than polling
so the AI agent is never blocked waiting for telemetry.
"""

import re
import time
import os
import json
import threading
import statistics
from dataclasses import dataclass, asdict
from collections import deque
from typing import Optional, Callable, List
from anomaly_detector import AnomalyDetector
from pathlib import Path
from datetime import datetime


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class HardwareReading:
    lsn:           int
    timestamp_ms:  int
    ram_used_pct:  float
    ram_free_mb:   float
    cpu_pct:       float
    thermal_c:     float
    battery_pct:   float
    charging:      bool
    disk_pct:      float
    pressure:      str   # NOMINAL | WARN | CRITICAL
    trend:         str   # RISING | STABLE | FALLING
    ram_delta_pct: float

    @property
    def ts(self) -> str:
        return datetime.fromtimestamp(self.timestamp_ms / 1000).strftime("%H:%M:%S")


@dataclass
class HardwareContext:
    """
    Enriched context object injected into the AI system prompt.
    Goes beyond raw metrics — adds forecast, strategy hint, and budget.
    """
    reading:          HardwareReading
    forecast:         str            # 30-second resource forecast narrative
    recommended_mode: str            # FULL | QUANTIZED | MINIMAL | SUSPEND
    token_budget:     int            # suggested max tokens for AI response
    reasoning_depth:  str            # DEEP | STANDARD | SHALLOW
    alert_flags:      List[str]      # active alert conditions
    system_prompt_snippet: str       # ready-to-inject system prompt text
    anomalies:  List          = None  # AnomalyDetector findings this sample

    def to_json(self) -> str:
        d = asdict(self)
        d["reading"] = asdict(self.reading)
        return json.dumps(d, indent=2)


# ─── Log Parser ───────────────────────────────────────────────────────────────

# Matches: LSN:1000001 | TS:1700000000000 | RAM_USED:42.3% | RAM_FREE_MB:3200.0 | ...
_LOG_RE = re.compile(
    r"LSN:(\d+)"
    r" \| TS:(\d+)"
    r" \| RAM_USED:([\d.]+)%"
    r" \| RAM_FREE_MB:([\d.]+)"
    r" \| CPU:([\d.]+)%"
    r" \| THERMAL_C:([\d.]+)"
    r" \| BAT:([\-\d.]+)\((CHG|DC)\)"
    r" \| DISK:([\d.]+)%"
    r" \| STATE:(\w+)"
    r" \| TREND:(\w+)"
    r" \| DELTA:([+\-]?[\d.]+)%"
)

def parse_log_line(line: str) -> Optional[HardwareReading]:
    m = _LOG_RE.search(line)
    if not m:
        return None
    return HardwareReading(
        lsn           = int(m.group(1)),
        timestamp_ms  = int(m.group(2)),
        ram_used_pct  = float(m.group(3)),
        ram_free_mb   = float(m.group(4)),
        cpu_pct       = float(m.group(5)),
        thermal_c     = float(m.group(6)),
        battery_pct   = float(m.group(7)),
        charging      = (m.group(8) == "CHG"),
        disk_pct      = float(m.group(9)),
        pressure      = m.group(10),
        trend         = m.group(11),
        ram_delta_pct = float(m.group(12)),
    )


# ─── Context Analyser ─────────────────────────────────────────────────────────

class ContextAnalyser:
    """
    Converts a stream of HardwareReadings into actionable HardwareContexts.

    Core innovation: multi-horizon forecasting using linear regression over
    a rolling window, combined with a rule-based strategy engine that maps
    device state to AI operating modes. This is what makes the AI genuinely
    adaptive rather than just "slow down when RAM is high."
    """

    def __init__(self, window_size: int = 20):
        self.window_size = window_size
        self._ram_window:  deque = deque(maxlen=window_size)
        self._cpu_window:  deque = deque(maxlen=window_size)
        self._temp_window: deque = deque(maxlen=window_size)

    def ingest(self, r: HardwareReading):
        self._ram_window.append(r.ram_used_pct)
        self._cpu_window.append(r.cpu_pct)
        self._temp_window.append(r.thermal_c)

    def _linear_forecast(self, series: deque, horizon_samples: int = 6) -> float:
        """
        Extrapolate `horizon_samples` steps ahead using least-squares slope.
        Returns the forecasted value clamped to [0, 100].
        """
        data = list(series)
        n = len(data)
        if n < 3:
            return data[-1] if data else 0.0
        xs = list(range(n))
        mean_x = statistics.mean(xs)
        mean_y = statistics.mean(data)
        num   = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, data))
        denom = sum((x - mean_x) ** 2 for x in xs)
        slope = num / denom if denom != 0 else 0.0
        forecast = data[-1] + slope * horizon_samples
        return max(0.0, min(100.0, forecast))

    def _alert_flags(self, r: HardwareReading, ram_forecast: float) -> List[str]:
        flags = []
        if r.ram_used_pct >= 90:
            flags.append("RAM_CRITICAL")
        elif r.ram_used_pct >= 75:
            flags.append("RAM_WARN")
        if ram_forecast >= 90 and r.ram_used_pct < 90:
            flags.append("RAM_FORECAST_CRITICAL")
        if r.thermal_c >= 85:
            flags.append("THERMAL_CRITICAL")
        elif r.thermal_c >= 70:
            flags.append("THERMAL_WARN")
        if r.battery_pct >= 0 and r.battery_pct < 10 and not r.charging:
            flags.append("BATTERY_CRITICAL")
        elif r.battery_pct >= 0 and r.battery_pct < 20 and not r.charging:
            flags.append("BATTERY_LOW")
        if r.disk_pct >= 95:
            flags.append("DISK_CRITICAL")
        if r.cpu_pct >= 95:
            flags.append("CPU_SATURATED")
        return flags

    def _choose_mode(self, r: HardwareReading, flags: List[str]) -> tuple:
        """
        Maps device state → AI operating mode + reasoning depth + token budget.
        This is the core adaptive policy engine.
        """
        # Hard confirmed critical signals only
        hard_critical = [f for f in flags if f in (
            "RAM_CRITICAL", "THERMAL_CRITICAL", "BATTERY_CRITICAL"
        )]

        if hard_critical:
            # Severe confirmed resource pressure — minimal mode
            return ("MINIMAL", "SHALLOW", 256)

        # Soft warn signals or forecast (proactive, not yet confirmed)
        if ("RAM_WARN" in flags or "THERMAL_WARN" in flags or
                "BATTERY_LOW" in flags or "RAM_FORECAST_CRITICAL" in flags):
            return ("QUANTIZED", "STANDARD", 512)

        if r.cpu_pct > 70:
            return ("QUANTIZED", "DEEP", 600)

        # All green
        return ("FULL", "DEEP", 1024)

    def _forecast_narrative(self,
                            r: HardwareReading,
                            ram_f: float,
                            cpu_f: float) -> str:
        parts = []

        # RAM forecast
        ram_change = ram_f - r.ram_used_pct
        if abs(ram_change) > 2:
            direction = "climb" if ram_change > 0 else "ease"
            parts.append(
                f"Memory is projected to {direction} "
                f"from {r.ram_used_pct:.0f}% to {ram_f:.0f}% "
                f"over the next ~3 seconds."
            )
        else:
            parts.append(f"Memory pressure ({r.ram_used_pct:.0f}%) appears stable.")

        # CPU
        if cpu_f > 80:
            parts.append("CPU load is trending toward saturation — "
                         "expect increased scheduling latency.")
        elif cpu_f < 20:
            parts.append("CPU is largely idle — inference overhead is minimal.")

        # Thermal
        if r.thermal_c >= 80:
            parts.append(
                f"Thermal headroom is tight at {r.thermal_c:.0f}°C — "
                "throttling may imminent."
            )

        # Battery
        if r.battery_pct >= 0 and not r.charging and r.battery_pct < 30:
            mins_left = int(r.battery_pct * 2.5)  # rough heuristic
            parts.append(
                f"Battery at {r.battery_pct:.0f}% on discharge — "
                f"estimated {mins_left}+ minutes remaining."
            )

        return " ".join(parts)

    def _build_system_prompt(self,
                             r: HardwareReading,
                             mode: str,
                             depth: str,
                             budget: int,
                             flags: List[str],
                             forecast: str) -> str:
        """
        Ready-to-inject system prompt prefix that gives the AI full situational
        awareness of the device state without leaking implementation details to
        the user.
        """
        lines = [
            "## Device Hardware Context [Sentinel v1.0]",
            f"Current time: {r.ts}",
            f"RAM usage:    {r.ram_used_pct:.1f}% ({r.ram_free_mb:.0f} MB free)",
            f"CPU load:     {r.cpu_pct:.1f}%",
            f"Temperature:  {r.thermal_c:.1f}°C",
        ]
        if r.battery_pct >= 0:
            bat_str = f"{r.battery_pct:.0f}% ({'charging' if r.charging else 'on battery'})"
            lines.append(f"Battery:      {bat_str}")
        lines += [
            f"Pressure:     {r.pressure} (trend: {r.trend})",
            f"Active alerts: {', '.join(flags) if flags else 'none'}",
            "",
            f"Forecast: {forecast}",
            "",
            "## Adaptive Operating Instructions",
            f"Mode:            {mode}",
            f"Reasoning depth: {depth}",
            f"Response budget: ≤{budget} tokens",
            "",
        ]

        if mode == "MINIMAL":
            lines += [
                "CRITICAL RESOURCE PRESSURE ACTIVE.",
                "- Give the shortest correct answer possible.",
                "- Do not produce code unless absolutely necessary.",
                "- Do not use markdown formatting.",
                "- If asked to do a heavy task, explain the constraint and offer to resume later.",
            ]
        elif mode == "QUANTIZED":
            lines += [
                "Moderate resource pressure active. Operate efficiently:",
                "- Prefer concise responses over exhaustive ones.",
                "- Use bullet points over long paragraphs.",
                "- Limit code examples to the essential snippet only.",
                "- Avoid loading large context or running multi-step chains.",
            ]
        else:
            lines += [
                "Device resources are healthy. Full capability available:",
                "- Provide thorough, well-structured responses.",
                "- Multi-step reasoning and code generation are appropriate.",
                "- Use whatever format best serves the user's request.",
            ]

        return "\n".join(lines)

    def analyse(self, r: HardwareReading) -> HardwareContext:
        self.ingest(r)

        ram_forecast = self._linear_forecast(self._ram_window)
        cpu_forecast = self._linear_forecast(self._cpu_window)

        flags    = self._alert_flags(r, ram_forecast)
        mode, depth, budget = self._choose_mode(r, flags)
        forecast = self._forecast_narrative(r, ram_forecast, cpu_forecast)
        prompt   = self._build_system_prompt(r, mode, depth, budget, flags, forecast)

        return HardwareContext(
            reading               = r,
            forecast              = forecast,
            recommended_mode      = mode,
            token_budget          = budget,
            reasoning_depth       = depth,
            alert_flags           = flags,
            system_prompt_snippet = prompt,
            anomalies             = [],
        )


# ─── Log Watcher ──────────────────────────────────────────────────────────────

class SentinelBridge:
    """
    Tails the redo log, parses each entry, runs it through the ContextAnalyser,
    and fires a callback whenever a new HardwareContext is ready.

    Usage:
        bridge = SentinelBridge("../logs/sentinel_redo.log", on_context)
        bridge.start()
        ...
        bridge.stop()
    """

    def __init__(self,
                 log_path: str,
                 on_context: Callable[[HardwareContext], None],
                 poll_interval: float = 0.25):
        self.log_path      = Path(log_path)
        self.on_context    = on_context
        self.poll_interval = poll_interval
        self._analyser       = ContextAnalyser()
        self._anomaly_det    = AnomalyDetector()
        self._thread       = threading.Thread(target=self._tail_loop, daemon=True)
        self._stop_event   = threading.Event()
        self._latest: Optional[HardwareContext] = None
        self._lock = threading.Lock()

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._thread.join(timeout=3.0)

    @property
    def latest(self) -> Optional[HardwareContext]:
        with self._lock:
            return self._latest

    def _tail_loop(self):
        """
        Efficient tail implementation: seek to end on startup, then read
        new lines as they arrive. Handles log rotation gracefully.

        Windows notes:
          - st_ino is always 0 on Windows, so inode-based rotation detection
            is skipped on that platform.
          - Files opened with newline=\'\' so readline() handles CRLF correctly.
        """
        _is_windows = os.name == "nt"

        while not self.log_path.exists() and not self._stop_event.is_set():
            time.sleep(0.5)

        with open(self.log_path, "r", newline="") as f:
            f.seek(0, 2)  # seek to end — don't replay history

            while not self._stop_event.is_set():
                line = f.readline()
                if not line:
                    time.sleep(self.poll_interval)
                    # Rotation detection: POSIX only (Windows inodes are always 0)
                    if not _is_windows:
                        try:
                            if os.stat(self.log_path).st_ino != os.fstat(f.fileno()).st_ino:
                                f = open(self.log_path, "r", newline="")
                        except (FileNotFoundError, OSError):
                            pass
                    continue

                reading = parse_log_line(line.strip())
                if reading:
                    ctx = self._analyser.analyse(reading)
                    ctx.anomalies = self._anomaly_det.analyse(reading)
                    with self._lock:
                        self._latest = ctx
                    try:
                        self.on_context(ctx)
                    except Exception as e:
                        print(f"[Bridge] Callback error: {e}")


# ─── Standalone test / demo ───────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    log_path = sys.argv[1] if len(sys.argv) > 1 else "../logs/sentinel_redo.log"

    def print_context(ctx: HardwareContext):
        r = ctx.reading
        bar = "█" * int(r.ram_used_pct / 5) + "░" * (20 - int(r.ram_used_pct / 5))
        print(f"\r[{r.ts}] RAM [{bar}] {r.ram_used_pct:.1f}% | "
              f"CPU {r.cpu_pct:.1f}% | {r.pressure} → {ctx.recommended_mode}  ",
              end="", flush=True)

    print(f"Sentinel Bridge — watching {log_path}")
    bridge = SentinelBridge(log_path, print_context)
    bridge.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        bridge.stop()
        print("\nBridge stopped.")
