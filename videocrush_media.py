"""Optional media-analysis and utility workflows for VideoCrush."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from videocrush_core import VideoCrushError, find_ffmpeg, media_duration, probe_video


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _run(
    command: Sequence[str],
    timeout: int = 600,
    cwd: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            creationflags=_creation_flags(),
            timeout=timeout,
            check=False,
            cwd=str(cwd) if cwd else None,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VideoCrushError(f"Could not run {command[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise VideoCrushError(f"{Path(command[0]).name} failed with exit code {result.returncode}: {detail[-500:]}")
    return result


@dataclass(frozen=True)
class QualityReport:
    reference: Path
    candidate: Path
    reference_size: int
    candidate_size: int
    size_delta_percent: float
    ssim: Optional[float]
    vmaf: Optional[float]
    thumbnails: Tuple[Path, ...] = ()


def build_thumbnail_command(input_path: Path, output_path: Path, timestamp: float, width: int = 480) -> List[str]:
    return [
        find_ffmpeg() or "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, timestamp):.3f}",
        "-i",
        str(input_path),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        "-y",
        str(output_path),
    ]


def create_thumbnail_strip(
    input_path: Path,
    output_dir: Path,
    duration: Optional[float] = None,
    count: int = 5,
) -> Tuple[Path, ...]:
    info = probe_video(input_path)
    duration = duration if duration is not None else media_duration(info)
    if duration <= 0:
        raise VideoCrushError("Could not determine duration for thumbnail strip.")
    output_dir.mkdir(parents=True, exist_ok=True)
    count = max(1, count)
    # Sampling exactly at the reported duration can land after the final
    # decoded frame and make FFmpeg produce no image. Keep the last sample
    # inside the stream while retaining an even spread across the clip.
    times = [0.0] if count == 1 else [duration * index / count for index in range(count)]
    paths: List[Path] = []
    for index, timestamp in enumerate(times):
        path = output_dir / f"thumb-{index:02d}.jpg"
        _run(build_thumbnail_command(input_path, path, timestamp))
        paths.append(path)
    return tuple(paths)


def _parse_ssim(output: str) -> Optional[float]:
    matches = re.findall(r"All:([0-9.]+)", output)
    return float(matches[-1]) if matches else None


def _vmaf_available(ffmpeg: str) -> bool:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        creationflags=_creation_flags(),
        timeout=30,
        check=False,
    )
    return "libvmaf" in result.stdout


def _compute_ssim(reference: Path, candidate: Path, ffmpeg: str) -> Optional[float]:
    result = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-i",
            str(reference),
            "-i",
            str(candidate),
            "-lavfi",
            "[0:v][1:v]ssim=stats_file=-",
            "-f",
            "null",
            "-",
        ]
    )
    return _parse_ssim(result.stderr + result.stdout)


def _compute_vmaf(reference: Path, candidate: Path, ffmpeg: str) -> Optional[float]:
    if not _vmaf_available(ffmpeg):
        return None
    handle, name = tempfile.mkstemp(prefix="videocrush-vmaf-", suffix=".json")
    os.close(handle)
    log_path = Path(name)
    try:
        _run(
            [
                ffmpeg,
                "-hide_banner",
                "-i",
                str(reference),
                "-i",
                str(candidate),
                "-lavfi",
                f"[0:v][1:v]libvmaf=log_fmt=json:log_path={log_path.name}",
                "-f",
                "null",
                "-",
            ],
            cwd=log_path.parent,
        )
        payload = json.loads(log_path.read_text(encoding="utf-8"))
        return float(payload.get("pooled_metrics", {}).get("vmaf", {}).get("mean"))
    except (OSError, ValueError, TypeError, KeyError, VideoCrushError):
        return None
    finally:
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            pass


def compare_quality(reference: Path, candidate: Path, thumbnails: Iterable[Path] = ()) -> QualityReport:
    reference = Path(reference)
    candidate = Path(candidate)
    if not reference.is_file() or not candidate.is_file():
        raise VideoCrushError("Both reference and candidate files are required for quality comparison.")
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise VideoCrushError("FFmpeg was not found for quality comparison.")
    reference_size = reference.stat().st_size
    candidate_size = candidate.stat().st_size
    delta = (candidate_size / reference_size - 1) * 100 if reference_size else 0.0
    return QualityReport(
        reference,
        candidate,
        reference_size,
        candidate_size,
        delta,
        _compute_ssim(reference, candidate, ffmpeg),
        _compute_vmaf(reference, candidate, ffmpeg),
        tuple(thumbnails),
    )


def build_gif_commands(input_path: Path, output_path: Path, fps: int = 12, width: int = 480) -> List[List[str]]:
    palette = output_path.with_suffix(".palette.png")
    ffmpeg = find_ffmpeg() or "ffmpeg"
    return [
        [
            ffmpeg,
            "-hide_banner",
            "-i",
            str(input_path),
            "-vf",
            f"fps={fps},scale={width}:-1:flags=lanczos,palettegen",
            "-y",
            str(palette),
        ],
        [
            ffmpeg,
            "-hide_banner",
            "-i",
            str(input_path),
            "-i",
            str(palette),
            "-lavfi",
            f"fps={fps},scale={width}:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=sierra2_4a",
            "-y",
            str(output_path),
        ],
    ]


def create_gif(input_path: Path, output_path: Path, fps: int = 12, width: int = 480) -> Path:
    commands = build_gif_commands(input_path, output_path, fps=fps, width=width)
    try:
        for command in commands:
            _run(command)
        return Path(output_path)
    finally:
        commands[0][-1] and Path(commands[0][-1]).unlink(missing_ok=True)


def image_siblings(video_path: Path) -> Tuple[Path, ...]:
    video_path = Path(video_path)
    patterns = (f"{video_path.stem}*.png", f"{video_path.stem}*.jpg", f"{video_path.stem}*.jpeg", f"{video_path.stem}*.gif")
    paths = {path for pattern in patterns for path in video_path.parent.glob(pattern) if path != video_path}
    return tuple(sorted(paths, key=lambda path: str(path).lower()))


def build_image_optimization_plan(paths: Iterable[Path], output_dir: Optional[Path] = None) -> List[List[str]]:
    plan: List[List[str]] = []
    for source in paths:
        source = Path(source)
        destination = (Path(output_dir) if output_dir else source.parent) / source.name
        if source.suffix.lower() == ".png" and shutil.which("pngquant"):
            plan.append(["pngquant", "--force", "--output", str(destination), "--", str(source)])
        elif source.suffix.lower() in {".jpg", ".jpeg"} and shutil.which("jpegoptim"):
            plan.append(["jpegoptim", "--strip-all", "--dest", str(destination.parent), str(source)])
        elif source.suffix.lower() == ".gif" and shutil.which("gifsicle"):
            plan.append(["gifsicle", "--batch", "--optimize=3", "--output", str(destination), str(source)])
    return plan


def optimize_images(paths: Iterable[Path], output_dir: Optional[Path] = None) -> Tuple[Tuple[str, ...], ...]:
    commands = build_image_optimization_plan(paths, output_dir=output_dir)
    for command in commands:
        _run(command)
    return tuple(tuple(command) for command in commands)


def build_download_command(url: str, output_dir: Path, ytdlp: Optional[str] = None) -> List[str]:
    binary = ytdlp or shutil.which("yt-dlp")
    if not binary:
        raise VideoCrushError("yt-dlp is required for URL/M3U8 capture but was not found.")
    return [binary, "--no-progress", "--merge-output-format", "mp4", "-o", str(Path(output_dir) / "%(title)s.%(ext)s"), url]


def download_url(url: str, output_dir: Path, ytdlp: Optional[str] = None) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _run(build_download_command(url, output_dir, ytdlp=ytdlp), timeout=3600)
    candidates = sorted(output_dir.glob("*"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise VideoCrushError("yt-dlp completed without producing a file.")
    return candidates[0]


def build_whisper_command(input_path: Path, output_dir: Path, model: str = "small", whisper: Optional[str] = None) -> List[str]:
    binary = whisper or shutil.which("whisper")
    if not binary:
        raise VideoCrushError("Whisper CLI is required for automatic subtitles but was not found.")
    return [binary, str(input_path), "--model", model, "--output_dir", str(output_dir), "--output_format", "srt"]


def transcribe_subtitles(input_path: Path, output_dir: Path, model: str = "small", whisper: Optional[str] = None) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _run(build_whisper_command(input_path, output_dir, model=model, whisper=whisper), timeout=7200)
    subtitle = output_dir / (Path(input_path).stem + ".srt")
    if not subtitle.is_file():
        raise VideoCrushError("Whisper completed without producing an SRT file.")
    return subtitle


def build_scene_detect_command(input_path: Path, threshold: float = 0.4) -> List[str]:
    return [
        find_ffmpeg() or "ffmpeg",
        "-hide_banner",
        "-i",
        str(input_path),
        "-vf",
        f"select='gt(scene,{threshold})',showinfo",
        "-f",
        "null",
        "-",
    ]


def parse_scene_times(output: str) -> Tuple[float, ...]:
    return tuple(float(value) for value in re.findall(r"pts_time:([0-9.]+)", output))


def detect_scene_times(input_path: Path, threshold: float = 0.4) -> Tuple[float, ...]:
    result = _run(build_scene_detect_command(input_path, threshold=threshold))
    return parse_scene_times(result.stderr + result.stdout)


def build_upscale_command(input_path: Path, output_path: Path, engine: str = "realesrgan", scale: int = 2) -> List[str]:
    if engine == "realesrgan":
        binary = shutil.which("realesrgan-ncnn-vulkan") or "realesrgan-ncnn-vulkan"
        return [binary, "-i", str(input_path), "-o", str(output_path), "-s", str(scale)]
    if engine == "video2x":
        binary = shutil.which("video2x") or "video2x"
        return [binary, "-i", str(input_path), "-o", str(output_path), "-r", str(scale)]
    raise VideoCrushError(f"Unknown upscaling engine: {engine}")


def upscale_video(input_path: Path, output_path: Path, engine: str = "realesrgan", scale: int = 2) -> Path:
    command = build_upscale_command(input_path, output_path, engine=engine, scale=scale)
    if not shutil.which(command[0]):
        raise VideoCrushError(f"Upscaling engine not found: {command[0]}")
    _run(command, timeout=7200)
    return Path(output_path)
