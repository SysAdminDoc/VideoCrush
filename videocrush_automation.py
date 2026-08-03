"""Optional automation helpers for VideoCrush.

All integrations are explicit: importing presets changes only the caller's
in-memory registry, context-menu registration is HKCU-scoped, task creation is
opt-in, and power actions are never triggered by merely importing this module.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from videocrush_core import PRESET_PROFILES, PresetProfile, VideoCrushError, collect_input_files


PRESET_SCHEMA_VERSION = 1


def export_presets(path: Path, profiles: Optional[Dict[str, PresetProfile]] = None) -> Path:
    """Write shareable profile JSON using an atomic replace."""

    selected = profiles or PRESET_PROFILES
    payload = {
        "schema_version": PRESET_SCHEMA_VERSION,
        "profiles": {name: asdict(profile) for name, profile in selected.items()},
    }
    payload["profiles"] = {
        name: {**values, "extra_video_args": list(values.get("extra_video_args", ())) }
        for name, values in payload["profiles"].items()
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)
    return target


def import_presets(path: Path) -> Dict[str, PresetProfile]:
    """Load and validate profiles without mutating the built-in registry."""

    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise VideoCrushError(f"Could not read preset file {target}: {exc}") from exc
    if int(payload.get("schema_version", 0)) != PRESET_SCHEMA_VERSION:
        raise VideoCrushError("Unsupported preset file schema version.")
    profiles: Dict[str, PresetProfile] = {}
    for name, values in (payload.get("profiles") or {}).items():
        if not isinstance(values, dict):
            raise VideoCrushError(f"Preset {name} is not an object.")
        values = dict(values)
        values["name"] = str(values.get("name", name))
        values["description"] = str(values.get("description", "Imported VideoCrush profile."))
        values["extra_video_args"] = tuple(str(item) for item in values.get("extra_video_args", ()))
        try:
            profile = PresetProfile(**values)
        except (TypeError, ValueError) as exc:
            raise VideoCrushError(f"Invalid preset {name}: {exc}") from exc
        profiles[str(name)] = profile
    if not profiles:
        raise VideoCrushError("Preset file contains no profiles.")
    return profiles


def merge_presets(path: Path, profiles: Optional[Dict[str, PresetProfile]] = None) -> Dict[str, PresetProfile]:
    merged = dict(profiles or PRESET_PROFILES)
    merged.update(import_presets(path))
    return merged


def parse_watch_rules(values: Iterable[str]) -> Dict[str, str]:
    """Parse rules such as ``.mov=web-1080p`` into a normalized map."""

    rules: Dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise VideoCrushError(f"Watch rule must be EXTENSION=PRESET: {value}")
        extension, preset = value.split("=", 1)
        extension = extension.strip().lower()
        if not extension.startswith("."):
            extension = "." + extension
        preset = preset.strip()
        if not extension or not preset:
            raise VideoCrushError(f"Watch rule must include an extension and preset: {value}")
        rules[extension] = preset
    return rules


def route_watch_file(path: Path, rules: Dict[str, str], default_preset: str) -> str:
    return rules.get(Path(path).suffix.lower(), default_preset)


def discover_watch_files(
    folder: Path,
    seen: Set[str],
    rules: Optional[Dict[str, str]] = None,
    recursive: bool = False,
) -> List[Tuple[Path, Optional[str]]]:
    """Return stable, not-yet-seen videos and their optional routed profiles."""

    results: List[Tuple[Path, Optional[str]]] = []
    for path in collect_input_files(Path(folder), recursive=recursive):
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        try:
            if time.time() - path.stat().st_mtime < 2:
                continue
        except OSError:
            continue
        results.append((path, rules.get(path.suffix.lower()) if rules else None))
    return results


def build_task_scheduler_command(
    task_name: str,
    executable: Path,
    input_path: Path,
    output_path: Path,
    preset: str,
    schedule: str,
) -> List[str]:
    """Build a per-user schtasks command; the caller decides whether to run it."""

    if os.name != "nt":
        raise VideoCrushError("Task Scheduler integration is Windows-only.")
    return [
        "schtasks",
        "/Create",
        "/F",
        "/SC",
        schedule,
        "/TN",
        task_name,
        "/TR",
        f'"{executable}" --input "{input_path}" --out "{output_path}" --preset "{preset}"',
        "/RL",
        "LIMITED",
    ]


def register_task(command: List[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise VideoCrushError((result.stderr or result.stdout or "schtasks failed").strip())


def install_context_menu(executable: Path, preset: str = "web-1080p") -> None:
    """Install a current-user Explorer action without requiring elevation."""

    if os.name != "nt":
        raise VideoCrushError("Explorer context-menu integration is Windows-only.")
    import winreg

    key_path = r"Software\Classes\*\shell\VideoCrush\command"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as command_key:
        command_key.SetValue("", winreg.REG_SZ, f'"{executable}" --input "%1" --out "%~dp1" --preset "{preset}"')


def remove_context_menu() -> None:
    if os.name != "nt":
        raise VideoCrushError("Explorer context-menu integration is Windows-only.")
    import winreg

    shell_key = r"Software\Classes\*\shell\VideoCrush"
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, shell_key + r"\command")
    except FileNotFoundError:
        return
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, shell_key)
    except FileNotFoundError:
        pass


def perform_power_action(action: str) -> None:
    """Perform an explicitly requested post-queue power action."""

    if action not in {"none", "sleep", "shutdown"}:
        raise VideoCrushError(f"Unknown power action: {action}")
    if action == "none":
        return
    if os.name == "nt":
        command = ["shutdown", "/h"] if action == "sleep" else ["shutdown", "/s", "/t", "0"]
    elif action == "sleep":
        command = ["systemctl", "suspend"]
    else:
        command = ["shutdown", "-h", "now"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise VideoCrushError((result.stderr or result.stdout or "power action failed").strip())
