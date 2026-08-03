import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from videocrush_media import (
    build_download_command,
    build_gif_commands,
    build_scene_detect_command,
    build_thumbnail_command,
    build_upscale_command,
    build_whisper_command,
    build_image_optimization_plan,
    parse_scene_times,
)
from videocrush_power import get_battery_status


class MediaTests(unittest.TestCase):
    def test_gif_uses_palette_generation_and_paletteuse(self):
        commands = build_gif_commands(Path("input.mp4"), Path("output.gif"))
        self.assertTrue(any("palettegen" in item for item in commands[0]))
        self.assertTrue(any("paletteuse" in item for item in commands[1]))
        self.assertTrue(commands[0][-1].endswith(".palette.png"))

    def test_command_adapters_are_deterministic(self):
        self.assertIn("scale=480:-2", build_thumbnail_command(Path("in.mp4"), Path("thumb.jpg"), 2.5))
        self.assertTrue(any("showinfo" in item for item in build_scene_detect_command(Path("in.mp4"))))
        self.assertEqual(parse_scene_times("pts_time:1.25\npts_time:4.5"), (1.25, 4.5))
        self.assertEqual(build_download_command("https://example.test/v", Path("out"), ytdlp="yt-dlp")[0], "yt-dlp")
        self.assertIn("--output_format", build_whisper_command(Path("in.mp4"), Path("out"), whisper="whisper"))
        self.assertEqual(build_upscale_command(Path("in.mp4"), Path("out.mp4"), engine="video2x")[0], "video2x")

    def test_image_optimizer_plan_uses_available_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "frame.png"
            source.write_bytes(b"png")
            with patch("videocrush_media.shutil.which", return_value="tool"):
                plan = build_image_optimization_plan([source])
            self.assertEqual(plan[0][0], "pngquant")

    def test_battery_probe_returns_a_status(self):
        status = get_battery_status()
        self.assertIsInstance(status.on_ac_power, bool)


if __name__ == "__main__":
    unittest.main()
