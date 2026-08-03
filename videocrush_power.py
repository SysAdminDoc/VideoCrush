"""Local battery-awareness helpers for long VideoCrush jobs."""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass(frozen=True)
class BatteryStatus:
    on_ac_power: bool
    percent: Optional[int]
    has_battery: bool


def get_battery_status() -> BatteryStatus:
    if os.name == "nt":
        class SystemPowerStatus(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_uint32),
                ("BatteryFullLifeTime", ctypes.c_uint32),
            ]

        status = SystemPowerStatus()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            percent = None if status.BatteryLifePercent == 255 else int(status.BatteryLifePercent)
            has_battery = status.BatteryFlag != 128
            return BatteryStatus(status.ACLineStatus == 1, percent, has_battery)
        return BatteryStatus(True, None, False)

    status_path = Path("/sys/class/power_supply/BAT0/status")
    capacity_path = Path("/sys/class/power_supply/BAT0/capacity")
    if status_path.is_file():
        status = status_path.read_text(encoding="utf-8").strip().lower()
        percent = int(capacity_path.read_text(encoding="utf-8").strip()) if capacity_path.is_file() else None
        return BatteryStatus(status in {"charging", "full"}, percent, True)
    return BatteryStatus(True, None, False)


def wait_for_ac_power(
    cancel_event=None,
    poll_seconds: float = 30.0,
    status_callback: Optional[Callable[[BatteryStatus], None]] = None,
) -> None:
    """Block between encode passes until AC power is available."""

    while True:
        status = get_battery_status()
        if status_callback:
            status_callback(status)
        if status.on_ac_power or not status.has_battery:
            return
        if cancel_event and cancel_event.is_set():
            return
        time.sleep(max(1.0, poll_seconds))
