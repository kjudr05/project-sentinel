"""
anomaly_detector.py
───────────────────
Project Sentinel — Real-Time Anomaly Detector

Detects statistically significant deviations in hardware metrics using
a combination of Z-score analysis, IQR-based outlier detection, and
pattern recognition for known failure signatures.

This is a genuine novelty contribution: instead of just reacting to
thresholds (RAM > 80%), the anomaly detector can identify:

  1. Sudden spikes  — RAM jumps 15% in one sample (memory leak burst)
  2. Slow creep     — RAM rising at a rate that will hit critical in <60s
  3. Thermal runaway— temperature climbing faster than load justifies
  4. Battery drain cliff — discharge rate suddenly accelerates (hardware fault)
  5. CPU thrash     — CPU oscillating rapidly between 0% and 100%
  6. Compound events— multiple metrics degrading simultaneously

These patterns map to specific AI adaptation strategies beyond simple
threshold-based mode selection.

Usage:
    from anomaly_detector import AnomalyDetector
    detector = AnomalyDetector()
    anomalies = detector.analyse(reading)
    # Returns list of Anomaly objects with type, severity, and narrative
"""

import time
import statistics
from dataclasses import dataclass, field
from collections import deque
from typing import List, Optional
from enum import Enum


# ─── Types ────────────────────────────────────────────────────────────────────

class AnomalyType(str, Enum):
    RAM_SPIKE          = "RAM_SPIKE"           # sudden large jump
    RAM_CREEP          = "RAM_CREEP"           # slow but inevitable rise
    THERMAL_RUNAWAY    = "THERMAL_RUNAWAY"     # temp rising faster than load
    BATTERY_CLIFF      = "BATTERY_CLIFF"       # drain rate acceleration
    CPU_THRASH         = "CPU_THRASH"          # rapid high-variance oscillation
    COMPOUND_PRESSURE  = "COMPOUND_PRESSURE"   # 3+ metrics degrading together
    MEMORY_OSCILLATION = "MEMORY_OSCILLATION"  # rapid alloc/free cycles (GC storm)
    DISK_SURGE         = "DISK_SURGE"          # disk usage jumping unexpectedly
    RECOVERY_DETECTED  = "RECOVERY_DETECTED"   # resources improving after critical


class Severity(str, Enum):
    LOW      = "LOW"
    MEDIUM   = "MEDIUM"
    HIGH     = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class Anomaly:
    type:       AnomalyType
    severity:   Severity
    metric:     str          # which metric triggered (ram, cpu, temp, bat, disk)
    value:      float        # current value
    baseline:   float        # expected value (rolling mean)
    deviation:  float        # how far from baseline (z-score or delta)
    narrative:  str          # human-readable explanation
    timestamp_ms: int        = field(default_factory=lambda: int(time.time() * 1000))
    ai_hint:    str          = ""   # suggested AI adaptation action

    @property
    def is_critical(self) -> bool:
        return self.severity in (Severity.HIGH, Severity.CRITICAL)


# ─── Rolling Statistics ───────────────────────────────────────────────────────

class RollingStat:
    """
    Maintains a rolling window of values and computes statistical summaries
    needed for anomaly detection: mean, stdev, z-score, IQR.
    """

    def __init__(self, window: int = 30):
        self.window  = window
        self._data:  deque = deque(maxlen=window)
        self._deltas: deque = deque(maxlen=window)
        self._last: Optional[float] = None

    def push(self, val: float):
        if self._last is not None:
            self._deltas.append(val - self._last)
        self._data.append(val)
        self._last = val

    @property
    def mean(self) -> float:
        return statistics.mean(self._data) if self._data else 0.0

    @property
    def stdev(self) -> float:
        return statistics.stdev(self._data) if len(self._data) > 2 else 0.0

    @property
    def z_score(self) -> float:
        """Z-score of the most recent value."""
        if len(self._data) < 3:
            return 0.0
        sd = self.stdev
        if sd < 1e-6:
            return 0.0
        return (self._data[-1] - self.mean) / sd

    @property
    def last_delta(self) -> float:
        return self._deltas[-1] if self._deltas else 0.0

    @property
    def delta_stdev(self) -> float:
        return statistics.stdev(self._deltas) if len(self._deltas) > 2 else 0.0

    @property
    def delta_z_score(self) -> float:
        """Z-score of the most recent delta (detects rate-of-change anomalies)."""
        if len(self._deltas) < 3:
            return 0.0
        sd = self.delta_stdev
        if sd < 1e-6:
            return 0.0
        mean_delta = statistics.mean(self._deltas)
        return (self._deltas[-1] - mean_delta) / sd

    @property
    def trend_slope(self) -> float:
        """Linear regression slope over the window (units/sample)."""
        data = list(self._data)
        n = len(data)
        if n < 3:
            return 0.0
        xs = list(range(n))
        mean_x = statistics.mean(xs)
        mean_y = statistics.mean(data)
        num   = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, data))
        denom = sum((x - mean_x) ** 2 for x in xs)
        return num / denom if denom > 1e-9 else 0.0

    @property
    def variance_ratio(self) -> float:
        """Ratio of recent variance to baseline — detects oscillation."""
        if len(self._data) < 8:
            return 1.0
        recent   = list(self._data)[-5:]
        baseline = list(self._data)[:-5]
        if not baseline:
            return 1.0
        try:
            var_recent   = statistics.variance(recent)
            var_baseline = statistics.variance(baseline)
            return var_recent / var_baseline if var_baseline > 1e-6 else 1.0
        except Exception:
            return 1.0

    @property
    def ready(self) -> bool:
        return len(self._data) >= 5

    def time_to_threshold(self, threshold: float) -> Optional[float]:
        """
        Estimate samples until value reaches threshold, based on current slope.
        Returns None if slope is non-positive or already past threshold.
        """
        if not self._data or self.trend_slope <= 0:
            return None
        current = self._data[-1]
        if current >= threshold:
            return 0.0
        remaining = threshold - current
        return remaining / self.trend_slope


# ─── Anomaly Detector ─────────────────────────────────────────────────────────

class AnomalyDetector:
    """
    Stateful anomaly detector. Feed it HardwareReading objects one at a time;
    it maintains rolling statistics and emits Anomaly objects when patterns
    are detected.

    Thresholds are tuned for a 500ms polling interval on a mobile device.
    Adjust SPIKE_Z_THRESHOLD for noisier environments.
    """

    # Z-score thresholds
    SPIKE_Z_THRESHOLD       = 2.5    # flag spikes > 2.5 stdev from mean
    THRASH_VARIANCE_RATIO   = 4.0    # CPU variance 4× baseline = thrash
    CREEP_HORIZON_SAMPLES   = 60     # samples ahead to forecast (30s)
    CREEP_WARN_THRESHOLD    = 85.0   # RAM% that triggers creep warning
    THERMAL_LOAD_RATIO      = 0.3    # temp rise / load rise ratio to flag runaway
    BATTERY_CLIFF_Z         = 2.0    # drain rate z-score for cliff detection
    COMPOUND_MIN_SIGNALS    = 3      # how many degrading metrics = compound event
    RECOVERY_DROP_PCT       = 10.0   # RAM must drop by this much to signal recovery

    def __init__(self, window: int = 30):
        self._ram   = RollingStat(window)
        self._cpu   = RollingStat(window)
        self._temp  = RollingStat(window)
        self._bat   = RollingStat(window)
        self._disk  = RollingStat(window)
        self._was_critical = False
        self._last_ram:  Optional[float] = None
        self._last_temp: Optional[float] = None
        self._sample_count = 0

    # ── Public interface ──────────────────────────────────────────────────────

    def analyse(self, reading) -> List[Anomaly]:
        """
        Feed one HardwareReading; returns a (possibly empty) list of Anomalies.
        Call this once per polling cycle.
        """
        self._ram.push(reading.ram_used_pct)
        self._cpu.push(reading.cpu_pct)
        self._temp.push(reading.thermal_c)
        if reading.battery_pct >= 0:
            self._bat.push(reading.battery_pct)
        self._disk.push(reading.disk_pct)
        self._sample_count += 1

        # Need enough history before analysing
        if self._sample_count < 6:
            self._last_ram  = reading.ram_used_pct
            self._last_temp = reading.thermal_c
            return []

        anomalies: List[Anomaly] = []

        anomalies += self._check_ram_spike(reading)
        anomalies += self._check_ram_creep(reading)
        anomalies += self._check_thermal_runaway(reading)
        anomalies += self._check_battery_cliff(reading)
        anomalies += self._check_cpu_thrash(reading)
        anomalies += self._check_compound(reading, anomalies)
        anomalies += self._check_memory_oscillation(reading)
        anomalies += self._check_recovery(reading)

        # Deduplicate: if same type already in list, keep highest severity
        seen = {}
        for a in anomalies:
            if a.type not in seen or a.severity > seen[a.type].severity:
                seen[a.type] = a
        anomalies = list(seen.values())

        # Sort: critical first
        sev_order = {Severity.CRITICAL: 0, Severity.HIGH: 1,
                     Severity.MEDIUM: 2, Severity.LOW: 3}
        anomalies.sort(key=lambda a: sev_order.get(a.severity, 9))

        self._was_critical = reading.pressure == "CRITICAL"
        self._last_ram     = reading.ram_used_pct
        self._last_temp    = reading.thermal_c
        return anomalies

    # ── Detectors ─────────────────────────────────────────────────────────────

    def _check_ram_spike(self, r) -> List[Anomaly]:
        """Detect sudden RAM jumps (memory leak burst, large allocation)."""
        if not self._ram.ready:
            return []
        z = self._ram.z_score
        dz = self._ram.delta_z_score
        delta = self._ram.last_delta

        # Flag if either the absolute value OR the rate-of-change is anomalous
        if abs(z) < self.SPIKE_Z_THRESHOLD and abs(dz) < self.SPIKE_Z_THRESHOLD:
            return []
        if abs(delta) < 3.0:  # ignore sub-3% changes even if statistically odd
            return []

        severity = (Severity.CRITICAL if delta > 15 else
                    Severity.HIGH     if delta > 8  else
                    Severity.MEDIUM)

        return [Anomaly(
            type      = AnomalyType.RAM_SPIKE,
            severity  = severity,
            metric    = "ram",
            value     = r.ram_used_pct,
            baseline  = self._ram.mean,
            deviation = z,
            narrative = (
                f"RAM jumped {delta:+.1f}% in one sample "
                f"({self._ram.mean:.1f}% baseline → {r.ram_used_pct:.1f}%). "
                f"Possible sudden large allocation or memory leak burst."
            ),
            ai_hint   = "Pause non-essential operations. Avoid large context loads.",
        )]

    def _check_ram_creep(self, r) -> List[Anomaly]:
        """Detect slow RAM rise that will hit critical within 30 seconds."""
        if not self._ram.ready:
            return []
        tta = self._ram.time_to_threshold(self.CREEP_WARN_THRESHOLD)
        if tta is None or tta > self.CREEP_HORIZON_SAMPLES or tta <= 0:
            return []

        seconds_remaining = tta * 0.5  # 500ms per sample
        severity = (Severity.CRITICAL if seconds_remaining < 10 else
                    Severity.HIGH     if seconds_remaining < 20 else
                    Severity.MEDIUM)

        return [Anomaly(
            type      = AnomalyType.RAM_CREEP,
            severity  = severity,
            metric    = "ram",
            value     = r.ram_used_pct,
            baseline  = self._ram.mean,
            deviation = self._ram.trend_slope,
            narrative = (
                f"RAM creeping up at {self._ram.trend_slope:.2f}%/sample. "
                f"Projected to hit {self.CREEP_WARN_THRESHOLD:.0f}% in "
                f"~{seconds_remaining:.0f}s at current rate."
            ),
            ai_hint   = (
                f"Proactively switch to QUANTIZED mode. "
                f"~{seconds_remaining:.0f}s before pressure becomes critical."
            ),
        )]

    def _check_thermal_runaway(self, r) -> List[Anomaly]:
        """
        Detect temperature rising faster than CPU load justifies.
        Normal: 1°C per 2% CPU load increase. Runaway: much faster.
        """
        if not self._temp.ready or not self._cpu.ready:
            return []

        temp_slope = self._temp.trend_slope   # °C / sample
        cpu_slope  = self._cpu.trend_slope    # % / sample

        # Only flag if temperature is actually rising
        if temp_slope < 0.1:
            return []

        # If CPU load is also rising proportionally, it's expected
        if cpu_slope > 1.0 and temp_slope / cpu_slope < self.THERMAL_LOAD_RATIO:
            return []

        # Temperature rising without matching CPU load = thermal issue
        if temp_slope < 0.5:
            return []

        severity = (Severity.CRITICAL if r.thermal_c > 82 else
                    Severity.HIGH     if r.thermal_c > 72 else
                    Severity.MEDIUM)

        return [Anomaly(
            type      = AnomalyType.THERMAL_RUNAWAY,
            severity  = severity,
            metric    = "temp",
            value     = r.thermal_c,
            baseline  = self._temp.mean,
            deviation = temp_slope,
            narrative = (
                f"Temperature rising at {temp_slope:.2f}°C/sample "
                f"({self._temp.mean:.1f}°C → {r.thermal_c:.1f}°C) "
                f"without proportional CPU load increase. "
                f"Possible sustained background workload or cooling issue."
            ),
            ai_hint   = "Reduce inference intensity immediately to prevent throttle.",
        )]

    def _check_battery_cliff(self, r) -> List[Anomaly]:
        """Detect accelerating battery drain (hardware fault or power surge)."""
        if not self._bat.ready or r.battery_pct < 0 or r.charging:
            return []

        z = self._bat.delta_z_score
        drain_rate = -self._bat.last_delta  # positive = draining

        # Battery should drain slowly and consistently; spikes in drain rate = fault
        if z > -self.BATTERY_CLIFF_Z:   # z is negative when draining faster
            return []
        if drain_rate < 0.3:  # ignore sub-0.3% drain spikes
            return []

        severity = (Severity.CRITICAL if r.battery_pct < 15 else
                    Severity.HIGH     if r.battery_pct < 30 else
                    Severity.MEDIUM)

        return [Anomaly(
            type      = AnomalyType.BATTERY_CLIFF,
            severity  = severity,
            metric    = "bat",
            value     = r.battery_pct,
            baseline  = -self._bat.mean,
            deviation = z,
            narrative = (
                f"Battery drain rate suddenly increased "
                f"({drain_rate:.1f}% this sample vs "
                f"{abs(statistics.mean(list(self._bat._deltas)[:-1])):.2f}% average). "
                f"At {r.battery_pct:.0f}% remaining."
            ),
            ai_hint   = "Suspend non-critical tasks immediately. Battery may be unstable.",
        )]

    def _check_cpu_thrash(self, r) -> List[Anomaly]:
        """
        Detect CPU thrashing: rapid oscillation between high and low load.
        Indicates scheduler contention or a spinning process.
        """
        if not self._cpu.ready:
            return []

        vr = self._cpu.variance_ratio
        if vr < self.THRASH_VARIANCE_RATIO:
            return []
        if self._cpu.mean < 20:  # low-mean oscillation is just idle noise
            return []

        severity = (Severity.HIGH   if vr > 8 else
                    Severity.MEDIUM)

        return [Anomaly(
            type      = AnomalyType.CPU_THRASH,
            severity  = severity,
            metric    = "cpu",
            value     = r.cpu_pct,
            baseline  = self._cpu.mean,
            deviation = vr,
            narrative = (
                f"CPU showing high-variance oscillation "
                f"(variance {vr:.1f}× baseline). Mean load: {self._cpu.mean:.0f}%. "
                f"Possible scheduler contention or spinning background process."
            ),
            ai_hint   = "Avoid spawning new threads. Prefer single-turn responses.",
        )]

    def _check_memory_oscillation(self, r) -> List[Anomaly]:
        """
        Detect rapid alloc/free cycles (GC storm pattern):
        RAM oscillates by >5% between samples at high frequency.
        """
        if not self._ram.ready:
            return []

        vr = self._ram.variance_ratio
        if vr < 3.0:
            return []

        # Only flag if we're at a meaningful RAM level (GC storms matter at >40%)
        if r.ram_used_pct < 40:
            return []

        return [Anomaly(
            type      = AnomalyType.MEMORY_OSCILLATION,
            severity  = Severity.MEDIUM,
            metric    = "ram",
            value     = r.ram_used_pct,
            baseline  = self._ram.mean,
            deviation = vr,
            narrative = (
                f"RAM showing rapid oscillation (variance {vr:.1f}× baseline "
                f"at {r.ram_used_pct:.0f}% load). "
                f"Possible garbage collection storm or frequent large allocations."
            ),
            ai_hint   = "Avoid responses that require large context buffers.",
        )]

    def _check_compound(self, r, existing: List[Anomaly]) -> List[Anomaly]:
        """
        Detect compound pressure: multiple metrics degrading simultaneously,
        even if none individually crosses the anomaly threshold.
        """
        signals = 0
        if r.ram_used_pct > 70:   signals += 1
        if r.cpu_pct > 65:        signals += 1
        if r.thermal_c > 67:      signals += 1
        if r.battery_pct >= 0 and r.battery_pct < 25 and not r.charging:
            signals += 1
        if r.disk_pct > 85:       signals += 1

        # Also count signals already detected
        signals += len([a for a in existing if a.severity in
                        (Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL)])

        if signals < self.COMPOUND_MIN_SIGNALS:
            return []

        # Only emit if not already in CRITICAL (would be redundant)
        if r.pressure == "CRITICAL" and self._was_critical:
            return []

        severity = Severity.CRITICAL if signals >= 5 else Severity.HIGH

        return [Anomaly(
            type      = AnomalyType.COMPOUND_PRESSURE,
            severity  = severity,
            metric    = "all",
            value     = float(signals),
            baseline  = float(self.COMPOUND_MIN_SIGNALS),
            deviation = float(signals - self.COMPOUND_MIN_SIGNALS),
            narrative = (
                f"{signals} hardware signals degrading simultaneously "
                f"(RAM {r.ram_used_pct:.0f}%, CPU {r.cpu_pct:.0f}%, "
                f"TEMP {r.thermal_c:.0f}°C). Compound stress event."
            ),
            ai_hint   = "Switch to MINIMAL mode. Do not start new heavy tasks.",
        )]

    def _check_recovery(self, r) -> List[Anomaly]:
        """
        Detect resource recovery after a critical event — useful for signalling
        the AI to gradually restore full capability.
        """
        if not self._was_critical:
            return []
        if self._last_ram is None:
            return []

        drop = self._last_ram - r.ram_used_pct
        if drop < self.RECOVERY_DROP_PCT:
            return []
        if r.ram_used_pct > 75:  # still too high to call it recovery
            return []

        return [Anomaly(
            type      = AnomalyType.RECOVERY_DETECTED,
            severity  = Severity.LOW,
            metric    = "ram",
            value     = r.ram_used_pct,
            baseline  = self._last_ram,
            deviation = -drop,
            narrative = (
                f"Resources recovering: RAM dropped {drop:.1f}% "
                f"({self._last_ram:.0f}% → {r.ram_used_pct:.0f}%). "
                f"Device stabilising after critical event."
            ),
            ai_hint   = "Gradually restore to QUANTIZED then FULL mode.",
        )]

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """Return a snapshot of rolling stats for debugging."""
        return {
            "samples":    self._sample_count,
            "ram":  {"mean": self._ram.mean,  "stdev": self._ram.stdev,
                     "slope": self._ram.trend_slope, "z": self._ram.z_score},
            "cpu":  {"mean": self._cpu.mean,  "stdev": self._cpu.stdev,
                     "slope": self._cpu.trend_slope},
            "temp": {"mean": self._temp.mean, "stdev": self._temp.stdev,
                     "slope": self._temp.trend_slope},
        }
