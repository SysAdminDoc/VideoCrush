import os
import tempfile
import time
import unittest
from pathlib import Path

from videocrush_automation import (
    discover_watch_files,
    export_presets,
    import_presets,
    parse_watch_rules,
    perform_power_action,
    route_watch_file,
)
from videocrush_core import PRESET_PROFILES


class AutomationTests(unittest.TestCase):
    def test_preset_exchange_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            export_presets(path, {"web-1080p": PRESET_PROFILES["web-1080p"]})
            profiles = import_presets(path)
            self.assertEqual(profiles["web-1080p"].resolution, "1080p")
            self.assertEqual(profiles["web-1080p"].extra_video_args, ())

    def test_watch_rules_normalize_extensions_and_route(self):
        rules = parse_watch_rules(["mov=web-720p", ".mkv=archive-av1"])
        self.assertEqual(rules, {".mov": "web-720p", ".mkv": "archive-av1"})
        self.assertEqual(route_watch_file(Path("clip.MOV"), rules, "web-1080p"), "web-720p")
        self.assertEqual(route_watch_file(Path("clip.mp4"), rules, "web-1080p"), "web-1080p")

    def test_watch_discovery_ignores_recent_and_seen_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            old_file = folder / "old.mp4"
            new_file = folder / "new.mp4"
            old_file.write_bytes(b"old")
            new_file.write_bytes(b"new")
            old_time = time.time() - 10
            os.utime(old_file, (old_time, old_time))
            found = discover_watch_files(folder, set())
            self.assertEqual([path.name for path, _ in found], ["old.mp4"])
            found_again = discover_watch_files(folder, {str(old_file.resolve()).lower()})
            self.assertEqual(found_again, [])

    def test_none_power_action_is_safe_noop(self):
        perform_power_action("none")


if __name__ == "__main__":
    unittest.main()
