# HEARTBEAT.md — Sentinel Background Tasks

## Schedule

Run every 60 seconds when the gateway is active.

## Tasks

### 1. Hardware health check
Read the latest entry from `logs/sentinel_redo.log`.
If the log has not been updated in > 10 seconds:
- Log `ENGINE_STALLED` to today's memory file
- Send a brief alert: "Hardware engine appears to have stopped. Telemetry is stale."

If the log is current and pressure is CRITICAL:
- Log the event to memory
- Do NOT alert the user proactively (they may be mid-task — wait for next message)

### 2. Memory housekeeping
If today's memory file (`memory/YYYY-MM-DD.md`) has grown beyond 50 entries:
- Summarise the oldest 25 into a single paragraph
- Replace them with the summary to keep the file compact

### 3. Session peak report (end of session only)
When the user says goodbye or the session ends:
- Write a one-paragraph summary of the session hardware profile to memory
- Format: "Session YYYY-MM-DD HH:MM — Peak RAM: X%, Peak temp: Y°C, Anomalies: N, Dominant mode: Z"

## Scope limits
- Do not send messages during heartbeat unless ENGINE_STALLED
- Do not read or write files outside the workspace and logs directories
- Do not make API calls during heartbeat
