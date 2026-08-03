"""Command-line entry point for VideoCrush."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from videocrush_automation import (
    build_task_scheduler_command,
    discover_watch_files,
    export_presets,
    import_presets,
    install_context_menu,
    parse_watch_rules,
    perform_power_action,
    register_task,
    remove_context_menu,
    route_watch_file,
)

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
from videocrush_distribution import DEFAULT_REPOSITORY, check_for_update


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="videocrush",
        description="Compress one video or a folder of videos with FFmpeg.",
    )
    parser.add_argument("--version", action="version", version=f"VideoCrush {VERSION}")
    parser.add_argument("--input", "-i", help="Input video file or folder.")
    parser.add_argument("--out", "--output", "-o", help="Output folder or exact output file.")
    parser.add_argument("--portable", action="store_true", help="Keep queue state in a local data folder beside the app.")
    parser.add_argument("--workers", type=int, default=4, help="Parallel batch workers (default: 4; use 1 for sequential output).")
    parser.add_argument("--check-update", action="store_true", help="Check the latest public release and exit when no input is given.")
    parser.add_argument("--update-repo", default=DEFAULT_REPOSITORY, help="GitHub owner/repository used by --check-update.")
    parser.add_argument("--preset", default="web-1080p", help="Compression profile.")
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
    parser.add_argument("--subtitles", choices=("passthrough", "strip", "burn-in"), default="passthrough")
    parser.add_argument("--subtitle-file", help="Subtitle file for --subtitles burn-in.")
    parser.add_argument("--subtitle-track", type=int, help="Use only this zero-based subtitle track.")
    parser.add_argument("--downmix", action="store_true", help="Downmix audio to stereo.")
    parser.add_argument("--loudness-normalize", action="store_true", help="Apply EBU R128 loudness normalization.")
    parser.add_argument("--constrained-vbr", action="store_true", help="Apply maxrate/bufsize around quality encoding.")
    parser.add_argument("--max-bitrate", type=int, help="Constrained-VBR maximum video bitrate in kbps.")
    parser.add_argument("--scene-crf", action="store_true", help="Enable AV1 scene-change and delta-Q tuning.")
    parser.add_argument("--pause-on-battery", action="store_true", help="Wait for AC power between encode passes/jobs.")
    parser.add_argument("--quality-report", action="store_true", help="Write SSIM/VMAF/size-delta JSON and thumbnail paths.")
    parser.add_argument("--gif-output", help="Create an optimized palette GIF from --input and exit.")
    parser.add_argument("--gif-fps", type=int, default=12)
    parser.add_argument("--gif-width", type=int, default=480)
    parser.add_argument("--url", help="Download a YouTube/M3U8 URL with yt-dlp before encoding.")
    parser.add_argument("--auto-subtitles", action="store_true", help="Generate SRT subtitles with Whisper and burn them in.")
    parser.add_argument("--whisper-model", default="small")
    parser.add_argument("--scene-detect", action="store_true", help="Emit FFmpeg scene-change timestamps before encoding.")
    parser.add_argument("--upscale-engine", choices=("realesrgan", "video2x"), help="Run an optional AI upscaler pre-pass.")
    parser.add_argument("--upscale-scale", type=int, default=2)
    parser.add_argument("--optimize-images", action="store_true", help="Run available pngquant/jpegoptim/gifsicle sibling optimizers.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running FFmpeg.")
    parser.add_argument("--export-script", help="Write the generated command(s) to a .bat or .sh file.")
    parser.add_argument("--export-presets", help="Export built-in or imported profiles to JSON.")
    parser.add_argument("--import-presets", help="Import profiles from a versioned JSON file.")
    parser.add_argument("--watch", help="Watch a folder and encode newly stable files.")
    parser.add_argument("--watch-rule", action="append", default=[], help="Route extension to profile, e.g. .mov=web-1080p.")
    parser.add_argument("--watch-once", action="store_true", help="Scan a watch folder once and exit.")
    parser.add_argument("--watch-interval", type=float, default=5.0, help="Watch polling interval in seconds.")
    parser.add_argument("--schedule-name", help="Task Scheduler task name to create.")
    parser.add_argument("--schedule", choices=("MINUTE", "HOURLY", "DAILY", "WEEKLY"), help="Task Scheduler cadence.")
    parser.add_argument("--register-schedule", action="store_true", help="Register the generated scheduled task.")
    parser.add_argument("--context-menu", choices=("install", "remove"), help="Install/remove the current-user Explorer action.")
    parser.add_argument("--after-queue", choices=("none", "sleep", "shutdown"), default="none", help="Explicit post-queue power action.")
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
            "subtitle_path": args.subtitle_file,
            "subtitle_track": args.subtitle_track,
            "audio_downmix": args.downmix,
            "loudness_normalize": args.loudness_normalize,
            "constrained_vbr": args.constrained_vbr,
            "max_bitrate_kbps": args.max_bitrate,
            "scene_crf": args.scene_crf,
            "pause_on_battery": args.pause_on_battery,
        }
    )
    return values


def _emit(args: argparse.Namespace, payload: dict) -> None:
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(payload.get("message", json.dumps(payload, sort_keys=True)))


def _process_item(
    args: argparse.Namespace,
    item: Path,
    output: Path,
    preset: str,
    profiles: Dict[str, object],
    multiple: bool,
) -> bool:
    source_item = item
    temporary = None
    try:
        if args.upscale_engine:
            from videocrush_media import upscale_video

            temporary = tempfile.TemporaryDirectory(prefix="videocrush-upscale-")
            item = Path(temporary.name) / source_item.name
            upscale_video(source_item, item, engine=args.upscale_engine, scale=args.upscale_scale)
        if args.scene_detect:
            from videocrush_media import detect_scene_times

            scenes = detect_scene_times(item)
            _emit(args, {"ok": True, "input": str(source_item), "scene_times": scenes, "message": f"Detected {len(scenes)} scene change(s)."})
        overrides = _overrides(args)
        if args.auto_subtitles:
            from videocrush_media import transcribe_subtitles

            subtitle_dir = output / ".videocrush-subtitles"
            subtitle_path = transcribe_subtitles(item, subtitle_dir, model=args.whisper_model)
            overrides.update({"subtitle_mode": "burn-in", "subtitle_path": str(subtitle_path)})
        output_path = output_path_for(
            source_item,
            output,
            output_format=profiles[preset].output_format,
            multiple=multiple,
        )
        settings = settings_from_profile(preset, item, output_path, profiles=profiles, **overrides)
        if args.dry_run or args.export_script:
            from videocrush_core import build_ffmpeg_commands, find_ffmpeg, probe_video

            info = probe_video(item)
            commands = build_ffmpeg_commands(settings, info=info, ffmpeg=find_ffmpeg() or "ffmpeg")
            shell = "cmd" if os.name == "nt" else "sh"
            if args.export_script:
                script_path = Path(args.export_script).expanduser().resolve()
                if multiple:
                    script_path = script_path.with_name(
                        f"{script_path.stem}_{item.stem}{script_path.suffix or ('.bat' if os.name == 'nt' else '.sh')}"
                    )
                script_path.parent.mkdir(parents=True, exist_ok=True)
                script_path.write_text(commands_to_script(commands, shell), encoding="utf-8")
                _emit(args, {"ok": True, "input": str(item), "script": str(script_path), "message": f"Wrote command script: {script_path}"})
            if args.dry_run:
                for command in commands:
                    _emit(args, {"ok": True, "input": str(item), "command": command, "message": " ".join(command)})
                return True

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
        if args.quality_report:
            from videocrush_media import compare_quality, create_thumbnail_strip

            thumbnail_root = result.output_path.parent / f".{result.output_path.stem}-thumbnails"
            before = create_thumbnail_strip(source_item, thumbnail_root / "before")
            after = create_thumbnail_strip(result.output_path, thumbnail_root / "after")
            report = compare_quality(source_item, result.output_path, thumbnails=before + after)
            report_path = result.output_path.with_name(result.output_path.name + ".quality.json")
            report_path.write_text(
                json.dumps(
                    {
                        "reference": str(report.reference),
                        "candidate": str(report.candidate),
                        "reference_size": report.reference_size,
                        "candidate_size": report.candidate_size,
                        "size_delta_percent": round(report.size_delta_percent, 2),
                        "ssim": report.ssim,
                        "vmaf": report.vmaf,
                        "thumbnails": [str(path) for path in report.thumbnails],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            _emit(args, {"ok": True, "quality_report": str(report_path), "message": f"Wrote quality report: {report_path}"})
        if args.optimize_images:
            from videocrush_media import image_siblings, optimize_images

            commands = optimize_images(image_siblings(source_item))
            _emit(args, {"ok": True, "optimized_images": len(commands), "message": f"Optimized {len(commands)} image sibling(s)."})
        return True
    except (VideoCrushError, OSError, ValueError) as exc:
        _emit(args, {"ok": False, "input": str(item), "error": str(exc), "message": f"{item.name}: {exc}"})
        return False
    finally:
        if temporary is not None:
            temporary.cleanup()


def _process_items(
    args: argparse.Namespace,
    jobs: List[Tuple[Path, str]],
    output: Path,
    profiles: Dict[str, object],
    multiple: bool,
) -> List[Tuple[Path, bool]]:
    def process(job: Tuple[Path, str]) -> Tuple[Path, bool]:
        item, preset = job
        return item, _process_item(args, item, output, preset, profiles, multiple)

    if len(jobs) <= 1 or args.workers <= 1:
        return [process(job) for job in jobs]
    worker_count = min(max(1, args.workers), len(jobs))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="videocrush") as executor:
        return list(executor.map(process, jobs))


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.workers < 1:
        _emit(args, {"ok": False, "error": "--workers must be at least 1."})
        return 2
    if args.portable:
        os.environ["VIDEOCRUSH_PORTABLE"] = "1"
    profiles = dict(PRESET_PROFILES)
    try:
        if args.check_update:
            update = check_for_update(repository=args.update_repo)
            _emit(
                args,
                {
                    "ok": True,
                    "current_version": update.current_version,
                    "latest_version": update.latest_version,
                    "update_available": update.update_available,
                    "release_url": update.release_url,
                    "message": (
                        f"VideoCrush {update.current_version} is current."
                        if not update.update_available
                        else f"VideoCrush {update.latest_version} is available: {update.release_url}"
                    ),
                },
            )
            if not args.input and not args.watch and not args.schedule:
                return 0
        if args.import_presets:
            profiles.update(import_presets(Path(args.import_presets).expanduser().resolve()))
        if args.export_presets:
            path = export_presets(Path(args.export_presets).expanduser().resolve(), profiles)
            _emit(args, {"ok": True, "presets": str(path), "message": f"Wrote presets: {path}"})
        if args.context_menu:
            if args.portable:
                _emit(args, {"ok": False, "error": "Portable mode does not write Explorer registry entries."})
                return 2
            if args.context_menu == "install":
                install_context_menu(Path(sys.argv[0]).resolve(), args.preset)
                _emit(args, {"ok": True, "message": "Installed the current-user VideoCrush context-menu action."})
            else:
                remove_context_menu()
                _emit(args, {"ok": True, "message": "Removed the current-user VideoCrush context-menu action."})
            if not args.input and not args.watch and not args.schedule:
                return 0
        if (args.export_presets or args.import_presets) and not args.input and not args.watch and not args.schedule:
            return 0
        if args.gif_output:
            if not args.input:
                _emit(args, {"ok": False, "error": "--gif-output requires --input."})
                return 2
            from videocrush_media import create_gif

            gif_path = create_gif(Path(args.input).expanduser().resolve(), Path(args.gif_output).expanduser().resolve(), fps=args.gif_fps, width=args.gif_width)
            _emit(args, {"ok": True, "output": str(gif_path), "message": f"Created GIF: {gif_path}"})
            return 0
        if args.url:
            if not args.out:
                _emit(args, {"ok": False, "error": "--url requires --out as a download/output folder."})
                return 2
            from videocrush_media import download_url

            downloaded = download_url(args.url, Path(args.out).expanduser().resolve())
            args.input = str(downloaded)
        if args.schedule:
            if not args.input or not args.out or not args.schedule_name:
                _emit(args, {"ok": False, "error": "--schedule requires --schedule-name, --input, and --out."})
                return 2
            command = build_task_scheduler_command(
                args.schedule_name,
                Path(sys.argv[0]).resolve(),
                Path(args.input).expanduser().resolve(),
                Path(args.out).expanduser().resolve(),
                args.preset,
                args.schedule,
            )
            if args.register_schedule:
                register_task(command)
                _emit(args, {"ok": True, "message": f"Registered scheduled task: {args.schedule_name}"})
            else:
                _emit(args, {"ok": True, "command": command, "message": " ".join(command)})
            return 0
        if args.preset not in profiles:
            _emit(args, {"ok": False, "error": f"Unknown preset profile: {args.preset}."})
            return 2
        if args.watch:
            rules = parse_watch_rules(args.watch_rule)
            output = Path(args.out).expanduser().resolve() if args.out else Path(args.watch).resolve() / "compressed"
            output.mkdir(parents=True, exist_ok=True)
            seen: Set[str] = set()
            failures = 0
            while True:
                discovered = discover_watch_files(Path(args.watch), seen, rules, recursive=args.recursive)
                jobs = []
                for item, _ in discovered:
                    preset = route_watch_file(item, rules, args.preset)
                    if preset not in profiles:
                        _emit(args, {"ok": False, "input": str(item), "error": f"Unknown routed preset: {preset}"})
                        failures += 1
                        seen.add(str(item.resolve()).lower())
                        continue
                    jobs.append((item, preset))
                results = _process_items(args, jobs, output, profiles, multiple=True)
                for item, success in results:
                    if success:
                        seen.add(str(item.resolve()).lower())
                    else:
                        failures += 1
                if args.watch_once:
                    if failures == 0 and args.after_queue != "none" and not args.dry_run:
                        perform_power_action(args.after_queue)
                    return 1 if failures else 0
                time.sleep(max(0.5, args.watch_interval))
        if not args.input or not args.out:
            _emit(args, {"ok": False, "error": "--input and --out are required for compression."})
            return 2
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
        results = _process_items(
            args,
            [(item, args.preset) for item in inputs],
            output,
            profiles,
            multiple or input_path.is_dir(),
        )
        failures = sum(not success for _, success in results)
        if failures == 0 and args.after_queue != "none" and not args.dry_run:
            perform_power_action(args.after_queue)
        return 1 if failures else 0
    except (VideoCrushError, OSError, ValueError) as exc:
        _emit(args, {"ok": False, "error": str(exc), "message": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
