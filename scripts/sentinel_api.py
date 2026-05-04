"""
sentinel_api.py
───────────────
Project Sentinel — REST API Server

Exposes hardware context over HTTP so any app on the device — a Samsung
Good Lock module, a DeX dashboard, a Knox-managed enterprise app — can
query Sentinel without embedding the bridge logic itself.

Endpoints:
  GET  /status          → current hardware context (JSON)
  GET  /metrics         → raw latest reading (JSON)
  GET  /mode            → just the AI mode string
  GET  /history?n=N     → last N readings
  GET  /health          → server heartbeat
  POST /simulate        → inject a fake reading (testing/demo)
  GET  /stream          → Server-Sent Events stream (live push)

No framework dependencies — uses Python's built-in http.server so the
footprint is zero beyond the standard library.

Run: python scripts/sentinel_api.py [--port 7474] [--log PATH] [--mock SCENARIO]
"""

import sys
import os
import json
import time
import threading
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from collections import deque
from typing import Optional
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))
sys.path.insert(0, str(Path(__file__).parent.parent / "agent"))
from sentinel_bridge import SentinelBridge, HardwareContext, ContextAnalyser
from sentinel_agent import _mock_context

# ─── Shared state (thread-safe) ───────────────────────────────────────────────

class SentinelAPIState:
    def __init__(self, history_size: int = 120):
        self._lock     = threading.Lock()
        self._latest:  Optional[HardwareContext] = None
        self._history: deque = deque(maxlen=history_size)
        self._sse_clients: list = []
        self.start_time = time.time()
        self.total_readings = 0

    def ingest(self, ctx: HardwareContext):
        with self._lock:
            self._latest = ctx
            self._history.append(ctx)
            self.total_readings += 1

        # Push to SSE clients
        payload = _ctx_to_dict(ctx)
        data    = "data: " + json.dumps(payload) + "\n\n"
        dead    = []
        for client in self._sse_clients:
            try:
                client["wfile"].write(data.encode())
                client["wfile"].flush()
            except Exception:
                dead.append(client)
        for d in dead:
            try:
                self._sse_clients.remove(d)
            except ValueError:
                pass

    def register_sse(self, client_info: dict):
        with self._lock:
            self._sse_clients.append(client_info)

    def unregister_sse(self, client_info: dict):
        with self._lock:
            try:
                self._sse_clients.remove(client_info)
            except ValueError:
                pass

    @property
    def latest(self) -> Optional[HardwareContext]:
        with self._lock:
            return self._latest

    def history(self, n: int = 20):
        with self._lock:
            items = list(self._history)[-n:]
        return [_ctx_to_dict(c) for c in items]


# ─── Serialisers ──────────────────────────────────────────────────────────────

def _ctx_to_dict(ctx: HardwareContext) -> dict:
    r = ctx.reading
    return {
        "lsn":              r.lsn,
        "timestamp_ms":     r.timestamp_ms,
        "time":             r.ts,
        "ram_used_pct":     round(r.ram_used_pct, 1),
        "ram_free_mb":      round(r.ram_free_mb, 1),
        "cpu_pct":          round(r.cpu_pct, 1),
        "thermal_c":        round(r.thermal_c, 1),
        "battery_pct":      round(r.battery_pct, 1),
        "charging":         r.charging,
        "disk_pct":         round(r.disk_pct, 1),
        "pressure":         r.pressure,
        "trend":            r.trend,
        "ram_delta_pct":    round(r.ram_delta_pct, 2),
        "ai_mode":          ctx.recommended_mode,
        "reasoning_depth":  ctx.reasoning_depth,
        "token_budget":     ctx.token_budget,
        "alert_flags":      ctx.alert_flags,
        "forecast":         ctx.forecast,
    }


# ─── Request Handler ──────────────────────────────────────────────────────────

class SentinelHandler(BaseHTTPRequestHandler):
    state: "SentinelAPIState" = None  # injected at server creation

    def log_message(self, fmt, *args):
        # Quieter logging — only log non-GET or errors
        if args and str(args[1]) not in ("200", "304"):
            super().log_message(fmt, *args)

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type",  "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _no_data(self):
        self._send_json({"error": "No hardware data yet — is the engine running?"}, 503)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        qs     = parse_qs(parsed.query)

        # ── /health ──────────────────────────────────────────────────────────
        if path == "/health":
            self._send_json({
                "status":          "ok",
                "uptime_sec":      round(time.time() - self.state.start_time, 1),
                "total_readings":  self.state.total_readings,
                "has_data":        self.state.latest is not None,
            })

        # ── /status ──────────────────────────────────────────────────────────
        elif path == "/status":
            ctx = self.state.latest
            if not ctx:
                return self._no_data()
            self._send_json(_ctx_to_dict(ctx))

        # ── /metrics ─────────────────────────────────────────────────────────
        elif path == "/metrics":
            ctx = self.state.latest
            if not ctx:
                return self._no_data()
            r = ctx.reading
            self._send_json({
                "ram_used_pct": round(r.ram_used_pct, 1),
                "cpu_pct":      round(r.cpu_pct, 1),
                "thermal_c":    round(r.thermal_c, 1),
                "battery_pct":  round(r.battery_pct, 1),
                "disk_pct":     round(r.disk_pct, 1),
            })

        # ── /mode ────────────────────────────────────────────────────────────
        elif path == "/mode":
            ctx = self.state.latest
            if not ctx:
                return self._no_data()
            self._send_json({
                "mode":          ctx.recommended_mode,
                "token_budget":  ctx.token_budget,
                "pressure":      ctx.reading.pressure,
                "alert_flags":   ctx.alert_flags,
            })

        # ── /history ─────────────────────────────────────────────────────────
        elif path == "/history":
            n = int(qs.get("n", ["20"])[0])
            n = max(1, min(n, 120))
            self._send_json({
                "count":   n,
                "history": self.state.history(n),
            })

        # ── /stream (Server-Sent Events) ─────────────────────────────────────
        elif path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type",  "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            # Send current state immediately if available
            ctx = self.state.latest
            if ctx:
                line = "data: " + json.dumps(_ctx_to_dict(ctx)) + "\n\n"
                try:
                    self.wfile.write(line.encode())
                    self.wfile.flush()
                except Exception:
                    return

            client = {"wfile": self.wfile}
            self.state.register_sse(client)
            try:
                # Keep connection alive — bridge pushes updates
                while True:
                    time.sleep(1.0)
                    # Heartbeat comment to detect dead connections
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                self.state.unregister_sse(client)

        # ── /anomalies ───────────────────────────────────────────────────────
        elif path == "/anomalies":
            ctx = self.state.latest
            if not ctx:
                return self._no_data()
            anoms = getattr(ctx, "anomalies", []) or []
            self._send_json({
                "count": len(anoms),
                "anomalies": [
                    {
                        "type":      a.type.value,
                        "severity":  a.severity.value,
                        "metric":    a.metric,
                        "value":     a.value,
                        "baseline":  a.baseline,
                        "narrative": a.narrative,
                        "ai_hint":   a.ai_hint,
                    }
                    for a in anoms
                ],
            })

        # ── /predict ─────────────────────────────────────────────────────────
        elif path == "/predict":
            h = self.state.history(30)
            if len(h) < 5:
                return self._send_json({"error": "Not enough history for prediction (need 5+ readings)"}, 400)
            import statistics as _st
            rams = [r["ram_used_pct"] for r in h]
            cpus = [r["cpu_pct"]      for r in h]
            temps= [r["thermal_c"]    for r in h]
            def _slope(vals):
                n = len(vals)
                xs = list(range(n))
                mx, my = _st.mean(xs), _st.mean(vals)
                num   = sum((x-mx)*(y-my) for x,y in zip(xs,vals))
                denom = sum((x-mx)**2 for x in xs)
                return num/denom if denom>1e-9 else 0.0
            horizon = 6  # samples ahead (3s at 500ms)
            self._send_json({
                "horizon_samples":  horizon,
                "horizon_seconds":  horizon * 0.5,
                "ram_now":          round(rams[-1], 1),
                "ram_forecast":     round(min(100, max(0, rams[-1] + _slope(rams)*horizon)), 1),
                "cpu_now":          round(cpus[-1], 1),
                "cpu_forecast":     round(min(100, max(0, cpus[-1] + _slope(cpus)*horizon)), 1),
                "temp_now":         round(temps[-1], 1),
                "temp_forecast":    round(min(120, max(0, temps[-1] + _slope(temps)*horizon)), 1),
                "ram_slope_per_sample": round(_slope(rams), 3),
                "will_hit_critical":    (rams[-1] + _slope(rams)*horizon) >= 90,
            })

        # ── /summary ─────────────────────────────────────────────────────────
        elif path == "/summary":
            ctx = self.state.latest
            h   = self.state.history(120)
            if not ctx or not h:
                return self._no_data()
            import statistics as _st
            rams  = [r["ram_used_pct"] for r in h]
            cpus  = [r["cpu_pct"]      for r in h]
            temps = [r["thermal_c"]    for r in h]
            modes = [r["ai_mode"]      for r in h]
            pressures = [r["pressure"] for r in h]
            mode_counts = {m: modes.count(m) for m in set(modes)}
            pressure_counts = {p: pressures.count(p) for p in set(pressures)}
            self._send_json({
                "uptime_sec":       round(time.time() - self.state.start_time, 1),
                "total_readings":   self.state.total_readings,
                "window_size":      len(h),
                "ram": {
                    "current": round(rams[-1], 1),
                    "mean":    round(_st.mean(rams), 1),
                    "peak":    round(max(rams), 1),
                    "min":     round(min(rams), 1),
                },
                "cpu": {
                    "current": round(cpus[-1], 1),
                    "mean":    round(_st.mean(cpus), 1),
                    "peak":    round(max(cpus), 1),
                },
                "thermal": {
                    "current": round(temps[-1], 1),
                    "mean":    round(_st.mean(temps), 1),
                    "peak":    round(max(temps), 1),
                },
                "mode_distribution":     mode_counts,
                "pressure_distribution": pressure_counts,
                "current_mode":    ctx.recommended_mode,
                "current_pressure":ctx.reading.pressure,
                "alert_flags":     ctx.alert_flags,
            })

        # ── /docs ────────────────────────────────────────────────────────────
        elif path in ("/", "/docs"):
            self._send_text(API_DOCS)

        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")

        # ── /simulate ────────────────────────────────────────────────────────
        if path == "/simulate":
            length  = int(self.headers.get("Content-Length", 0))
            body    = self.rfile.read(length)
            try:
                data     = json.loads(body)
                scenario = data.get("scenario", "nominal")
                if scenario not in ("nominal", "warn", "critical"):
                    raise ValueError(f"Unknown scenario: {scenario}")
                ctx = _mock_context(scenario)
                self.state.ingest(ctx)
                self._send_json({"ok": True, "injected": scenario,
                                 "mode": ctx.recommended_mode})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        else:
            self._send_json({"error": "Unknown POST endpoint"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


API_DOCS = """\
Project Sentinel — REST API
═══════════════════════════

GET  /health          Server heartbeat + uptime
GET  /status          Full hardware context (JSON)
GET  /metrics         Raw metric numbers only
GET  /mode            AI operating mode + flags
GET  /history?n=N     Last N readings (max 120)
GET  /anomalies       Current anomaly events (type, severity, narrative)
GET  /predict         30-second resource forecast (linear regression)
GET  /summary         Session statistics (peaks, means, distributions)
GET  /stream          Server-Sent Events live stream
GET  /docs            This help text

POST /simulate        Inject mock reading for demo/testing
                      Body: {"scenario": "nominal"|"warn"|"critical"}

All responses include CORS headers (Access-Control-Allow-Origin: *).
"""


# ─── Server Builder ───────────────────────────────────────────────────────────

def make_server(port: int, state: SentinelAPIState) -> HTTPServer:
    """Create an HTTPServer with the shared state injected into the handler."""
    handler = type("Handler", (SentinelHandler,), {"state": state})
    server  = HTTPServer(("0.0.0.0", port), handler)
    server.timeout = 1.0
    return server


# ─── Entry Point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sentinel REST API Server")
    parser.add_argument("--port",  type=int, default=7474)
    parser.add_argument("--log",   default="../logs/sentinel_redo.log")
    parser.add_argument("--mock",  choices=["nominal", "warn", "critical"],
                        help="Simulate hardware instead of reading live log")
    args = parser.parse_args()

    state = SentinelAPIState()

    if args.mock:
        def _mock_feeder():
            import random
            while True:
                ctx = _mock_context(args.mock)
                ctx.reading.ram_used_pct += random.uniform(-2, 2)
                ctx.reading.cpu_pct      += random.uniform(-4, 4)
                ctx.reading.ram_used_pct  = max(0, min(100, ctx.reading.ram_used_pct))
                ctx.reading.cpu_pct       = max(0, min(100, ctx.reading.cpu_pct))
                state.ingest(ctx)
                time.sleep(0.5)
        threading.Thread(target=_mock_feeder, daemon=True).start()
        print(f"  Mock mode: {args.mock}")
    else:
        bridge = SentinelBridge(args.log, state.ingest)
        bridge.start()
        print(f"  Watching: {args.log}")

    server = make_server(args.port, state)

    print(f"\n  Sentinel API Server")
    print(f"  ───────────────────")
    print(f"  http://localhost:{args.port}/status")
    print(f"  http://localhost:{args.port}/mode")
    print(f"  http://localhost:{args.port}/stream  (SSE)")
    print(f"  http://localhost:{args.port}/docs")
    print(f"\n  Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
