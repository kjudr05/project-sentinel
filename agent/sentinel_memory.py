"""
sentinel_memory.py
──────────────────
Project Sentinel — Session Hardware Memory

Gives the AI agent a structured memory of hardware events that occurred
during the current conversation. Without this, the agent is stateless
with respect to hardware history — it knows the *current* state but
cannot answer questions like:

  "Why was your last answer so short?"
  "When did my phone start getting hot?"
  "Has the RAM been rising all session?"
  "What triggered the warning 5 minutes ago?"

The memory is:
  - Compact: stores events, not raw readings (no memory bloat)
  - Queryable: natural-language summaries generated on demand
  - Injected: automatically included in the agent system prompt
  - Bounded: capped at MAX_EVENTS to prevent context overflow

Architecture:
  HardwareEvent   — a timestamped notable occurrence (mode change, anomaly, threshold)
  SessionMemory   — maintains the event log and generates summaries
  MemoryInjector  — formats memory for system prompt injection

This is a genuine novelty: no published on-device AI system maintains
structured hardware history across conversation turns.
"""

import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum
from collections import deque


# ─── Event Types ──────────────────────────────────────────────────────────────

class EventKind(str, Enum):
    MODE_CHANGE      = "MODE_CHANGE"       # AI operating mode changed
    THRESHOLD_CROSS  = "THRESHOLD_CROSS"   # metric crossed warn/critical line
    ANOMALY          = "ANOMALY"           # anomaly detector fired
    RECOVERY         = "RECOVERY"          # resources improved after critical
    SESSION_START    = "SESSION_START"     # conversation began
    USER_IMPACT      = "USER_IMPACT"       # hardware directly affected a response


@dataclass
class HardwareEvent:
    kind:        EventKind
    timestamp_ms: int
    summary:     str           # one-line human-readable description
    detail:      str           # fuller explanation for context injection
    metric:      str           # which metric (ram, cpu, temp, bat, all)
    value:       float         # metric value at time of event
    severity:    str           # LOW | MEDIUM | HIGH | CRITICAL
    ai_mode:     str           # what mode the AI was in when this happened
    lsn:         int = 0       # log sequence number for correlation

    @property
    def age_seconds(self) -> float:
        return (time.time() * 1000 - self.timestamp_ms) / 1000.0

    @property
    def age_str(self) -> str:
        s = self.age_seconds
        if s < 60:   return f"{s:.0f}s ago"
        if s < 3600: return f"{s/60:.0f}m ago"
        return f"{s/3600:.1f}h ago"

    @property
    def is_recent(self) -> bool:
        return self.age_seconds < 30.0


# ─── Session Memory ───────────────────────────────────────────────────────────

class SessionMemory:
    """
    Maintains a bounded event log for a conversation session.

    Events are added by the bridge/agent when notable hardware state
    changes occur. The memory generates natural-language summaries
    suitable for injection into the AI system prompt.
    """

    MAX_EVENTS        = 30     # cap to prevent context explosion
    DEDUP_WINDOW_S    = 5.0    # don't log the same event type twice in 5s
    SUMMARY_MAX_ITEMS = 8      # items in the injected summary

    def __init__(self):
        self._events:  deque = deque(maxlen=self.MAX_EVENTS)
        self._last_mode: Optional[str] = None
        self._last_event_times: Dict[str, float] = {}
        self._session_start_ms: int = int(time.time() * 1000)
        self._peak_ram:  float = 0.0
        self._peak_temp: float = 0.0
        self._min_bat:   float = 100.0
        self._mode_counts: Dict[str, int] = {}
        self._anomaly_counts: Dict[str, int] = {}

        # Log session start
        self._add(HardwareEvent(
            kind=EventKind.SESSION_START,
            timestamp_ms=self._session_start_ms,
            summary="Conversation started",
            detail="Session hardware monitoring began.",
            metric="all", value=0.0, severity="LOW", ai_mode="FULL"
        ))

    def record_context(self, ctx) -> List[HardwareEvent]:
        """
        Called every time a new HardwareContext arrives from the bridge.
        Returns list of new events that were recorded this cycle.
        """
        r       = ctx.reading
        mode    = ctx.recommended_mode
        new_evs = []

        # Track peaks
        self._peak_ram  = max(self._peak_ram,  r.ram_used_pct)
        self._peak_temp = max(self._peak_temp, r.thermal_c)
        if r.battery_pct >= 0:
            self._min_bat = min(self._min_bat, r.battery_pct)

        # Mode changes
        if mode != self._last_mode and self._last_mode is not None:
            direction = "↑" if self._mode_order(mode) > self._mode_order(self._last_mode) else "↓"
            ev = HardwareEvent(
                kind=EventKind.MODE_CHANGE,
                timestamp_ms=r.timestamp_ms,
                summary=f"AI mode {direction} {self._last_mode} → {mode}",
                detail=(
                    f"Operating mode changed from {self._last_mode} to {mode} "
                    f"(RAM {r.ram_used_pct:.0f}%, CPU {r.cpu_pct:.0f}%, "
                    f"{r.thermal_c:.0f}°C). "
                    f"Token budget: {ctx.token_budget}."
                ),
                metric="all", value=r.ram_used_pct,
                severity=self._mode_to_severity(mode),
                ai_mode=mode, lsn=r.lsn,
            )
            if self._should_record(ev):
                self._add(ev)
                new_evs.append(ev)
                self._mode_counts[mode] = self._mode_counts.get(mode, 0) + 1

        self._last_mode = mode

        # Threshold crossings
        threshold_evs = self._check_thresholds(r, mode)
        for ev in threshold_evs:
            if self._should_record(ev):
                self._add(ev)
                new_evs.append(ev)

        # Anomaly events
        if hasattr(ctx, "anomalies") and ctx.anomalies:
            for anomaly in ctx.anomalies:
                if anomaly.severity.value in ("HIGH", "CRITICAL"):
                    ev = HardwareEvent(
                        kind=EventKind.ANOMALY,
                        timestamp_ms=r.timestamp_ms,
                        summary=f"Anomaly: {anomaly.type.value} [{anomaly.severity.value}]",
                        detail=anomaly.narrative,
                        metric=anomaly.metric,
                        value=anomaly.value,
                        severity=anomaly.severity.value,
                        ai_mode=mode, lsn=r.lsn,
                    )
                    if self._should_record(ev):
                        self._add(ev)
                        new_evs.append(ev)
                        self._anomaly_counts[anomaly.type.value] = \
                            self._anomaly_counts.get(anomaly.type.value, 0) + 1

        return new_evs

    def record_user_impact(self, reason: str, mode: str, lsn: int = 0):
        """
        Called when a hardware constraint directly influenced a response.
        E.g. when the agent gave a shorter answer due to MINIMAL mode.
        """
        ev = HardwareEvent(
            kind=EventKind.USER_IMPACT,
            timestamp_ms=int(time.time() * 1000),
            summary=f"Response adapted: {reason}",
            detail=(
                f"A hardware constraint affected this response: {reason}. "
                f"Current mode: {mode}."
            ),
            metric="all", value=0.0,
            severity="MEDIUM", ai_mode=mode, lsn=lsn,
        )
        self._add(ev)

    # ── Summary generation ────────────────────────────────────────────────────

    def system_prompt_block(self, current_ctx=None) -> str:
        """
        Generate a compact hardware-history block for the AI system prompt.
        Designed to add context without consuming too many tokens.
        """
        lines = ["## Session Hardware History"]

        session_s = (time.time() * 1000 - self._session_start_ms) / 1000
        lines.append(f"Session duration: {session_s:.0f}s")

        # Peak stats
        lines.append(
            f"Session peaks: RAM {self._peak_ram:.0f}%  "
            f"TEMP {self._peak_temp:.0f}°C  "
            f"BAT_MIN {self._min_bat:.0f}%"
        )

        # Mode distribution
        if self._mode_counts:
            mode_str = "  ".join(f"{m}:{n}x" for m, n in self._mode_counts.items())
            lines.append(f"Mode history: {mode_str}")

        # Recent notable events (most recent first)
        notable = [e for e in reversed(list(self._events))
                   if e.kind != EventKind.SESSION_START][:self.SUMMARY_MAX_ITEMS]

        if notable:
            lines.append("\nRecent hardware events:")
            for ev in notable:
                icon = {
                    EventKind.MODE_CHANGE:     "⇄",
                    EventKind.THRESHOLD_CROSS: "⚠",
                    EventKind.ANOMALY:         "⚡",
                    EventKind.RECOVERY:        "↓",
                    EventKind.USER_IMPACT:     "✎",
                }.get(ev.kind, "·")
                lines.append(f"  {icon} [{ev.age_str}] {ev.summary}")
        else:
            lines.append("No notable hardware events this session.")

        # Anomaly summary
        if self._anomaly_counts:
            anom_str = ", ".join(
                f"{t}: {n}×" for t, n in sorted(
                    self._anomaly_counts.items(), key=lambda x: -x[1])[:4]
            )
            lines.append(f"\nAnomalies detected this session: {anom_str}")

        lines.append(
            "\nUse this history to explain why earlier responses were "
            "shorter or less detailed, and to give accurate answers "
            "about the device's hardware behaviour this session."
        )

        return "\n".join(lines)

    def answer_hardware_question(self, question: str) -> Optional[str]:
        """
        Attempt to answer common hardware-history questions directly from memory,
        without needing an API call. Returns None if the question isn't answerable.
        """
        q = question.lower()

        # "When did it get hot / start throttling?"
        if any(w in q for w in ("hot", "heat", "warm", "thermal", "throttl")):
            hot_events = [e for e in self._events
                          if "thermal" in e.metric.lower() or "THERMAL" in e.summary]
            if hot_events:
                ev = hot_events[-1]
                return (f"The device first showed thermal pressure "
                        f"{ev.age_str} ({ev.summary}). "
                        f"Peak temperature this session: {self._peak_temp:.0f}°C.")
            elif self._peak_temp > 70:
                return (f"Temperature peaked at {self._peak_temp:.0f}°C this session, "
                        f"but no explicit thermal event was logged.")

        # "Why was your last answer short?"
        if any(w in q for w in ("short", "brief", "minimal", "why", "last answer")):
            impacts = [e for e in reversed(list(self._events))
                       if e.kind == EventKind.USER_IMPACT]
            if impacts:
                return (f"My last response was adapted because: {impacts[0].detail} "
                        f"(occurred {impacts[0].age_str})")
            mode_evs = [e for e in reversed(list(self._events))
                        if e.kind == EventKind.MODE_CHANGE and "MINIMAL" in e.summary]
            if mode_evs:
                return (f"The device entered MINIMAL mode {mode_evs[0].age_str} "
                        f"({mode_evs[0].detail}), which caused shorter responses.")

        # "Has the RAM been rising?"
        if any(w in q for w in ("ram", "memory")):
            ram_evs = [e for e in self._events if "ram" in e.metric.lower()]
            if ram_evs:
                latest = ram_evs[-1]
                return (f"RAM has shown {len(ram_evs)} notable events this session. "
                        f"Peak: {self._peak_ram:.0f}%. "
                        f"Latest: {latest.summary} ({latest.age_str}).")
            return (f"RAM peaked at {self._peak_ram:.0f}% this session. "
                    f"No threshold crossings logged.")

        # "Battery status / how long left?"
        if any(w in q for w in ("battery", "bat", "charge")):
            bat_evs = [e for e in self._events if "bat" in e.metric.lower()]
            if bat_evs or self._min_bat < 100:
                return (f"Battery minimum this session: {self._min_bat:.0f}%. "
                        + (f"Battery events: {len(bat_evs)}." if bat_evs else ""))

        return None  # let the AI handle it

    # ── Event stats ───────────────────────────────────────────────────────────

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def has_had_critical(self) -> bool:
        return any(e.severity == "CRITICAL" for e in self._events)

    @property
    def recent_events(self) -> List[HardwareEvent]:
        return [e for e in self._events if e.is_recent]

    def all_events(self) -> List[HardwareEvent]:
        return list(self._events)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _add(self, ev: HardwareEvent):
        self._events.append(ev)
        key = ev.kind.value + ":" + ev.metric
        self._last_event_times[key] = time.time()

    def _should_record(self, ev: HardwareEvent) -> bool:
        """Deduplicate: don't record the same event kind too frequently."""
        key = ev.kind.value + ":" + ev.metric
        last = self._last_event_times.get(key, 0)
        return (time.time() - last) > self.DEDUP_WINDOW_S

    def _check_thresholds(self, r, mode: str) -> List[HardwareEvent]:
        events = []
        checks = [
            ("ram",  r.ram_used_pct, 90, 75, "RAM"),
            ("temp", r.thermal_c,    85, 70, "Temperature"),
        ]
        for metric, val, crit, warn, label in checks:
            if val >= crit:
                events.append(HardwareEvent(
                    kind=EventKind.THRESHOLD_CROSS,
                    timestamp_ms=r.timestamp_ms,
                    summary=f"{label} crossed CRITICAL threshold ({val:.0f}%)",
                    detail=f"{label} reached {val:.1f}, exceeding the {crit}% critical threshold.",
                    metric=metric, value=val,
                    severity="CRITICAL", ai_mode=mode, lsn=r.lsn,
                ))
            elif val >= warn:
                events.append(HardwareEvent(
                    kind=EventKind.THRESHOLD_CROSS,
                    timestamp_ms=r.timestamp_ms,
                    summary=f"{label} crossed WARN threshold ({val:.0f}%)",
                    detail=f"{label} reached {val:.1f}, above the {warn}% warning threshold.",
                    metric=metric, value=val,
                    severity="MEDIUM", ai_mode=mode, lsn=r.lsn,
                ))
        return events

    @staticmethod
    def _mode_order(mode: str) -> int:
        return {"FULL": 0, "QUANTIZED": 1, "MINIMAL": 2, "SUSPEND": 3}.get(mode, 0)

    @staticmethod
    def _mode_to_severity(mode: str) -> str:
        return {"FULL": "LOW", "QUANTIZED": "MEDIUM",
                "MINIMAL": "HIGH", "SUSPEND": "CRITICAL"}.get(mode, "LOW")


# ─── Memory-aware agent mixin ────────────────────────────────────────────────

class MemoryMixin:
    """
    Mix into SentinelAgent to add session hardware memory.
    The mixin intercepts chat() to inject memory into the system prompt
    and to record user-impact events after each response.
    """

    def __init_memory__(self):
        self._memory = SessionMemory()

    def chat_with_memory(self, user_message: str, base_chat_fn) -> str:
        """
        Wraps the base chat function with memory-aware preprocessing.
        Call this instead of chat() when memory is enabled.
        """
        ctx = self._get_context() if hasattr(self, "_get_context") else None

        # Update memory with current hardware context
        if ctx:
            self._memory.record_context(ctx)

        # Check if the question can be answered directly from memory
        direct = self._memory.answer_hardware_question(user_message)
        if direct:
            self._memory.record_user_impact(
                "Answered from hardware session memory (no API call needed)",
                ctx.recommended_mode if ctx else "FULL"
            )
            return direct

        # Otherwise call the underlying chat function
        response = base_chat_fn(user_message)

        # Record if hardware constrained the response
        if ctx and ctx.recommended_mode != "FULL":
            self._memory.record_user_impact(
                f"Response truncated to {ctx.token_budget} tokens due to "
                f"{ctx.recommended_mode} mode (pressure: {ctx.reading.pressure})",
                ctx.recommended_mode,
                lsn=ctx.reading.lsn,
            )

        return response

    @property
    def memory(self) -> SessionMemory:
        return self._memory

    def memory_summary(self) -> str:
        """Return a plain-text summary of session hardware history."""
        return self._memory.system_prompt_block()


# ─── Self-test / demo ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..","bridge"))
    from sentinel_bridge import HardwareReading, ContextAnalyser
    from anomaly_detector import AnomalyDetector

    def make_ctx(ram, cpu=30, temp=55, bat=75, charging=False):
        r = HardwareReading(
            lsn=1, timestamp_ms=int(time.time()*1000),
            ram_used_pct=ram, ram_free_mb=max(0,(100-ram)*60),
            cpu_pct=cpu, thermal_c=temp, battery_pct=bat,
            charging=charging, disk_pct=55, pressure="NOMINAL",
            trend="STABLE", ram_delta_pct=0,
        )
        a = ContextAnalyser()
        ctx = a.analyse(r)
        ctx.anomalies = []
        return ctx

    print("=== Session Memory Demo ===\n")
    mem = SessionMemory()

    # Simulate a session arc
    scenarios = [
        (32,  15, 46,  90, True,  "Chat starts, device healthy"),
        (55,  40, 58,  85, False, "User runs a task, RAM rises"),
        (78,  68, 73,  40, False, "Device under pressure"),
        (93,  90, 88,  12, False, "Critical state"),
        (35,  20, 49,  11, True,  "User closed app, recovery"),
    ]

    for ram, cpu, temp, bat, chg, label in scenarios:
        ctx = make_ctx(ram, cpu, temp, bat, chg)
        new_evs = mem.record_context(ctx)
        print(f"  {label}")
        for ev in new_evs:
            print(f"    → {ev.summary}")
        time.sleep(0.05)

    print(f"\n  Total events: {mem.event_count}")
    print(f"  Had critical: {mem.has_had_critical}")
    print()
    print(mem.system_prompt_block())
    print()
    print("--- Direct memory answers ---")
    for q in ["When did it get hot?", "Why was your last answer short?",
               "How is the RAM?", "Battery status?"]:
        ans = mem.answer_hardware_question(q)
        print(f"\nQ: {q}")
        print(f"A: {ans or '(not answerable from memory)'}")
