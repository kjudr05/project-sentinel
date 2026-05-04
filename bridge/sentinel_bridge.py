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
from datetime import datetime
import threading
import statistics
from dataclasses import dataclass, asdict
from collections import deque
from typing import Optional, Callable, List
from pathlib import Path


from anomaly_detector import AnomalyDetector


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class HardwareReading:
    lsn: int
    timestamp_ms: int
    ram_used_pct: float
    ram_free_mb: float
    cpu_pct: float
    thermal_c: float
    battery_pct: float
    charging: bool
    disk_pct: float
    pressure: str   # NOMINAL | WARN | CRITICAL
    trend: str      # RISING | STABLE | FALLING
    ram_delta_pct: float

    @property
    def ts(self) -> str:
        return datetime.fromtimestamp(
            self.timestamp_ms / 1000
        ).strftime("%H:%M:%S")


@dataclass
class HardwareContext:
    """
    Enriched context object injected into the AI system prompt.
    Goes beyond raw metrics — adds forecast, strategy hint, and budget.
    """
    reading: HardwareReading
    forecast: str
    recommended_mode: str        # FULL | QUANTIZED | MINIMAL | SUSPEND
    token_budget: int
    reasoning_depth: str         # DEEP | STANDARD | SHALLOW
    alert_flags: List[str]
    system_prompt_snippet: str
    anomalies: List = None       # AnomalyDetector findings this sample

    def to_json(self) -> str:
        d = asdict(self)
        d["reading"] = asdict(self.reading)
        return json.dumps(d, indent=2)


# ─── Log Parser ───────────────────────────────────────────────────────────────

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
    """
    Parse one redo-log line into a HardwareReading.
    Returns None if format is invalid.
    """
    match = _LOG_RE.search(line)
    if not match:
        return None

    return HardwareReading(
        lsn=int(match.group(1)),
        timestamp_ms=int(match.group(2)),
        ram_used_pct=float(match.group(3)),
        ram_free_mb=float(match.group(4)),
        cpu_pct=float(match.group(5)),
        thermal_c=float(match.group(6)),
        battery_pct=float(match.group(7)),
        charging=(match.group(8) == "CHG"),
        disk_pct=float(match.group(9)),
        pressure=match.group(10),
        trend=match.group(11),
        ram_delta_pct=float(match.group(12)),
    )


# ─── Context Analyser ─────────────────────────────────────────────────────────

class ContextAnalyser:
    """
    Converts a stream of HardwareReadings into actionable HardwareContexts.

    Core innovation:
    Multi-horizon forecasting using rolling-window linear regression,
    combined with a rule-based strategy engine.
    """

    def __init__(self, window_size: int = 20):
        self.window_size = window_size
        self._ram_window = deque(maxlen=window_size)
        self._cpu_window = deque(maxlen=window_size)
        self._temp_window = deque(maxlen=window_size)

    def ingest(self, reading: HardwareReading) -> None:
        self._ram_window.append(reading.ram_used_pct)
        self._cpu_window.append(reading.cpu_pct)
        self._temp_window.append(reading.thermal_c)

    def _linear_forecast(
        self,
        series: deque,
        horizon_samples: int = 6
    ) -> float:
        """
        Extrapolate horizon_samples ahead using least-squares slope.
        Returns clamped [0,100].
        """
        data = list(series)
        n = len(data)

        if n < 3:
            return data[-1] if data else 0.0

        xs = list(range(n))
        mean_x = statistics.mean(xs)
        mean_y = statistics.mean(data)

        numerator = sum(
            (x - mean_x) * (y - mean_y)
            for x, y in zip(xs, data)
        )

        denominator = sum(
            (x - mean_x) ** 2
            for x in xs
        )

        slope = numerator / denominator if denominator else 0.0
        forecast = data[-1] + slope * horizon_samples

        return max(0.0, min(100.0, forecast))

    def _alert_flags(
        self,
        reading: HardwareReading,
        ram_forecast: float
    ) -> List[str]:
        flags = []

        if reading.ram_used_pct >= 90:
            flags.append("RAM_CRITICAL")
        elif reading.ram_used_pct >= 75:
            flags.append("RAM_WARN")

        if ram_forecast >= 90 and reading.ram_used_pct < 90:
            flags.append("RAM_FORECAST_CRITICAL")

        if reading.thermal_c >= 85:
            flags.append("THERMAL_CRITICAL")
        elif reading.thermal_c >= 70:
            flags.append("THERMAL_WARN")

        if (
            reading.battery_pct >= 0
            and reading.battery_pct < 10
            and not reading.charging
        ):
            flags.append("BATTERY_CRITICAL")

        elif (
            reading.battery_pct >= 0
            and reading.battery_pct < 20
            and not reading.charging
        ):
            flags.append("BATTERY_LOW")

        if reading.disk_pct >= 95:
            flags.append("DISK_CRITICAL")

        if reading.cpu_pct >= 95:
            flags.append("CPU_SATURATED")

        return flags

    def _choose_mode(
        self,
        reading: HardwareReading,
        flags: List[str]
    ) -> tuple:
        """
        Device state → operating mode + reasoning depth + token budget
        """
        hard_critical = [
            flag for flag in flags
            if flag in (
                "RAM_CRITICAL",
                "THERMAL_CRITICAL",
                "BATTERY_CRITICAL",
            )
        ]

        if hard_critical:
            return ("MINIMAL", "SHALLOW", 256)

        if (
            "RAM_WARN" in flags
            or "THERMAL_WARN" in flags
            or "BATTERY_LOW" in flags
            or "RAM_FORECAST_CRITICAL" in flags
        ):
            return ("QUANTIZED", "STANDARD", 512)

        if reading.cpu_pct > 70:
            return ("QUANTIZED", "DEEP", 600)

        return ("FULL", "DEEP", 1024)

    def _forecast_narrative(
        self,
        reading: HardwareReading,
        ram_forecast: float,
        cpu_forecast: float
    ) -> str:
        parts = []

        ram_change = ram_forecast - reading.ram_used_pct

        if abs(ram_change) > 2:
            direction = "climb" if ram_change > 0 else "ease"
            parts.append(
                f"Memory is projected to {direction} "
                f"from {reading.ram_used_pct:.0f}% "
                f"to {ram_forecast:.0f}% over the next ~3 seconds."
            )
        else:
            parts.append(
                f"Memory pressure ({reading.ram_used_pct:.0f}%) appears stable."
            )

        if cpu_forecast > 80:
            parts.append(
                "CPU load is trending toward saturation — "
                "expect increased scheduling latency."
            )
        elif cpu_forecast < 20:
            parts.append(
                "CPU is largely idle — inference overhead is minimal."
            )

        if reading.thermal_c >= 80:
            parts.append(
                f"Thermal headroom is tight at "
                f"{reading.thermal_c:.0f}°C — throttling may be imminent."
            )

        if (
            reading.battery_pct >= 0
            and not reading.charging
            and reading.battery_pct < 30
        ):
            mins_left = int(reading.battery_pct * 2.5)

            parts.append(
                f"Battery at {reading.battery_pct:.0f}% on discharge — "
                f"estimated {mins_left}+ minutes remaining."
            )

        return " ".join(parts)

    def _build_system_prompt(
        self,
        reading: HardwareReading,
        mode: str,
        depth: str,
        budget: int,
        flags: List[str],
        forecast: str
    ) -> str:
        lines = [
            "## Device Hardware Context [Sentinel v1.0]",
            f"Current time: {reading.ts}",
            f"RAM usage:    {reading.ram_used_pct:.1f}% "
            f"({reading.ram_free_mb:.0f} MB free)",
            f"CPU load:     {reading.cpu_pct:.1f}%",
            f"Temperature:  {reading.thermal_c:.1f}°C",
        ]

        if reading.battery_pct >= 0:
            battery_status = (
                f"{reading.battery_pct:.0f}% "
                f"({'charging' if reading.charging else 'on battery'})"
            )
            lines.append(f"Battery:      {battery_status}")

        lines += [
            f"Pressure:     {reading.pressure} (trend: {reading.trend})",
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
                "- If asked to do a heavy task, explain constraints.",
            ]

        elif mode == "QUANTIZED":
            lines += [
                "Moderate resource pressure active.",
                "- Prefer concise responses.",
                "- Use bullets over long paragraphs.",
                "- Limit code to essential snippets.",
                "- Avoid large multi-step chains.",
            ]

        else:
            lines += [
                "Device resources are healthy.",
                "- Full reasoning available.",
                "- Thorough responses allowed.",
                "- Complex generation acceptable.",
            ]

        return "\n".join(lines)

    def analyse(self, reading: HardwareReading) -> HardwareContext:
        self.ingest(reading)

        ram_forecast = self._linear_forecast(self._ram_window)
        cpu_forecast = self._linear_forecast(self._cpu_window)

        flags = self._alert_flags(reading, ram_forecast)

        mode, depth, budget = self._choose_mode(reading, flags)

        forecast = self._forecast_narrative(
            reading,
            ram_forecast,
            cpu_forecast
        )

        prompt = self._build_system_prompt(
            reading,
            mode,
            depth,
            budget,
            flags,
            forecast
        )

        return HardwareContext(
            reading=reading,
            forecast=forecast,
            recommended_mode=mode,
            token_budget=budget,
            reasoning_depth=depth,
            alert_flags=flags,
            system_prompt_snippet=prompt,
            anomalies=[],
        )


# ─── Log Watcher ──────────────────────────────────────────────────────────────

class SentinelBridge:
    """
    Tails redo log, parses entries, analyzes them,
    and triggers callback with HardwareContext.
    """

    def __init__(
        self,
        log_path: str,
        on_context: Callable[[HardwareContext], None],
        poll_interval: float = 0.25,
    ):
        self.log_path = Path(log_path)
        self.on_context = on_context
        self.poll_interval = poll_interval

        self._analyser = ContextAnalyser()
        self._anomaly_det = AnomalyDetector()

        self._thread = threading.Thread(
            target=self._tail_loop,
            daemon=True,
        )

        self._stop_event = threading.Event()
        self._latest: Optional[HardwareContext] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=3.0)

    @property
    def latest(self) -> Optional[HardwareContext]:
        with self._lock:
            return self._latest

    def _tail_loop(self) -> None:
        """
        Efficient tail:
        - Starts at EOF
        - Reads new lines only
        - Handles POSIX log rotation
        """
        is_windows = os.name == "nt"

        while (
            not self.log_path.exists()
            and not self._stop_event.is_set()
        ):
            time.sleep(0.5)

        with open(self.log_path, "r", newline="") as log_file:
            log_file.seek(0, os.SEEK_END)

            while not self._stop_event.is_set():
                line = log_file.readline()

                if not line:
                    time.sleep(self.poll_interval)

                    if not is_windows:
                        try:
                            if (
                                os.stat(self.log_path).st_ino
                                != os.fstat(log_file.fileno()).st_ino
                            ):
                                log_file = open(
                                    self.log_path,
                                    "r",
                                    newline="",
                                )

                        except (FileNotFoundError, OSError):
                            pass

                    continue

                reading = parse_log_line(line.strip())

                if not reading:
                    continue

                context = self._analyser.analyse(reading)
                context.anomalies = self._anomaly_det.analyse(reading)

                with self._lock:
                    self._latest = context

                try:
                    self.on_context(context)

                except Exception as exc:
                    print(f"[Bridge] Callback error: {exc}")


# ─── Standalone Test / Demo ──────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    log_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "../logs/sentinel_redo.log"
    )

    def print_context(context: HardwareContext) -> None:
        reading = context.reading

        filled = int(reading.ram_used_pct / 5)
        bar = "█" * filled + "░" * (20 - filled)

        print(
            f"\r[{reading.ts}] "
            f"RAM [{bar}] {reading.ram_used_pct:.1f}% | "
            f"CPU {reading.cpu_pct:.1f}% | "
            f"{reading.pressure} → {context.recommended_mode}  ",
            end="",
            flush=True,
        )

    print(f"Sentinel Bridge — watching {log_path}")

    bridge = SentinelBridge(
        log_path=log_path,
        on_context=print_context,
    )

    bridge.start()

    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        bridge.stop()
        print("\nBridge stopped.")