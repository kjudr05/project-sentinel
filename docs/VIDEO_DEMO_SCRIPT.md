# Video Demo Guide — Project Sentinel
## Samsung PRISM AX Hackathon (10-minute walkthrough)

---

## Tools needed to record
- **Windows:** Xbox Game Bar (`Win + G`) → Record → Start Recording
- Or download **OBS Studio** (free) from obsproject.com
- Recommended resolution: 1920×1080, 30fps

---

## Scene 1 — Introduction (0:00 – 1:00)

Open a Command Prompt and the web dashboard side by side.

**Say:**
> "This is Project Sentinel — a hardware-aware AI agent for Samsung Galaxy devices.
> The problem we solved: on-device AI runs blind. It doesn't know if the phone is overheating,
> running out of RAM, or down to 5% battery. Sentinel fixes that.
> We give the AI a nervous system — real-time hardware telemetry injected into every response."

Show the architecture diagram from `docs/DESIGN.md` briefly.

---

## Scene 2 — Tests passing (1:00 – 2:00)

In Command Prompt:
```
cd C:\sentinel\sentinel
python -m pytest tests/ -v
```

**Say:**
> "133 tests — unit, integration, stress, and property tests — all passing.
> This covers the log parser, context analyser, anomaly detector, bridge, agent, and API server."

Wait for the green output. Zoom in on `133 passed`.

---

## Scene 3 — Web Dashboard (2:00 – 4:00)

Double-click `dashboard\sentinel_web.html`.

**Say:**
> "This is the live dashboard. It's showing the Full Cycle demo —
> watch the device go from healthy, to warning, to critical, and back to recovery."

Point out each element as it changes:
- RAM gauge turning yellow then red
- Mode changing: FULL → QUANTIZED → MINIMAL
- Anomaly badges appearing
- Sparklines showing the history
- Forecast text updating

Then open a second terminal and run:
```
python scripts\sentinel_api.py --mock critical
```

Reload the dashboard. Say:
> "Now it's connected to a live API stream. The dashboard updates every 500ms via Server-Sent Events."

---

## Scene 4 — AI Agent adapting (4:00 – 6:30)

Open two side-by-side terminals.

**Terminal 1 (nominal):**
```
python agent\sentinel_agent.py --mock nominal
```
Type: `What can you help me with?`

**Say:** "In FULL mode — device is healthy — the agent gives a rich, detailed response."

Show the green mode banner and the full response.

**Terminal 2 (critical):**
```
python agent\sentinel_agent.py --mock critical
```
Type: `What can you help me with?`

**Say:** "Same question, same agent, same model — but now the device is in CRITICAL state.
Watch: the mode banner turns red, the response is shorter, and the token budget drops from 1024 to 256.
The AI is literally thinking less to protect the device."

Show the red MINIMAL banner and the concise response side by side.

---

## Scene 5 — Anomaly Detection (6:30 – 8:00)

Run:
```
python scripts\demo.py --step 5
```

**Say:**
> "Beyond simple thresholds, Sentinel detects 8 failure signatures statistically.
> Here — a RAM spike: RAM jumped 15% in one sample, flagged immediately.
> Here — thermal runaway: temperature rising faster than CPU load justifies.
> Here — battery cliff: drain rate suddenly accelerated.
> These are patterns no threshold-based system can see."

---

## Scene 6 — Benchmark (8:00 – 9:00)

Run:
```
python scripts\benchmark.py
```

**Say:**
> "The overhead question: how much does Sentinel cost?
> Parse + analyse: under 1 microsecond per cycle.
> At 500ms polling, that's 0.017% CPU overhead.
> The bridge is effectively invisible to the rest of the system."

Zoom in on the overhead percentage line.

---

## Scene 7 — OpenClaw Integration (9:00 – 10:00)

Show the file tree in File Explorer:
```
openclaw/
  workspace/
    SOUL.md
    SKILL.md (inside skills/sentinel-hardware/)
    AGENTS.md
    HEARTBEAT.md
```

Open `SOUL.md` briefly.

**Say:**
> "Finally — OpenClaw integration. The SOUL.md defines the agent's hardware-aware personality.
> The custom Skill reads the redo log, runs analysis, and injects hardware context
> into the system prompt before every turn — automatically.
> This is what the hackathon asked for: an OpenClaw skill that adapts to device state."

Close with:
> "Project Sentinel. 133 tests. 0.017% overhead. Real-time hardware awareness.
> The AI that feels the device."

---

## After recording

1. Save the video as `sentinel_demo.mp4`
2. Upload to YouTube (Unlisted is fine) or Google Drive
3. Add the link to your GitHub README under a `## Video Demo` section:

```markdown
## Video Demo

[Watch the 10-minute walkthrough →](https://youtube.com/YOUR_LINK)
```
