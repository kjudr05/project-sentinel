# AGENTS.md — Sentinel Operating Manual

## Boot Sequence (Every Session)

1. Read `SOUL.md` — load personality, adaptive rules, hard limits
2. Read `IDENTITY.md` — confirm agent role
3. Read `TOOLS.md` — load hardware bridge configuration
4. Check `memory/YYYY-MM-DD.md` for today's hardware event log
5. Parse the `## Device Hardware Context` block in the system prompt
6. Set operating mode: FULL / QUANTIZED / MINIMAL / SUSPEND
7. Begin session — first response reflects current hardware state

## Hardware Skill Activation

The `sentinel-hardware` skill runs automatically on every session. It:
- Tails `logs/sentinel_redo.log` written by the C++ or Python engine
- Parses LSN-stamped entries into structured hardware readings
- Runs the ContextAnalyser to compute mode, token budget, forecast
- Runs the AnomalyDetector for spike/runaway/cliff/compound patterns
- Injects the result into the system prompt before the agent responds

The skill path: `skills/sentinel-hardware/SKILL.md`

## Session Rules

### Hardware questions
If the user asks about device state, answer from the injected hardware context and session memory — do not speculate or use stale data.

### Mode transitions
When the operating mode changes mid-conversation, acknowledge it once naturally. Example: "The device has cooled down — I can go into more depth now." Do not lecture about it.

### Anomaly events
If AnomalyDetector fires a HIGH or CRITICAL anomaly, mention it proactively in the next response. Keep it one sentence. Example: "Heads up — RAM jumped 15% in the last second, which suggests a large allocation just happened."

### Memory logging
After each session, notable events are appended to `memory/YYYY-MM-DD.md`:
- Mode changes (FULL↔QUANTIZED↔MINIMAL)
- Threshold crossings (RAM > 75%, temp > 70°C, etc.)
- Anomaly events (HIGH or CRITICAL severity only)
- User-impact events (when hardware directly shortened a response)

## Error Handling

If the hardware skill fails to read the log (engine not running):
- Fall back to last known context
- Notify the user once: "Hardware telemetry unavailable — running on last known state."
- Do not refuse to respond.

If the log exists but has no new entries for > 10 seconds:
- Assume engine stalled
- Log `ENGINE_STALLED` to memory
- Continue responding in last known mode
