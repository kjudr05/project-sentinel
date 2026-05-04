"""
samsung_metrics.py
──────────────────
Project Sentinel — Samsung Galaxy Hardware Reader

Provides Samsung-specific metric paths for Galaxy devices running Android
or a Samsung Linux userspace (Galaxy Book / DeX mode). Augments the
generic sysfs reader in the C++ engine with Galaxy-specific telemetry.

This module is used by the bridge when running natively on a Galaxy device.
On non-Samsung hardware it gracefully falls back to the generic reader.

Samsung-specific sources:
  - Exynos thermal zones (zone IDs differ from generic ARM)
  - Samsung battery health sysfs (cycle count, health status)
  - GPU load via /sys/devices/platform/mali (Exynos) or adreno (Snapdragon)
  - Display brightness as a proxy for user activity level
  - Knox security state (for enterprise adaptive policy)

References:
  - Samsung Open Source (https://opensource.samsung.com)
  - Exynos 2400 TRM (internal, not public)
  - Android Power HAL sysfs interface
"""

import os
import re
import time
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, field


# ─── Device Detection ─────────────────────────────────────────────────────────

def detect_samsung_device() -> Dict[str, str]:
    """
    Detect which Samsung device we're running on using Android props or
    DMI data on Linux. Returns a dict with 'model', 'soc', and 'profile'.
    """
    info = {"model": "unknown", "soc": "unknown", "profile": "generic"}

    # Android: read build properties
    if Path("/system/build.prop").exists():
        try:
            props = Path("/system/build.prop").read_text(errors="ignore")
            model_m = re.search(r"ro\.product\.model=(.+)", props)
            soc_m   = re.search(r"ro\.hardware=(.+)", props)
            if model_m:
                info["model"] = model_m.group(1).strip()
            if soc_m:
                info["soc"] = soc_m.group(1).strip()
        except Exception:
            pass

    # Linux: read DMI
    for dmi_path in ["/sys/class/dmi/id/product_name",
                     "/sys/class/dmi/id/board_vendor"]:
        try:
            val = Path(dmi_path).read_text().strip()
            if "samsung" in val.lower() or "galaxy" in val.lower():
                info["model"] = val
                break
        except Exception:
            pass

    # Map model to profile
    model_lower = info["model"].lower()
    if "s24" in model_lower or "s23" in model_lower:
        info["profile"] = "galaxy_s"
    elif "book" in model_lower:
        info["profile"] = "galaxy_book"
    elif "tab" in model_lower:
        info["profile"] = "galaxy_tab"
    elif "fold" in model_lower or "flip" in model_lower:
        info["profile"] = "galaxy_fold"

    return info


# ─── Exynos Thermal Zones ─────────────────────────────────────────────────────

# Galaxy S-series Exynos 2400 thermal zone mapping
EXYNOS_THERMAL_ZONES = {
    "galaxy_s": {
        "cpu_big":   "thermal_zone3",   # Cortex-X4 cluster
        "cpu_mid":   "thermal_zone2",   # Cortex-A720 cluster
        "cpu_little":"thermal_zone1",   # Cortex-A520 cluster
        "gpu":       "thermal_zone7",   # Xclipse 940
        "npu":       "thermal_zone9",   # Samsung NPU (on-device AI)
        "battery":   "thermal_zone12",
        "skin":      "thermal_zone15",  # Surface temperature sensor
    },
    "galaxy_book": {
        "cpu":       "thermal_zone0",
        "gpu":       "thermal_zone1",
        "battery":   "thermal_zone5",
    },
    "generic": {
        "cpu":       "thermal_zone0",
    }
}


def read_samsung_thermal(profile: str = "generic") -> Dict[str, Optional[float]]:
    """Read all available thermal zones for a Samsung device profile."""
    zones   = EXYNOS_THERMAL_ZONES.get(profile, EXYNOS_THERMAL_ZONES["generic"])
    results = {}
    base    = Path("/sys/class/thermal")

    for name, zone in zones.items():
        try:
            val = (base / zone / "temp").read_text().strip()
            results[name] = int(val) / 1000.0  # milli-Celsius → Celsius
        except Exception:
            results[name] = None

    return results


def get_skin_temperature(profile: str = "generic") -> Optional[float]:
    """
    Returns the device surface temperature — most relevant for user experience.
    Falls back to CPU core temp if skin sensor unavailable.
    """
    zones   = read_samsung_thermal(profile)
    skin    = zones.get("skin")
    if skin is not None:
        return skin
    # Fallback: max of available CPU temps
    cpu_temps = [v for k, v in zones.items() if "cpu" in k and v is not None]
    return max(cpu_temps) if cpu_temps else None


# ─── Samsung Battery Health ───────────────────────────────────────────────────

@dataclass
class BatteryHealth:
    capacity_pct:   float = 0.0
    cycle_count:    int   = 0
    health_status:  str   = "unknown"   # Good | Overheat | Dead | Over voltage
    charge_full:    int   = 0           # µAh — design capacity
    charge_now:     int   = 0           # µAh — current charge
    voltage_mv:     float = 0.0
    current_ma:     float = 0.0         # + = charging, - = discharging
    temp_c:         float = 0.0
    charging:       bool  = False
    # Derived
    estimated_minutes_remaining: int = -1

    @property
    def is_healthy(self) -> bool:
        return self.health_status in ("Good", "unknown") and self.capacity_pct > 80

    @property
    def degradation_pct(self) -> float:
        """How much design capacity has been lost (0 = new, 20 = 20% worn)."""
        if self.charge_full <= 0:
            return 0.0
        # This would require charge_full_design; approximation:
        return max(0.0, (1 - self.capacity_pct / 100.0) * 20)


def read_battery_health(supply: str = "battery") -> BatteryHealth:
    """
    Read full Samsung battery health from Android power_supply sysfs.
    Samsung Galaxy devices expose richer data than the Android standard.
    """
    base = Path(f"/sys/class/power_supply/{supply}")
    h    = BatteryHealth()

    if not base.exists():
        # Try common alternates
        for alt in ["BAT0", "BAT1", "main-battery"]:
            if Path(f"/sys/class/power_supply/{alt}").exists():
                base = Path(f"/sys/class/power_supply/{alt}")
                break
        else:
            return h

    def _read(name: str, default=None):
        try:
            return (base / name).read_text().strip()
        except Exception:
            return default

    # Standard fields
    cap_str = _read("capacity")
    if cap_str:
        try:
            h.capacity_pct = float(cap_str)
        except ValueError:
            pass

    status = _read("status", "Unknown")
    h.charging = status in ("Charging", "Full")

    health_raw = _read("health", "Good")
    h.health_status = health_raw

    # Samsung-specific extensions
    cycle_raw = _read("cycle_count")
    if cycle_raw:
        try:
            h.cycle_count = int(cycle_raw)
        except ValueError:
            pass

    charge_full_raw = _read("charge_full")
    if charge_full_raw:
        try:
            h.charge_full = int(charge_full_raw)
        except ValueError:
            pass

    charge_now_raw = _read("charge_now")
    if charge_now_raw:
        try:
            h.charge_now = int(charge_now_raw)
        except ValueError:
            pass

    voltage_raw = _read("voltage_now")
    if voltage_raw:
        try:
            h.voltage_mv = int(voltage_raw) / 1000.0
        except ValueError:
            pass

    current_raw = _read("current_now")
    if current_raw:
        try:
            # µA → mA, Samsung convention: positive=discharging
            raw_ma = int(current_raw) / 1000.0
            h.current_ma = -raw_ma if h.charging else raw_ma
        except ValueError:
            pass

    temp_raw = _read("temp")
    if temp_raw:
        try:
            h.temp_c = int(temp_raw) / 10.0  # Samsung reports in 0.1°C
        except ValueError:
            pass

    # Estimate time remaining
    if h.current_ma > 1 and not h.charging and h.charge_full > 0:
        hours = (h.charge_now / 1000.0) / h.current_ma  # charge in mAh / drain rate
        h.estimated_minutes_remaining = int(hours * 60)

    return h


# ─── Samsung GPU Load ─────────────────────────────────────────────────────────

def read_gpu_load(soc: str = "exynos") -> Optional[float]:
    """
    Read GPU utilisation from sysfs.
    Exynos: Mali Xclipse / Mali-G series
    Snapdragon: Adreno
    """
    # Mali (Exynos)
    mali_paths = [
        "/sys/devices/platform/mali/utilisation",
        "/sys/devices/platform/23000000.mali/utilisation",
        "/sys/class/misc/mali0/device/utilisation",
    ]
    for p in mali_paths:
        try:
            val = Path(p).read_text().strip()
            return float(val)
        except Exception:
            pass

    # Adreno (Snapdragon Galaxy devices)
    adreno_paths = [
        "/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage",
        "/sys/class/devfreq/kgsl-3d0/cur_freq",
    ]
    for p in adreno_paths:
        try:
            val = Path(p).read_text().strip()
            # cur_freq is a frequency, not a percentage — skip for now
            if "busy" in p:
                return float(val.replace("%", ""))
        except Exception:
            pass

    return None  # Not available (containerised / non-GPU host)


# ─── Samsung NPU (Neural Processing Unit) ─────────────────────────────────────

def read_npu_load() -> Optional[float]:
    """
    Read Samsung NPU utilisation (Exynos 2400's dedicated AI accelerator).
    The NPU runs on-device ML models independently of the CPU.
    When the NPU is busy, the AI agent should offload to it rather than
    competing on CPU.
    """
    npu_paths = [
        "/sys/class/npu/npu0/utilization",
        "/sys/devices/platform/npu/utilization",
        "/sys/kernel/debug/npu/stats",
    ]
    for p in npu_paths:
        try:
            val = Path(p).read_text().strip()
            return float(val.split()[0])
        except Exception:
            pass
    return None


# ─── Display / User Activity ──────────────────────────────────────────────────

def read_display_brightness() -> Optional[int]:
    """
    Current display brightness (0–255 or 0–1023 depending on panel).
    High brightness correlates with active use — Sentinel can use this
    to distinguish an idle screen (safe to use full resources) from
    active use (user is waiting for AI response, latency matters).
    """
    paths = [
        "/sys/class/backlight/panel0-backlight/brightness",
        "/sys/class/backlight/lcd-backlight/brightness",
        "/sys/class/leds/lcd-backlight/brightness",
    ]
    for p in paths:
        try:
            return int(Path(p).read_text().strip())
        except Exception:
            pass
    return None


# ─── Knox Security State ─────────────────────────────────────────────────────

def read_knox_state() -> Dict[str, Any]:
    """
    Read Samsung Knox security state. In enterprise deployments, Knox
    warranties and policies affect which AI capabilities are permitted.
    A Knox-compromised device should restrict AI to MINIMAL mode.
    """
    state = {
        "knox_version":   None,
        "warranty_void":  False,   # True if device has been rooted/modified
        "mdm_active":     False,   # True if managed by enterprise MDM
        "drk_state":      "unknown",  # Device Root Key state
    }

    # Knox warranty bit (exposed via sysfs on Samsung devices)
    knox_paths = {
        "warranty_void": "/sys/class/sec/sec_afc/drk",
        "knox_version":  "/sys/module/knox_kap/version",
    }
    for key, path in knox_paths.items():
        try:
            val = Path(path).read_text().strip()
            if key == "warranty_void":
                state["warranty_void"] = val != "0"
            else:
                state[key] = val
        except Exception:
            pass

    return state


# ─── Composite Samsung Snapshot ───────────────────────────────────────────────

@dataclass
class SamsungMetrics:
    thermal:     Dict[str, Optional[float]] = field(default_factory=dict)
    battery:     BatteryHealth              = field(default_factory=BatteryHealth)
    gpu_load:    Optional[float]            = None
    npu_load:    Optional[float]            = None
    brightness:  Optional[int]             = None
    knox:        Dict[str, Any]             = field(default_factory=dict)
    profile:     str                        = "generic"
    available:   bool                       = False

    def to_context_str(self) -> str:
        """
        Produce a human-readable context string for injection into the
        AI system prompt, extending the standard hardware context.
        """
        parts = []

        if self.gpu_load is not None:
            parts.append(f"GPU: {self.gpu_load:.0f}%")

        if self.npu_load is not None:
            parts.append(f"NPU (AI accelerator): {self.npu_load:.0f}%")
        else:
            parts.append("NPU: available (idle or unmonitored)")

        skin = self.thermal.get("skin")
        if skin is not None:
            parts.append(f"Surface temp: {skin:.1f}°C")

        if self.battery.cycle_count > 0:
            parts.append(f"Battery cycles: {self.battery.cycle_count}")
            if self.battery.cycle_count > 400:
                parts.append("⚠ Battery aged — capacity may be reduced")

        if self.battery.estimated_minutes_remaining > 0:
            parts.append(
                f"Est. battery life: {self.battery.estimated_minutes_remaining} min"
            )

        if self.knox.get("warranty_void"):
            parts.append("⚠ Knox warranty void — running in degraded security mode")

        if self.knox.get("mdm_active"):
            parts.append("Knox MDM active — enterprise policy in effect")

        if not parts:
            return "(Samsung-specific telemetry not available on this host)"

        return "\n".join(f"  {p}" for p in parts)


def read_all(profile: str = "auto") -> SamsungMetrics:
    """
    Read all available Samsung metrics. Gracefully handles missing sysfs
    paths (returns None fields) so this is safe on non-Samsung hardware.
    """
    if profile == "auto":
        info    = detect_samsung_device()
        profile = info.get("profile", "generic")
        soc     = info.get("soc", "unknown")
    else:
        soc = "unknown"

    m = SamsungMetrics(profile=profile)

    m.thermal    = read_samsung_thermal(profile)
    m.battery    = read_battery_health()
    m.gpu_load   = read_gpu_load(soc)
    m.npu_load   = read_npu_load()
    m.brightness = read_display_brightness()
    m.knox       = read_knox_state()

    # Mark as available if at least thermal data came through
    m.available  = any(v is not None for v in m.thermal.values())

    return m


# ─── Self-test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Samsung Metrics Reader — Self Test")
    print("───────────────────────────────────")

    device = detect_samsung_device()
    print(f"Device:  {device['model']} ({device['profile']})")
    print(f"SoC:     {device['soc']}")
    print()

    m = read_all()
    print(f"Profile:     {m.profile}")
    print(f"Available:   {m.available}")
    print(f"GPU load:    {m.gpu_load}")
    print(f"NPU load:    {m.npu_load}")
    print(f"Brightness:  {m.brightness}")
    print(f"Battery:")
    print(f"  Capacity:  {m.battery.capacity_pct}%")
    print(f"  Cycles:    {m.battery.cycle_count}")
    print(f"  Health:    {m.battery.health_status}")
    print(f"  Charging:  {m.battery.charging}")
    print(f"  Est. mins: {m.battery.estimated_minutes_remaining}")
    print(f"Thermal zones:")
    for k, v in m.thermal.items():
        print(f"  {k}: {v}°C")
    print()
    print("Context string:")
    print(m.to_context_str())
