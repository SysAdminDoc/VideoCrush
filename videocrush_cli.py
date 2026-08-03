"""Command-line entry point for VideoCrush."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from videocrush_core import (
    PRESET_PROFILES,
    VERSION,
    VideoCrushError,
    collect_input_files,
    commands_to_script,
    format_size,
    output_path_for,
    run_compression,
    settings_from_profile,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="videocrush",
        description="Compress one video or a folder of videos with FFmpeg.",
    )
    parser.add_argument("--version", action="version", version=f"VideoCrush {VERSION}")
    parser.add_argument("--input", "-i", required=True, help="Input video file or folder.")
    parser.add_argument("--out", "--output", "-o", required=True, help="Output folder or exact output file.")
    parser.add_argument("--preset", choices=sorted(PRESET_PROFILES), default="web-1080p", help="Compression profile.")
    parser.add_argument("--recursive", action="store_true", help="Recurse into subfolders when input is a folder.")
    parser.add_argument("--extensions", help="Comma-separated extension filter, such as mp4,mkv.")
    parser.add_argument("--mode", choices=("target-size", "quality"), help="Override the profile's encoding mode.")
    parser.add_argument("--target-mb", type=float, help="Target output size in megabytes.")
    parser.add_argument("--crf", type=float, help="Override quality mode CRF/CQ value.")
    parser.add_argument("--codec", help="FFmpeg video encoder name, for example libx264 or h264_nvenc.")
    parser.add_argument("--encode-preset", help="FFmpeg encoder preset, for example medium or fast.")
    parser.add_argument("--resolution", help="Output height, such as 1080p, or Original.")
    parser.add_argument("--audio", choices=("aac", "libopus", "copy", "an"), help="Audio encoder or an to strip audio.")
    parser.add_argument("--audio-bitrate", type=int, help="Audio bitrate in kbps.")
    parser.add_argument("--no-two-pass", action="store_true", help="Disable two-pass target-size encoding.")
    parser.add_argument("--crop", choices=("none", "auto"), default="none", help="Detect and remove letterboxing.")
    parser.add_argument("--hdr", choices=("passthrough", "tone-map-sdr"), default="passthrough")
    parser.add_argument("--subtitles", choices=("passthrough", "strip"), default="passthrough")
    parser.add_argument("--downmix", action="store_true", help="Downmix audio to stereo.")
    parser.add_argument("--loudness-normalize", action="store_true", help="Apply EBU R128 loudness normalization.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running FFmpeg.")
    parser.add_argument("--export-script", help="Write the generated command(s) to a .bat or .sh file.")
    parser.add_argument("--json", action="store_true", help="Emit one machine-readable result object per input.")
    return parser


def _overrides(args: argparse.Namespace) -> dict:
    values = {}
    for key, attr in (
        ("video_codec", "codec"),
        ("encode_preset", "encode_preset"),
        ("resolution", "resolution"),
        ("audio_codec", "audio"),
        ("audio_bitrate", "audio_bitrate"),
        ("mode", "mode"),
        ("target_mb", "target_mb"),
        ("crf", "crf"),
    ):
        value = getattr(args, attr)
        if value is not None:
            values[key] = value
    if args.no_two_pass:
        values["two_pass"] = False
    values.update(
        {
            "crop_mode": args.crop,
            "hdr_mode": args.hdr,
            "subtitle_mode": args.subtitles,
            "audio_downmix": args.downmix,
            "loudness_normalize": args.loudness_normalize,
        }
    )
    return values


def _emit(args: argparse.Namespace, payload: dict) -> None:
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(payload.get("message", json.dumps(payload, sort_keys=True)))


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    input_path = Path(args.input).expanduser().resolve()
    output = Path(args.out).expanduser().resolve()
    extensions = args.extensions.split(",") if args.extensions else None
    inputs = collect_input_files(input_path, recursive=args.recursive, extensions=extensions)
    if not inputs:
        _emit(args, {"ok": False, "error": f"No supported video files found in {input_path}."})
        return 2
    multiple = len(inputs) > 1
    if multiple or input_path.is_dir():
        output.mkdir(parents=True, exist_ok=True)
    overrides = _overrides(args)
    failures = 0
    for item in inputs:
        output_path = output_path_for(
            item,
            output,
            output_format=PRESET_PROFILES[args.preset].output_format,
            multiple=multiple or input_path.is_dir(),
        )
        settings = settings_from_profile(args.preset, item, output_path, **overrides)
        try:
            if args.dry_run or args.export_script:
                from videocrush_core import build_ffmpeg_commands, find_ffmpeg, probe_video

                info = probe_video(item)
                commands = build_ffmpeg_commands(settings, info=info, ffmpeg=find_ffmpeg() or "ffmpeg")
                shell = "cmd" if os.name == "nt" else "sh"
                if args.export_script:
                    script_path = Path(args.export_script).expanduser().resolve()
                    if multiple:
                        script_path = script_path.with_name(f"{script_path.stem}_{item.stem}{script_path.suffix or ('.bat' if os.name == 'nt' else '.sh')}")
                    script_path.parent.mkdir(parents=True, exist_ok=True)
                    script_path.write_text(commands_to_script(commands, shell), encoding="utf-8")
                    _emit(args, {"ok": True, "input": str(item), "script": str(script_path), "message": f"Wrote command script: {script_path}"})
                if args.dry_run:
                    for command in commands:
                        _emit(args, {"ok": True, "input": str(item), "command": command, "message": " ".join(command)})
                if args.dry_run:
                    continue
            def progress(value: int) -> None:
                if not args.json:
                    print(f"\r{item.name}: {value:3d}%", end="", file=sys.stderr, flush=True)

            result = run_compression(
                settings,
                progress_callback=progress,
                log_callback=(lambda message: print(message, file=sys.stderr)) if not args.json else None,
            )
            if not args.json:
                print(file=sys.stderr)
            _emit(
                args,
                {
                    "ok": True,
                    "input": str(result.input_path),
                    "output": str(result.output_path),
                    "input_size": result.input_size,
                    "output_size": result.output_size,
                    "saved_percent": round(result.saved_percent, 2),
                    "message": f"{result.input_path.name} -> {result.output_path} ({format_size(result.output_size)}, saved {result.saved_percent:.1f}%)",
                },
            )
        except (VideoCrushError, OSError, ValueError) as exc:
            failures += 1
            _emit(args, {"ok": False, "input": str(item), "error": str(exc), "message": f"{item.name}: {exc}"})
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
