"""
replay.py
─────────
Project Sentinel — Session Replay Tool

Replays a previously recorded sentinel_redo.log at configurable speed,
writing entries to a fresh log file that the bridge and agent watch in
real time. This lets you demo Sentinel without live hardware — including
pre-scripted "crisis" scenarios that would be impossible to trigger on
demand during a hackathon presentation.

Usage:
  # Replay a real captured log at 2× speed
  python scripts/replay.py --input logs/sentinel_redo.log --speed 2

  # Replay a built-in scenario (nominal → warn → critical → recovery)
  python scripts/replay.py --scenario full_cycle

  # Write to custom output log (for bridge/agent to watch)
  python scripts/replay.py --scenario crisis --output /tmp/demo.log

Built-in scenarios:
  nominal     — healthy device, stable RAM, low CPU
  warn        — gradual RAM rise, moderate heat
  crisis      — RAM spike → CRITICAL, thermal throttle, battery low
  recovery    — starts critical, then resources free up
  full_cycle  — nominal → warn → crisis → recovery (full demo arc)
"""

import sys
import os
import re
import time
import argparse
import random
from pathlib import Path
from typing import List, Tuple
from datetime import datetime

# ─── Synthetic scenario generators ───────────────────────────────────────────

def _make_line(lsn: int, ts_ms: int, ram: float, cpu: float,
               temp: float, bat: float, charging: bool,
               disk: float, state: str, trend: str, delta: float) -> str:
    chg = "CHG" if charging else "DC"
    return (
        f"LSN:{lsn} | TS:{ts_ms} | RAM_USED:{ram:.1f}% | "
        f"RAM_FREE_MB:{max(0, (100-ram)*60):.1f} | CPU:{cpu:.1f}% | "
        f"THERMAL_C:{temp:.1f} | BAT:{bat:.1f}({chg}) | DISK:{disk:.1f}% | "
        f"STATE:{state} | TREND:{trend} | DELTA:{delta:+.1f}%"
    )


def _pressure(ram: float, temp: float, bat: float, charging: bool) -> str:
    if ram >= 90 or temp >= 85 or (bat < 10 and not charging):
        return "CRITICAL"
    if ram >= 75 or temp >= 70 or (bat < 20 and not charging):
        return "WARN"
    return "NOMINAL"


def _trend(deltas: List[float]) -> str:
    if len(deltas) < 2:
        return "STABLE"
    slope = sum(deltas[-5:]) / len(deltas[-5:])
    if slope >  0.4:
        return "RISING"
    if slope < -0.4:
        return "FALLING"
    return "STABLE"


class ScenarioBuilder:
    """Builds synthetic log lines for a named scenario."""

    def __init__(self, name: str):
        self.name = name
        self.lsn  = 1_000_000
        self.ts   = int(time.time() * 1000)
        self.deltas: List[float] = []

    def _next(self, ram, cpu, temp, bat, charging=False, disk=55.0) -> str:
        self.lsn += 1
        self.ts  += 500
        prev_ram  = float(self.deltas[-1]) if self.deltas else ram
        delta     = ram - prev_ram
        self.deltas.append(delta)
        state = _pressure(ram, temp, bat, charging)
        trend = _trend(self.deltas)
        return _make_line(self.lsn, self.ts, ram, cpu, temp,
                          bat, charging, disk, state, trend, delta)

    def _jitter(self, val: float, spread: float = 1.5) -> float:
        return max(0.0, min(100.0, val + random.uniform(-spread, spread)))

    def nominal(self, n: int = 20) -> List[Tuple[str, float]]:
        """Healthy device — steady RAM, low CPU, cool."""
        lines = []
        for _ in range(n):
            lines.append((self._next(
                ram=self._jitter(32, 1.5), cpu=self._jitter(15, 3),
                temp=self._jitter(46, 1), bat=self._jitter(85, 0.2),
                charging=True, disk=55.0
            ), 0.5))
        return lines

    def warn(self, n: int = 20) -> List[Tuple[str, float]]:
        """RAM creeping up, device warm, battery on discharge."""
        lines = []
        for i in range(n):
            ram  = 55 + i * 1.2
            temp = 60 + i * 0.6
            bat  = 65 - i * 0.3
            lines.append((self._next(
                ram=self._jitter(ram, 1), cpu=self._jitter(50+i, 4),
                temp=self._jitter(temp, 1.5), bat=self._jitter(bat, 0.2),
                charging=False, disk=55.0
            ), 0.5))
        return lines

    def crisis(self, n: int = 20) -> List[Tuple[str, float]]:
        """RAM spike, thermal limit, battery critical."""
        lines = []
        for i in range(n):
            ram  = min(95, 78 + i * 1.0)
            temp = min(90, 73 + i * 0.9)
            bat  = max(5,  40 - i * 1.8)
            lines.append((self._next(
                ram=self._jitter(ram, 0.8), cpu=self._jitter(82+i*0.5, 3),
                temp=self._jitter(temp, 1), bat=self._jitter(bat, 0.2),
                charging=False, disk=55.0
            ), 0.5))
        return lines

    def recovery(self, n: int = 20) -> List[Tuple[str, float]]:
        """App closed, RAM freed, temp dropping."""
        lines = []
        for i in range(n):
            ram  = max(30, 92 - i * 3.2)
            temp = max(45, 87 - i * 2.1)
            bat  = 38 + i * 0.1  # barely recovering (plugged in)
            lines.append((self._next(
                ram=self._jitter(ram, 1), cpu=self._jitter(max(10, 88 - i*4), 4),
                temp=self._jitter(temp, 1.5), bat=self._jitter(bat, 0.1),
                charging=True, disk=55.0
            ), 0.5))
        return lines

    def full_cycle(self) -> List[Tuple[str, float]]:
        """Complete arc: nominal → warn → crisis → recovery."""
        lines  = self.nominal(12)
        lines += self.warn(15)
        lines += self.crisis(18)
        lines += self.recovery(15)
        return lines


SCENARIOS = {
    "nominal":    lambda b: b.nominal(30),
    "warn":       lambda b: b.warn(30),
    "crisis":     lambda b: b.crisis(30),
    "recovery":   lambda b: b.recovery(30),
    "full_cycle": lambda b: b.full_cycle(),
}

# ─── Replay engine ────────────────────────────────────────────────────────────

def replay_file(input_path: str, output_path: str, speed: float):
    """Replay a real captured log, preserving original timestamps."""
    lines = Path(input_path).read_text().splitlines()
    ts_re = re.compile(r"TS:(\d+)")

    valid = []
    for line in lines:
        m = ts_re.search(line)
        if m:
            valid.append((int(m.group(1)), line))

    if not valid:
        print(f"  No parseable lines in {input_path}")
        return

    print(f"  Replaying {len(valid)} lines from {input_path}")
    print(f"  Speed: {speed}×  →  {output_path}\n")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    prev_ts = valid[0][0]
    with open(output_path, "w") as out:
        for ts, line in valid:
            gap_ms = (ts - prev_ts) / speed
            if gap_ms > 0:
                time.sleep(gap_ms / 1000.0)
            out.write(line + "\n")
            out.flush()
            prev_ts = ts

            # Print progress bar
            ram_m = re.search(r"RAM_USED:([\d.]+)%", line)
            state_m = re.search(r"STATE:(\w+)", line)
            if ram_m and state_m:
                ram   = float(ram_m.group(1))
                state = state_m.group(1)
                bar   = "█" * int(ram / 5) + "░" * (20 - int(ram / 5))
                c = {"NOMINAL": "\033[32m", "WARN": "\033[33m",
                     "CRITICAL": "\033[31m"}.get(state, "")
                print(f"\r  RAM [{bar}] {c}{ram:.0f}% {state}\033[0m  ",
                      end="", flush=True)

    print(f"\n\n  Replay complete.")


def replay_scenario(scenario_name: str, output_path: str, speed: float):
    """Replay a built-in synthetic scenario."""
    if scenario_name not in SCENARIOS:
        print(f"  Unknown scenario: {scenario_name}")
        print(f"  Available: {', '.join(SCENARIOS)}")
        sys.exit(1)

    builder = ScenarioBuilder(scenario_name)
    entries = SCENARIOS[scenario_name](builder)

    print(f"  Scenario: {scenario_name.upper()}  ({len(entries)} samples)")
    print(f"  Speed: {speed}×  →  {output_path}\n")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as out:
        for line, interval_s in entries:
            out.write(line + "\n")
            out.flush()

            # Visualise
            ram_m   = re.search(r"RAM_USED:([\d.]+)%", line)
            state_m = re.search(r"STATE:(\w+)", line)
            mode_m  = re.search(r"TREND:(\w+)", line)
            if ram_m and state_m:
                ram   = float(ram_m.group(1))
                state = state_m.group(1)
                trend = mode_m.group(1) if mode_m else "?"
                bar   = "█" * int(ram / 5) + "░" * (20 - int(ram / 5))
                c = {"NOMINAL":"\033[32m","WARN":"\033[33m","CRITICAL":"\033[31m"}.get(state,"")
                print(f"\r  RAM [{bar}] {c}{ram:5.1f}% {state:<10}\033[0m trend:{trend}  ",
                      end="", flush=True)

            time.sleep(interval_s / speed)

    print(f"\n\n  Scenario replay complete.")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sentinel Session Replay",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/replay.py --scenario full_cycle
  python scripts/replay.py --scenario crisis --speed 3
  python scripts/replay.py --input logs/sentinel_redo.log --speed 2
  python scripts/replay.py --scenario nominal --output /tmp/sentinel_demo.log
        """
    )
    parser.add_argument("--scenario", choices=list(SCENARIOS),
                        help="Built-in scenario to replay")
    parser.add_argument("--input",    help="Real log file to replay")
    parser.add_argument("--output",   default="../logs/sentinel_redo.log",
                        help="Output log path (default: ../logs/sentinel_redo.log)")
    parser.add_argument("--speed",    type=float, default=1.0,
                        help="Playback speed multiplier (default: 1.0)")
    parser.add_argument("--list",     action="store_true",
                        help="List available scenarios and exit")
    args = parser.parse_args()

    if args.list:
        print("  Available scenarios:")
        descs = {
            "nominal":   "Healthy device, stable RAM, low CPU",
            "warn":      "Gradual RAM rise, moderate heat, battery draining",
            "crisis":    "RAM spike → CRITICAL, thermal throttle, battery low",
            "recovery":  "Resources freed, temp dropping, device stabilising",
            "full_cycle":"Complete arc: nominal → warn → crisis → recovery",
        }
        for name, desc in descs.items():
            print(f"    {name:<12} {desc}")
        return

    if not args.scenario and not args.input:
        print("  Specify --scenario or --input. Use --list to see scenarios.")
        parser.print_help()
        sys.exit(1)

    if args.scenario:
        replay_scenario(args.scenario, args.output, args.speed)
    else:
        replay_file(args.input, args.output, args.speed)


if __name__ == "__main__":
    main()
