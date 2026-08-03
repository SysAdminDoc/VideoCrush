import tempfile
import unittest
from pathlib import Path

from videocrush_core import (
    CompressionSettings,
    VideoCrushError,
    build_ffmpeg_commands,
    collect_input_files,
    commands_to_script,
    format_duration,
    format_size,
    output_path_for,
)


class CoreTests(unittest.TestCase):
    def test_formatters(self):
        self.assertEqual(format_size(1024), "1.0 KB")
        self.assertEqual(format_size(1024**2), "1.0 MB")
        self.assertEqual(format_duration(3661), "1:01:01")
        self.assertEqual(format_duration(61), "1:01")

    def test_collect_files_is_sorted_and_recursive_when_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "b.MKV").write_bytes(b"")
            (root / "ignore.txt").write_bytes(b"")
            nested = root / "nested"
            nested.mkdir()
            (nested / "a.mp4").write_bytes(b"")
            self.assertEqual([p.name for p in collect_input_files(root)], ["b.MKV"])
            self.assertEqual([p.name for p in collect_input_files(root, recursive=True)], ["b.MKV", "a.mp4"])

    def test_output_path_supports_exact_file_and_directory_modes(self):
        source = Path("source.mov")
        self.assertEqual(output_path_for(source, Path("out.mp4")), Path("out.mp4"))
        self.assertEqual(output_path_for(source, Path("out"), "mkv"), Path("out/source_compressed.mkv"))

    def test_target_size_builds_two_pass_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            settings = CompressionSettings(source, root / "out.mp4", target_mb=25, mode="target-size")
            info = {"format": {"duration": "10"}, "streams": [{"codec_type": "audio", "bit_rate": "128000"}]}
            commands = build_ffmpeg_commands(settings, info=info, ffmpeg="ffmpeg")
            self.assertEqual(len(commands), 2)
            self.assertIn("-pass", commands[0])
            self.assertIn("1", commands[0])
            self.assertIn("2", commands[1])
            self.assertIn("-b:v", commands[1])

    def test_quality_build_has_crf_and_advanced_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
            source.write_bytes(b"source")
            subtitle = root / "captions.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
            settings = CompressionSettings(
                source,
                root / "out.mkv",
                mode="quality",
                crf=28,
                resolution="720p",
                crop_filter="crop=100:100:0:0",
                hdr_mode="tone-map-sdr",
                subtitle_mode="burn-in",
                subtitle_path=subtitle,
                audio_downmix=True,
                loudness_normalize=True,
            )
            command = build_ffmpeg_commands(settings)[0]
            self.assertIn("-crf", command)
            self.assertIn("28", command)
            self.assertIn("scale=-2:720", command[command.index("-vf") + 1])
            self.assertIn("tonemap", command[command.index("-vf") + 1])
            self.assertIn("subtitles=", command[command.index("-vf") + 1])
            self.assertIn("-ac", command)
            self.assertTrue(any("loudnorm" in item for item in command))

    def test_invalid_burn_in_settings_fail_early(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"source")
            settings = CompressionSettings(source, Path(directory) / "out.mp4", mode="quality", subtitle_mode="burn-in")
            with self.assertRaises(VideoCrushError):
                build_ffmpeg_commands(settings)

    def test_script_rendering_is_shell_specific(self):
        command = [["ffmpeg", "-i", "input file.mp4", "output.mp4"]]
        windows = commands_to_script(command, "cmd")
        shell = commands_to_script(command, "sh")
        self.assertTrue(windows.startswith("@echo off"))
        self.assertIn("if errorlevel 1", windows)
        self.assertTrue(shell.startswith("#!/usr/bin/env sh"))
        self.assertIn("'input file.mp4'", shell)


if __name__ == "__main__":
    unittest.main()
