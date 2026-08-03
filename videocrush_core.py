"""Testable FFmpeg core for VideoCrush.

The desktop UI is intentionally kept out of this module.  The same settings,
command construction, probing, and process runner are used by the CLI and can
be exercised without creating a window.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import time
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple


VERSION = "0.1.0"

VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".wmv",
        ".flv",
        ".webm",
        ".m4v",
        ".mpg",
        ".mpeg",
        ".ts",
        ".vob",
        ".3gp",
    }
)

ENCODERS = {
    "H.264 (libx264)": "libx264",
    "H.265 (libx265)": "libx265",
    "VP9": "libvpx-vp9",
    "AV1 (SVT)": "libsvtav1",
    "H.264 (NVENC)": "h264_nvenc",
    "H.265 (NVENC)": "hevc_nvenc",
    "AV1 (NVENC)": "av1_nvenc",
    "H.264 (AMF)": "h264_amf",
    "H.265 (AMF)": "hevc_amf",
    "AV1 (AMF)": "av1_amf",
    "H.264 (QSV)": "h264_qsv",
    "H.265 (QSV)": "hevc_qsv",
    "AV1 (QSV)": "av1_qsv",
    "H.264 (VideoToolbox)": "h264_videotoolbox",
    "H.265 (VideoToolbox)": "hevc_videotoolbox",
}

VIDEO_CODECS = ENCODERS
AUDIO_CODECS = {"aac": "aac", "opus": "libopus", "copy": "copy", "none": "an"}


class VideoCrushError(RuntimeError):
    """An expected, user-actionable failure from the encoding core."""


@dataclass(frozen=True)
class PresetProfile:
    """A shareable compression profile."""

    name: str
    description: str
    video_codec: str = "libx264"
    encode_preset: str = "medium"
    resolution: str = "Original"
    mode: str = "quality"
    target_mb: Optional[float] = None
    crf: float = 23.0
    audio_codec: str = "aac"
    audio_bitrate: int = 128
    output_format: str = "mp4"
    two_pass: bool = True


PRESET_PROFILES: Dict[str, PresetProfile] = {
    "web-1080p": PresetProfile(
        "web-1080p",
        "H.264 web video capped at 1080p.",
        resolution="1080p",
        crf=23,
    ),
    "web-720p": PresetProfile(
        "web-720p",
        "H.264 web video capped at 720p.",
        resolution="720p",
        crf=24,
    ),
    "email-10mb": PresetProfile(
        "email-10mb",
        "Target a file below 10 MB with automatic bitrate allocation.",
        mode="target-size",
        target_mb=10,
        resolution="720p",
        audio_bitrate=96,
    ),
    "archive-av1": PresetProfile(
        "archive-av1",
        "Slow AV1 quality encode for archival storage.",
        video_codec="libsvtav1",
        encode_preset="6",
        mode="quality",
        crf=30,
        audio_codec="libopus",
        audio_bitrate=128,
        output_format="mkv",
        two_pass=False,
    ),
    "smartphone-h264": PresetProfile(
        "smartphone-h264",
        "Baseline-compatible H.264 output for older mobile devices.",
        video_codec="libx264",
        encode_preset="fast",
        mode="quality",
        crf=23,
        audio_codec="aac",
        audio_bitrate=128,
    ),
    "lossless": PresetProfile(
        "lossless",
        "Lossless FFV1 video in an MKV container.",
        video_codec="ffv1",
        encode_preset="medium",
        mode="quality",
        crf=0,
        audio_codec="copy",
        audio_bitrate=128,
        output_format="mkv",
        two_pass=False,
    ),
}


@dataclass
class CompressionSettings:
    input_path: Path
    output_path: Path
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    encode_preset: str = "medium"
    resolution: str = "Original"
    audio_bitrate: int = 128
    mode: str = "target-size"
    target_mb: Optional[float] = 25.0
    crf: float = 23.0
    two_pass: bool = True
    crop_mode: str = "none"
    crop_filter: Optional[str] = None
    hdr_mode: str = "passthrough"
    subtitle_mode: str = "passthrough"
    subtitle_path: Optional[Path] = None
    audio_downmix: bool = False
    loudness_normalize: bool = False
    output_format: Optional[str] = None
    extra_video_args: List[str] = field(default_factory=list)

    def normalized(self) -> "CompressionSettings":
        """Return validated settings with string paths and canonical modes."""

        mode = self.mode.lower().replace("_", "-")
        if mode not in {"target-size", "quality"}:
            raise VideoCrushError("Mode must be 'target-size' or 'quality'.")
        if mode == "target-size" and (self.target_mb is None or self.target_mb <= 0):
            raise VideoCrushError("Target-size mode requires a positive target size.")
        if self.crf < 0 or self.crf > 63:
            raise VideoCrushError("CRF must be between 0 and 63.")
        if self.audio_bitrate < 0:
            raise VideoCrushError("Audio bitrate cannot be negative.")
        if self.subtitle_mode not in {"passthrough", "strip", "burn-in"}:
            raise VideoCrushError("Subtitle mode must be passthrough, strip, or burn-in.")
        if self.subtitle_mode == "burn-in" and not self.subtitle_path:
            raise VideoCrushError("Burn-in subtitle mode requires a subtitle file.")
        if self.hdr_mode not in {"passthrough", "tone-map-sdr"}:
            raise VideoCrushError("HDR mode must be passthrough or tone-map-sdr.")
        if self.crop_mode not in {"none", "auto", "manual"}:
            raise VideoCrushError("Crop mode must be none, auto, or manual.")
        return replace(
            self,
            input_path=Path(self.input_path),
            output_path=Path(self.output_path),
            mode=mode,
        )


@dataclass(frozen=True)
class CompressionResult:
    input_path: Path
    output_path: Path
    input_size: int
    output_size: int
    saved_percent: float
    duration: float
    commands: Tuple[Tuple[str, ...], ...]


def find_binary(name: str) -> Optional[str]:
    """Find an executable in PATH and common Windows FFmpeg locations."""

    candidates = [name]
    if os.name == "nt" and not name.lower().endswith(".exe"):
        candidates.append(name + ".exe")
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return path
    if name in {"ffmpeg", "ffprobe"}:
        common = [
            Path(r"C:\ffmpeg\bin") / name,
            Path(r"C:\Program Files\ffmpeg\bin") / name,
            Path("/usr/bin") / name,
            Path("/usr/local/bin") / name,
        ]
        for path in common:
            candidate = path.with_suffix(".exe") if os.name == "nt" else path
            if candidate.is_file():
                return str(candidate)
    return None


def find_ffmpeg() -> Optional[str]:
    return find_binary("ffmpeg")


def find_ffprobe() -> Optional[str]:
    probe = find_binary("ffprobe")
    if probe:
        return probe
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        candidate = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if candidate.is_file():
            return str(candidate)
    return None


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _suspend_process(process: subprocess.Popen) -> bool:
    """Suspend a running FFmpeg process without creating a console window."""

    try:
        if os.name == "nt":
            import ctypes

            status = ctypes.windll.ntdll.NtSuspendProcess(process._handle)
            return status == 0
        os.kill(process.pid, getattr(__import__("signal"), "SIGSTOP"))
        return True
    except (AttributeError, OSError, TypeError):
        return False


def _resume_process(process: subprocess.Popen) -> bool:
    try:
        if os.name == "nt":
            import ctypes

            status = ctypes.windll.ntdll.NtResumeProcess(process._handle)
            return status == 0
        os.kill(process.pid, getattr(__import__("signal"), "SIGCONT"))
        return True
    except (AttributeError, OSError, TypeError):
        return False


def probe_video(filepath: Path, ffprobe: Optional[str] = None) -> Optional[dict]:
    """Return ffprobe JSON for a media file, or None when it cannot be read."""

    probe = ffprobe or find_ffprobe()
    if not probe:
        return None
    command = [
        probe,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(filepath),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_creation_flags(),
            check=False,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def media_duration(info: Optional[dict]) -> float:
    if not info:
        return 0.0
    try:
        return float(info.get("format", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def format_size(bytes_value: int) -> str:
    if bytes_value < 1024:
        return f"{bytes_value} B"
    if bytes_value < 1024**2:
        return f"{bytes_value / 1024:.1f} KB"
    if bytes_value < 1024**3:
        return f"{bytes_value / 1024**2:.1f} MB"
    return f"{bytes_value / 1024**3:.2f} GB"


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _audio_bitrate(info: dict, settings: CompressionSettings) -> int:
    if settings.audio_codec == "an":
        return 0
    if settings.audio_codec != "copy":
        return settings.audio_bitrate
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "audio":
            try:
                return max(1, int(stream.get("bit_rate", 128000)) // 1000)
            except (TypeError, ValueError):
                break
    return 128


def calculate_video_bitrate(info: dict, settings: CompressionSettings) -> int:
    """Calculate target video bitrate, reserving a small muxing margin."""

    normalized = settings.normalized()
    if normalized.mode != "target-size":
        raise VideoCrushError("Video bitrate is only calculated in target-size mode.")
    duration = media_duration(info)
    if duration <= 0:
        raise VideoCrushError("Could not determine video duration.")
    audio_bits = _audio_bitrate(info, normalized) * 1000 * duration
    target_bits = float(normalized.target_mb) * 8 * 1024 * 1024 * 0.98
    video_kbps = int((target_bits - audio_bits) / duration / 1000)
    if video_kbps < 50:
        raise VideoCrushError(
            f"Calculated video bitrate ({video_kbps} kbps) is too low; increase target size."
        )
    return video_kbps


def _escape_subtitle_path(path: Path) -> str:
    """Escape a subtitle path for FFmpeg's subtitles filter."""

    value = str(path.resolve()).replace("\\", "/")
    if len(value) > 1 and value[1] == ":":
        value = value[0] + "\\:" + value[2:]
    return value.replace("'", "\\'")


def _filter_chain(settings: CompressionSettings) -> Optional[str]:
    filters: List[str] = []
    if settings.crop_filter:
        filters.append(settings.crop_filter)
    if settings.resolution and settings.resolution != "Original":
        try:
            height = int(str(settings.resolution).lower().replace("p", ""))
        except ValueError as exc:
            raise VideoCrushError(f"Invalid resolution: {settings.resolution}") from exc
        filters.append(f"scale=-2:{height}")
    if settings.hdr_mode == "tone-map-sdr":
        filters.append(
            "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=mobius,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
        )
    if settings.subtitle_mode == "burn-in":
        if not settings.subtitle_path:
            raise VideoCrushError("Burn-in subtitle mode requires a subtitle file.")
        filters.append(f"subtitles='{_escape_subtitle_path(settings.subtitle_path)}'")
    return ",".join(filters) if filters else None


def _is_hardware_encoder(codec: str) -> bool:
    return any(codec.endswith(suffix) for suffix in ("_nvenc", "_amf", "_qsv", "_videotoolbox"))


def _encoder_quality_args(codec: str, crf: float) -> List[str]:
    if codec.endswith("_nvenc"):
        return ["-rc", "vbr", "-cq", str(int(crf)), "-b:v", "0"]
    if codec.endswith("_amf"):
        return ["-rc", "cqp", "-qp_i", str(int(crf)), "-qp_p", str(int(crf))]
    if codec.endswith("_qsv"):
        return ["-global_quality", str(int(crf))]
    if codec.endswith("_videotoolbox"):
        return ["-q:v", str(int(crf))]
    return ["-crf", str(crf).rstrip("0").rstrip(".")]


def _codec_args(settings: CompressionSettings, video_kbps: Optional[int]) -> List[str]:
    args = ["-c:v", settings.video_codec]
    if settings.mode == "target-size":
        if video_kbps is None:
            raise VideoCrushError("A target-size encode requires a calculated video bitrate.")
        args += ["-b:v", f"{video_kbps}k"]
    else:
        args += _encoder_quality_args(settings.video_codec, settings.crf)
    if settings.encode_preset and not settings.video_codec.endswith("_videotoolbox"):
        args += ["-preset", settings.encode_preset]
    args += list(settings.extra_video_args)
    return args


def _audio_args(settings: CompressionSettings, include_audio: bool = True) -> List[str]:
    if not include_audio or settings.audio_codec == "an":
        return ["-an"]
    if settings.audio_codec == "copy":
        args = ["-c:a", "copy"]
    else:
        args = ["-c:a", settings.audio_codec, "-b:a", f"{settings.audio_bitrate}k"]
    audio_filters: List[str] = []
    if settings.audio_downmix:
        args += ["-ac", "2"]
    if settings.loudness_normalize and settings.audio_codec != "copy":
        audio_filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    if audio_filters:
        args += ["-af", ",".join(audio_filters)]
    return args


def _map_args(settings: CompressionSettings, include_audio: bool = True) -> List[str]:
    args = ["-map", "0:v:0"]
    if include_audio and settings.audio_codec != "an":
        args += ["-map", "0:a:0?"]
    if settings.subtitle_mode == "passthrough":
        args += ["-map", "0:s?"]
    elif settings.subtitle_mode == "strip":
        args += ["-sn"]
    return args


def _passlogfile(settings: CompressionSettings) -> Path:
    return settings.output_path.with_name(f".{settings.output_path.stem}.ffmpeg2pass")


def _null_output() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"


def build_ffmpeg_commands(
    settings: CompressionSettings,
    info: Optional[dict] = None,
    ffmpeg: str = "ffmpeg",
) -> List[List[str]]:
    """Build one or two deterministic FFmpeg commands without running them."""

    normalized = settings.normalized()
    if not normalized.input_path.is_file():
        raise VideoCrushError(f"Input file does not exist: {normalized.input_path}")
    if normalized.mode == "target-size":
        if info is None:
            raise VideoCrushError("Probe metadata is required for target-size mode.")
        video_kbps = calculate_video_bitrate(info, normalized)
    else:
        video_kbps = None

    filters = _filter_chain(normalized)
    passlog = _passlogfile(normalized)
    common = [ffmpeg, "-hide_banner", "-y", "-i", str(normalized.input_path)]
    commands: List[List[str]] = []
    use_two_pass = normalized.mode == "target-size" and normalized.two_pass

    if use_two_pass:
        pass_one = list(common) + _map_args(normalized, include_audio=False)
        if filters:
            pass_one += ["-vf", filters]
        pass_one += _codec_args(normalized, video_kbps)
        pass_one += ["-pass", "1", "-passlogfile", str(passlog), "-an", "-f", "null", _null_output()]
        commands.append(pass_one)

    final = list(common) + _map_args(normalized, include_audio=True)
    if filters:
        final += ["-vf", filters]
    final += _codec_args(normalized, video_kbps)
    if use_two_pass:
        final += ["-pass", "2", "-passlogfile", str(passlog)]
    final += _audio_args(normalized)
    if normalized.output_path.suffix.lower() == ".mp4":
        final += ["-movflags", "+faststart"]
    final.append(str(normalized.output_path))
    commands.append(final)
    return commands


def _parse_crop(output: str) -> Optional[str]:
    matches = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", output)
    if not matches:
        return None
    width, height, x, y = matches[-1]
    return f"crop={width}:{height}:{x}:{y}"


def detect_crop(
    filepath: Path,
    ffmpeg: Optional[str] = None,
    sample_seconds: int = 10,
) -> Optional[str]:
    """Run FFmpeg cropdetect on a short sample and return its last crop filter."""

    binary = ffmpeg or find_ffmpeg()
    if not binary:
        raise VideoCrushError("FFmpeg was not found.")
    command = [
        binary,
        "-hide_banner",
        "-ss",
        "0",
        "-i",
        str(filepath),
        "-t",
        str(max(1, sample_seconds)),
        "-vf",
        "cropdetect=24:16:0",
        "-f",
        "null",
        _null_output(),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        creationflags=_creation_flags(),
        timeout=max(30, sample_seconds * 5),
        check=False,
    )
    return _parse_crop(result.stderr + "\n" + result.stdout)


def supported_encoders(ffmpeg: Optional[str] = None) -> Dict[str, str]:
    """Return the known UI labels whose FFmpeg encoder is available."""

    binary = ffmpeg or find_ffmpeg()
    if not binary:
        return {}
    result = subprocess.run(
        [binary, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        creationflags=_creation_flags(),
        timeout=30,
        check=False,
    )
    available = set(re.findall(r"\s([a-z0-9_]+)\s+", result.stdout))
    return {label: codec for label, codec in ENCODERS.items() if codec in available}


def hardware_accelerators(ffmpeg: Optional[str] = None) -> List[str]:
    binary = ffmpeg or find_ffmpeg()
    if not binary:
        return []
    result = subprocess.run(
        [binary, "-hide_banner", "-hwaccels"],
        capture_output=True,
        text=True,
        creationflags=_creation_flags(),
        timeout=30,
        check=False,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip() and not line.startswith("Hardware")]


def choose_hardware_encoder(available: Optional[Dict[str, str]] = None) -> Optional[str]:
    available = available or supported_encoders()
    system = platform.system()
    preference: List[str]
    if system == "Darwin":
        preference = ["H.264 (VideoToolbox)", "H.265 (VideoToolbox)"]
    else:
        preference = [
            "H.264 (NVENC)",
            "H.265 (NVENC)",
            "H.264 (QSV)",
            "H.265 (QSV)",
            "H.264 (AMF)",
            "H.265 (AMF)",
        ]
    for label in preference:
        if label in available:
            return available[label]
    return None


def collect_input_files(
    input_path: Path,
    recursive: bool = False,
    extensions: Optional[Iterable[str]] = None,
) -> List[Path]:
    """Collect a deterministic list of supported video files."""

    path = Path(input_path)
    allowed = {str(ext).lower() if str(ext).startswith(".") else "." + str(ext).lower() for ext in (extensions or VIDEO_EXTENSIONS)}
    if path.is_file():
        return [path] if path.suffix.lower() in allowed else []
    if not path.is_dir():
        return []
    iterator = path.rglob("*") if recursive else path.glob("*")
    return sorted((item for item in iterator if item.is_file() and item.suffix.lower() in allowed), key=lambda p: str(p).lower())


def output_path_for(input_path: Path, output: Path, output_format: Optional[str] = None, multiple: bool = False) -> Path:
    """Resolve a CLI output directory or exact single-file output path."""

    output = Path(output)
    if not multiple and output.suffix:
        return output
    extension = output_format or input_path.suffix.lstrip(".") or "mp4"
    if extension.startswith("."):
        extension = extension[1:]
    return output / f"{input_path.stem}_compressed.{extension}"


def command_to_text(command: Sequence[str], shell: str = "powershell") -> str:
    if shell.lower() in {"cmd", "bat", "windows"}:
        return subprocess.list2cmdline([str(part) for part in command])
    return " ".join(shlex.quote(str(part)) for part in command)


def commands_to_script(commands: Sequence[Sequence[str]], shell: str = "powershell") -> str:
    """Render reproducible commands as a Windows .bat or POSIX shell script."""

    windows = shell.lower() in {"cmd", "bat", "windows"}
    lines = ["@echo off" if windows else "#!/usr/bin/env sh", ""]
    for command in commands:
        lines.append(command_to_text(command, "cmd" if windows else "sh"))
        lines.append("if errorlevel 1 exit /b 1" if windows else "if [ $? -ne 0 ]; then exit 1; fi")
    return "\n".join(lines) + "\n"


def cleanup_pass_logs(settings: CompressionSettings) -> None:
    base = _passlogfile(settings)
    for suffix in ("", "-0.log", "-0.log.mbtree", ".log", ".log.mbtree"):
        candidate = Path(str(base) + suffix)
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


def run_ffmpeg_command(
    command: Sequence[str],
    duration: float,
    progress_callback: Optional[Callable[[int], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    progress_offset: int = 0,
    progress_scale: int = 100,
) -> None:
    """Run FFmpeg with machine-readable progress and no visible console window."""

    full_command = list(command) + ["-progress", "pipe:1", "-nostats"]
    try:
        process = subprocess.Popen(
            full_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=_creation_flags(),
        )
    except OSError as exc:
        raise VideoCrushError(f"Could not start FFmpeg: {exc}") from exc

    tail: List[str] = []
    monitor_stop = threading.Event()

    def monitor_process() -> None:
        suspended = False
        while not monitor_stop.is_set() and process.poll() is None:
            if cancel_event and cancel_event.is_set():
                try:
                    process.terminate()
                except OSError:
                    pass
                break
            should_pause = bool(pause_event and pause_event.is_set())
            if should_pause and not suspended:
                suspended = _suspend_process(process)
            elif not should_pause and suspended:
                _resume_process(process)
                suspended = False
            time.sleep(0.05)
        if suspended:
            _resume_process(process)

    monitor_thread = threading.Thread(target=monitor_process, name="videocrush-process-monitor", daemon=True)
    monitor_thread.start()
    try:
        assert process.stdout is not None
        for raw_line in iter(process.stdout.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            tail.append(line)
            del tail[:-20]
            if "=" in line:
                key, value = line.split("=", 1)
                current_seconds: Optional[float] = None
                if key == "out_time_ms":
                    try:
                        current_seconds = float(value) / 1_000_000
                    except ValueError:
                        pass
                elif key == "out_time":
                    match = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", value)
                    if match:
                        hours, minutes, seconds = match.groups()
                        current_seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                if current_seconds is not None and progress_callback:
                    percent = min(100, max(0, int(current_seconds / duration * 100))) if duration > 0 else 0
                    progress_callback(progress_offset + int(percent * progress_scale / 100))
            elif log_callback and not line.startswith("frame="):
                log_callback(line)
        return_code = process.wait()
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise VideoCrushError("FFmpeg did not stop after cancellation.")
    finally:
        monitor_stop.set()
        monitor_thread.join(timeout=1)
        if process.stdout:
            process.stdout.close()
    if cancel_event and cancel_event.is_set():
        raise VideoCrushError("Compression cancelled.")
    if return_code != 0:
        detail = "\n".join(tail[-5:])
        raise VideoCrushError(f"FFmpeg failed with exit code {return_code}.\n{detail}")
    if progress_callback:
        progress_callback(progress_offset + progress_scale)


def run_compression(
    settings: CompressionSettings,
    progress_callback: Optional[Callable[[int], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    status_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
    ffmpeg: Optional[str] = None,
    ffprobe: Optional[str] = None,
) -> CompressionResult:
    """Probe, build, and execute a compression job."""

    normalized = settings.normalized()
    if not normalized.input_path.is_file():
        raise VideoCrushError(f"Input file does not exist: {normalized.input_path}")
    binary = ffmpeg or find_ffmpeg()
    if not binary:
        raise VideoCrushError("FFmpeg not found. Install FFmpeg or add it to PATH.")
    info = probe_video(normalized.input_path, ffprobe=ffprobe)
    if not info:
        raise VideoCrushError("Could not read video file metadata.")
    duration = media_duration(info)
    if duration <= 0:
        raise VideoCrushError("Could not determine video duration.")
    if normalized.crop_mode == "auto" and not normalized.crop_filter:
        normalized.crop_filter = detect_crop(normalized.input_path, ffmpeg=binary)
        if normalized.crop_filter:
            if log_callback:
                log_callback(f"Detected crop: {normalized.crop_filter}")
        elif log_callback:
            log_callback("No letterbox detected; preserving the original frame.")
    normalized.output_path.parent.mkdir(parents=True, exist_ok=True)
    commands = build_ffmpeg_commands(normalized, info=info, ffmpeg=binary)
    if log_callback:
        for index, command in enumerate(commands, start=1):
            label = "Pass 1/2" if len(commands) == 2 and index == 1 else "Encode"
            log_callback(f"{label}: {command_to_text(command, 'cmd' if os.name == 'nt' else 'sh')}")
    try:
        for index, command in enumerate(commands):
            if status_callback:
                status_callback("Pass 1/2 — Analyzing..." if len(commands) == 2 and index == 0 else "Encoding...")
            offset = int(index * 100 / len(commands))
            scale = int(100 / len(commands))
            run_ffmpeg_command(
                command,
                duration,
                progress_callback=progress_callback,
                log_callback=log_callback,
                cancel_event=cancel_event,
                pause_event=pause_event,
                progress_offset=offset,
                progress_scale=scale,
            )
        if not normalized.output_path.is_file():
            raise VideoCrushError("FFmpeg completed without creating the output file.")
        input_size = normalized.input_path.stat().st_size
        output_size = normalized.output_path.stat().st_size
        saved = (1 - output_size / input_size) * 100 if input_size else 0.0
        if progress_callback:
            progress_callback(100)
        if status_callback:
            status_callback("Done")
        return CompressionResult(
            input_path=normalized.input_path,
            output_path=normalized.output_path,
            input_size=input_size,
            output_size=output_size,
            saved_percent=saved,
            duration=duration,
            commands=tuple(tuple(command) for command in commands),
        )
    finally:
        cleanup_pass_logs(normalized)


def settings_from_profile(
    profile_name: str,
    input_path: Path,
    output_path: Path,
    **overrides,
) -> CompressionSettings:
    try:
        profile = PRESET_PROFILES[profile_name]
    except KeyError as exc:
        raise VideoCrushError(f"Unknown preset profile: {profile_name}") from exc
    values = {
        "input_path": Path(input_path),
        "output_path": Path(output_path),
        "video_codec": profile.video_codec,
        "audio_codec": profile.audio_codec,
        "encode_preset": profile.encode_preset,
        "resolution": profile.resolution,
        "audio_bitrate": profile.audio_bitrate,
        "mode": profile.mode,
        "target_mb": profile.target_mb,
        "crf": profile.crf,
        "two_pass": profile.two_pass,
        "output_format": profile.output_format,
    }
    values.update(overrides)
    return CompressionSettings(**values)
