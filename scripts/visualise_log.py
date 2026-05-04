"""
visualise_log.py
────────────────
Project Sentinel — Session Log Visualiser

Reads any sentinel_redo.log file and generates a self-contained HTML
report showing:
  - Timeline chart of all metrics (RAM, CPU, thermal, battery)
  - Mode transition timeline
  - Anomaly events overlaid on the chart
  - Statistical summary (peaks, averages, pressure distribution)
  - Comparison with baseline (first 10 readings)

This is useful for:
  - Post-session analysis after a heavy workload
  - Hackathon demo: show judges a pre-recorded crisis session visually
  - Debugging: correlate AI behaviour with hardware events

Usage:
  python scripts/visualise_log.py --input logs/sentinel_redo.log
  python scripts/visualise_log.py --input logs/sentinel_redo.log --output report.html
  python scripts/visualise_log.py --demo   (uses built-in synthetic session)
"""

import sys
import os
import re
import json
import argparse
import statistics
import time
import tempfile
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))
from sentinel_bridge import parse_log_line, HardwareReading, ContextAnalyser
from anomaly_detector import AnomalyDetector, AnomalyType


# ─── Data loading ─────────────────────────────────────────────────────────────

def load_log(path: str) -> List[HardwareReading]:
    readings = []
    with open(path, "r", newline="", errors="ignore") as f:
        for line in f:
            r = parse_log_line(line.strip())
            if r:
                readings.append(r)
    return readings


def generate_synthetic_session() -> List[HardwareReading]:
    """Build a synthetic demo session (nominal → warn → critical → recovery)."""
    sys.path.insert(0, str(Path(__file__).parent))
    from replay import ScenarioBuilder
    import io, tempfile

    builder = ScenarioBuilder("demo")
    lines   = builder.full_cycle()
    readings = []
    for line, _ in lines:
        r = parse_log_line(line.strip())
        if r:
            readings.append(r)
    return readings


# ─── Analysis ─────────────────────────────────────────────────────────────────

def analyse_session(readings: List[HardwareReading]) -> Dict[str, Any]:
    analyser = ContextAnalyser()
    detector = AnomalyDetector()
    modes    = []
    anomaly_events = []

    for r in readings:
        ctx = analyser.analyse(r)
        modes.append(ctx.recommended_mode)
        anoms = detector.analyse(r)
        for a in anoms:
            anomaly_events.append({
                "lsn":      r.lsn,
                "ts_ms":    r.timestamp_ms,
                "type":     a.type.value,
                "severity": a.severity.value,
                "narrative":a.narrative[:120],
                "metric":   a.metric,
            })

    ram_vals  = [r.ram_used_pct for r in readings]
    cpu_vals  = [r.cpu_pct      for r in readings]
    temp_vals = [r.thermal_c    for r in readings]
    bat_vals  = [r.battery_pct  for r in readings if r.battery_pct >= 0]

    pressure_counts = {}
    for r in readings:
        pressure_counts[r.pressure] = pressure_counts.get(r.pressure, 0) + 1

    mode_counts = {}
    for m in modes:
        mode_counts[m] = mode_counts.get(m, 0) + 1

    duration_s = 0
    if len(readings) >= 2:
        duration_s = (readings[-1].timestamp_ms - readings[0].timestamp_ms) / 1000

    return {
        "n_readings":       len(readings),
        "duration_s":       duration_s,
        "ram_mean":         statistics.mean(ram_vals) if ram_vals else 0,
        "ram_peak":         max(ram_vals) if ram_vals else 0,
        "ram_min":          min(ram_vals) if ram_vals else 0,
        "cpu_mean":         statistics.mean(cpu_vals) if cpu_vals else 0,
        "cpu_peak":         max(cpu_vals) if cpu_vals else 0,
        "temp_mean":        statistics.mean(temp_vals) if temp_vals else 0,
        "temp_peak":        max(temp_vals) if temp_vals else 0,
        "bat_min":          min(bat_vals) if bat_vals else -1,
        "pressure_counts":  pressure_counts,
        "mode_counts":      mode_counts,
        "modes":            modes,
        "anomaly_events":   anomaly_events,
        "n_anomalies":      len(anomaly_events),
    }


# ─── HTML report generator ────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Sentinel Session Report</title>
<style>
  :root {{
    --bg: #0d0f14; --surface: #161a24; --border: #232840;
    --accent: #3d7aed; --green: #22c55e; --yellow: #f59e0b;
    --red: #ef4444; --text: #e2e8f0; --muted: #64748b;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text);
          font-family: 'SF Mono','Cascadia Code','Consolas',monospace;
          font-size: 13px; padding: 2rem; }}
  h1 {{ color: var(--accent); font-size: 1.1rem; margin-bottom: 0.3rem; }}
  h2 {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase;
         letter-spacing: 0.1em; margin: 1.5rem 0 0.7rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin: 1rem 0; }}
  .card {{ background: var(--surface); border: 1px solid var(--border);
           border-radius: 8px; padding: 1rem; }}
  .card .label {{ font-size: 0.7rem; color: var(--muted); text-transform: uppercase;
                  letter-spacing: 0.08em; }}
  .card .value {{ font-size: 1.6rem; font-weight: 700; margin: 0.3rem 0; }}
  .card .sub   {{ font-size: 0.75rem; color: var(--muted); }}
  .green {{ color: var(--green); }} .yellow {{ color: var(--yellow); }}
  .red   {{ color: var(--red);   }} .accent  {{ color: var(--accent); }}
  canvas {{ width: 100%; height: 200px; display: block;
            background: var(--surface); border-radius: 8px;
            border: 1px solid var(--border); }}
  .anomaly {{ padding: 0.4rem 0.8rem; border-radius: 5px; margin: 0.2rem 0;
              font-size: 0.75rem; border: 1px solid; }}
  .anomaly.CRITICAL {{ background: rgba(239,68,68,0.1); border-color: var(--red); }}
  .anomaly.HIGH     {{ background: rgba(239,68,68,0.07); border-color: rgba(239,68,68,0.5); }}
  .anomaly.MEDIUM   {{ background: rgba(245,158,11,0.1); border-color: var(--yellow); }}
  .anomaly.LOW      {{ background: rgba(100,116,139,0.1); border-color: var(--muted); }}
  .mode-bar {{ display: flex; height: 20px; border-radius: 4px; overflow: hidden;
               margin: 0.5rem 0; }}
  .mode-seg {{ display: flex; align-items: center; justify-content: center;
               font-size: 0.6rem; color: #000; font-weight: 700; }}
  table {{ width: 100%; border-collapse: collapse; margin: 0.5rem 0; }}
  th, td {{ padding: 0.4rem 0.8rem; text-align: left; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--muted); font-weight: 400; font-size: 0.7rem; text-transform: uppercase; }}
  .ts {{ color: var(--muted); }}
  footer {{ margin-top: 2rem; color: var(--muted); font-size: 0.7rem; }}
</style>
</head>
<body>
<h1>▐ Project Sentinel — Session Report</h1>
<p style="color:var(--muted);margin-top:0.3rem">Generated: {generated_at} &nbsp;·&nbsp;
{n_readings} samples &nbsp;·&nbsp; {duration_str}</p>

<h2>Summary Statistics</h2>
<div class="grid">
  <div class="card">
    <div class="label">Peak RAM</div>
    <div class="value {ram_class}">{ram_peak:.1f}%</div>
    <div class="sub">mean {ram_mean:.1f}% &nbsp; min {ram_min:.1f}%</div>
  </div>
  <div class="card">
    <div class="label">Peak CPU</div>
    <div class="value {cpu_class}">{cpu_peak:.1f}%</div>
    <div class="sub">mean {cpu_mean:.1f}%</div>
  </div>
  <div class="card">
    <div class="label">Peak Temperature</div>
    <div class="value {temp_class}">{temp_peak:.1f}°C</div>
    <div class="sub">mean {temp_mean:.1f}°C</div>
  </div>
  <div class="card">
    <div class="label">Anomalies</div>
    <div class="value {anom_class}">{n_anomalies}</div>
    <div class="sub">{pressure_str}</div>
  </div>
</div>

<h2>Metric Timeline</h2>
<canvas id="chart"></canvas>

<h2>AI Mode Distribution</h2>
<div class="mode-bar" id="mode-bar"></div>
<div style="display:flex;gap:1rem;margin-top:0.3rem;font-size:0.72rem;color:var(--muted)">
  <span style="color:var(--green)">■ FULL</span>
  <span style="color:var(--yellow)">■ QUANTIZED</span>
  <span style="color:var(--red)">■ MINIMAL</span>
</div>

<h2>Anomaly Events ({n_anomalies} total)</h2>
{anomaly_html}

<h2>Raw Mode Timeline</h2>
<table>
  <tr><th>Sample #</th><th>RAM %</th><th>CPU %</th><th>Temp °C</th><th>Pressure</th><th>Mode</th></tr>
  {mode_table_rows}
</table>

<footer>Project Sentinel · Apache 2.0 · {n_readings} log entries processed</footer>

<script>
const readings = {readings_json};
const anomalies = {anomalies_json};
const modes = {modes_json};

// Chart
const canvas = document.getElementById('chart');
const ctx    = canvas.getContext('2d');
const W = canvas.offsetWidth;
const H = canvas.offsetHeight;
canvas.width  = W * window.devicePixelRatio || W;
canvas.height = H * window.devicePixelRatio || H;
ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);

function drawLine(data, color, scale) {{
  if (!data.length) return;
  ctx.beginPath();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.shadowColor = color;
  ctx.shadowBlur = 3;
  data.forEach((v, i) => {{
    const x = (i / (data.length - 1)) * W;
    const y = H - (Math.min(100, v * scale) / 100) * H;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }});
  ctx.stroke();
  ctx.shadowBlur = 0;
}}

// Grid
[25, 50, 75].forEach(pct => {{
  const y = H - (pct / 100) * H;
  ctx.strokeStyle = '#232840'; ctx.lineWidth = 0.5;
  ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  ctx.fillStyle = '#64748b'; ctx.font = '9px monospace';
  ctx.fillText(pct + '%', 2, y - 2);
}});

drawLine(readings.map(r => r.ram_used_pct), '#3d7aed', 1);
drawLine(readings.map(r => r.cpu_pct),      '#22c55e', 1);
drawLine(readings.map(r => r.thermal_c),    '#f59e0b', 1);

// Anomaly markers
anomalies.forEach(a => {{
  const idx = readings.findIndex(r => r.lsn === a.lsn);
  if (idx < 0) return;
  const x = (idx / (readings.length - 1)) * W;
  const col = a.severity === 'CRITICAL' ? '#ef4444' : '#f59e0b';
  ctx.strokeStyle = col; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
  ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  ctx.setLineDash([]);
}});

// Legend
const legend = [
  ['RAM %', '#3d7aed'], ['CPU %', '#22c55e'], ['Temp °C', '#f59e0b']
];
legend.forEach(([label, color], i) => {{
  ctx.fillStyle = color;
  ctx.fillRect(10 + i * 90, H - 18, 10, 10);
  ctx.fillStyle = '#e2e8f0';
  ctx.font = '10px monospace';
  ctx.fillText(label, 24 + i * 90, H - 9);
}});

// Mode bar
const bar = document.getElementById('mode-bar');
const total = modes.length;
const groups = [];
let cur = modes[0], count = 0;
modes.forEach(m => {{
  if (m === cur) {{ count++; }} else {{
    groups.push([cur, count]); cur = m; count = 1;
  }}
}});
groups.push([cur, count]);
const modeColors = {{ FULL:'#22c55e', QUANTIZED:'#f59e0b', MINIMAL:'#ef4444' }};
groups.forEach(([mode, n]) => {{
  const seg = document.createElement('div');
  seg.className = 'mode-seg';
  seg.style.width = (n / total * 100) + '%';
  seg.style.background = modeColors[mode] || '#64748b';
  if (n / total > 0.08) seg.textContent = mode[0];
  bar.appendChild(seg);
}});
</script>
</body>
</html>"""


def _colour_class(val: float, warn: float, crit: float) -> str:
    return "red" if val >= crit else "yellow" if val >= warn else "green"


def generate_html(readings: List[HardwareReading],
                  analysis: Dict[str, Any]) -> str:

    # Anomaly HTML
    anom_html = ""
    for a in analysis["anomaly_events"][:20]:
        sev = a["severity"]
        anom_html += (
            f'<div class="anomaly {sev}">'
            f'<span class="ts">LSN {a["lsn"]}</span> &nbsp;'
            f'<strong>{a["type"]}</strong> [{sev}] &mdash; {a["narrative"]}'
            f'</div>\n'
        )
    if not anom_html:
        anom_html = '<p style="color:var(--muted)">No anomalies detected this session.</p>'

    # Mode table (sample every N for brevity)
    stride = max(1, len(readings) // 25)
    analyser2 = ContextAnalyser()
    rows = ""
    for i, r in enumerate(readings[::stride]):
        ctx = analyser2.analyse(r)
        pc = {"NOMINAL": "green", "WARN": "yellow", "CRITICAL": "red"}.get(r.pressure, "")
        mc = {"FULL": "green", "QUANTIZED": "yellow", "MINIMAL": "red"}.get(ctx.recommended_mode, "")
        rows += (
            f"<tr><td>{i * stride}</td>"
            f"<td>{r.ram_used_pct:.1f}</td>"
            f"<td>{r.cpu_pct:.1f}</td>"
            f"<td>{r.thermal_c:.1f}</td>"
            f"<td class='{pc}'>{r.pressure}</td>"
            f"<td class='{mc}'>{ctx.recommended_mode}</td></tr>\n"
        )

    # Duration
    dur = analysis["duration_s"]
    if dur < 60:
        dur_str = f"{dur:.0f}s"
    else:
        dur_str = f"{dur/60:.1f} min"

    # Pressure summary
    pc = analysis["pressure_counts"]
    parts = []
    total = sum(pc.values())
    for state in ["NOMINAL", "WARN", "CRITICAL"]:
        n = pc.get(state, 0)
        if n:
            parts.append(f"{state} {n/total*100:.0f}%")
    pressure_str = " · ".join(parts)

    # Readings JSON (downsample for chart)
    stride_js = max(1, len(readings) // 200)
    readings_js = [
        {"lsn": r.lsn, "ram_used_pct": r.ram_used_pct,
         "cpu_pct": r.cpu_pct, "thermal_c": r.thermal_c}
        for r in readings[::stride_js]
    ]

    return HTML_TEMPLATE.format(
        generated_at   = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        n_readings     = analysis["n_readings"],
        duration_str   = dur_str,
        ram_peak       = analysis["ram_peak"],
        ram_mean       = analysis["ram_mean"],
        ram_min        = analysis["ram_min"],
        ram_class      = _colour_class(analysis["ram_peak"], 75, 90),
        cpu_peak       = analysis["cpu_peak"],
        cpu_mean       = analysis["cpu_mean"],
        cpu_class      = _colour_class(analysis["cpu_peak"], 70, 90),
        temp_peak      = analysis["temp_peak"],
        temp_mean      = analysis["temp_mean"],
        temp_class     = _colour_class(analysis["temp_peak"], 70, 85),
        n_anomalies    = analysis["n_anomalies"],
        anom_class     = "red" if analysis["n_anomalies"] > 3 else
                         "yellow" if analysis["n_anomalies"] > 0 else "green",
        pressure_str   = pressure_str,
        anomaly_html   = anom_html,
        mode_table_rows= rows,
        readings_json  = json.dumps(readings_js),
        anomalies_json = json.dumps(analysis["anomaly_events"][:50]),
        modes_json     = json.dumps(analysis["modes"][::stride_js]),
    )


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sentinel Log Visualiser")
    parser.add_argument("--input",  "-i", default=None,
                        help="Path to sentinel_redo.log")
    parser.add_argument("--output", "-o", default=None,
                        help="Output HTML path (default: <input>.html or report.html)")
    parser.add_argument("--demo",   action="store_true",
                        help="Generate a synthetic demo report (no log file needed)")
    parser.add_argument("--open",   action="store_true",
                        help="Open the report in the browser after generating")
    args = parser.parse_args()

    if args.demo:
        print("  Generating synthetic full-cycle session...")
        readings = generate_synthetic_session()
        output   = args.output or "sentinel_demo_report.html"
    elif args.input:
        print(f"  Loading {args.input}...")
        readings = load_log(args.input)
        output   = args.output or (args.input.replace(".log", "_report.html"))
    else:
        parser.print_help()
        print("\n  Tip: use --demo to generate a report without a log file.")
        sys.exit(1)

    if not readings:
        print("  No parseable readings found.")
        sys.exit(1)

    print(f"  Analysing {len(readings)} readings...")
    analysis = analyse_session(readings)

    print(f"  Generating HTML report...")
    html = generate_html(readings, analysis)

    with open(output, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n  ✓ Report: {output}")
    print(f"    Readings : {analysis['n_readings']}")
    print(f"    Duration : {analysis['duration_s']:.0f}s")
    print(f"    Peak RAM : {analysis['ram_peak']:.1f}%")
    print(f"    Peak temp: {analysis['temp_peak']:.1f}°C")
    print(f"    Anomalies: {analysis['n_anomalies']}")
    print(f"    Modes    : {analysis['mode_counts']}")

    if args.open:
        import webbrowser
        webbrowser.open(output)


if __name__ == "__main__":
    main()
