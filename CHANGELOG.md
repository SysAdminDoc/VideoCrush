# Changelog

All notable changes to VideoCrush will be documented in this file.

## [v0.2.0] - 2026-08-03

- Added a tested, dependency-free FFmpeg core shared by the desktop app and CLI.
- Added target-size and quality encoding modes, advanced crop/HDR/subtitle/audio filters, and deterministic command-script export.
- Added the `videocrush` CLI with folder recursion, extension filtering, preset profiles, dry-run output, and JSON results.
- Added a persisted queue window with drag/drop and folder intake, per-file overrides, priority/reorder/retry controls, pause/resume, and per-job logs.
- Fixed frozen-build bootstrap recursion and verified the packaged queue window on the isolated virtual display.
- Added verified hardware-encoder discovery/default selection, platform-oriented profiles, no-upscale resolution caps, per-track subtitle burn-in/pass/strip, HDR tone mapping, loudness/downmix controls, and AV1 constrained-VBR scene tuning.
- Added versioned preset JSON exchange, watch-folder extension routing, explicit Task Scheduler/context-menu helpers, and opt-in post-queue sleep/shutdown actions.
- Added persisted quality reports with before/after thumbnail strips, SSIM/VMAF metrics, file-size deltas, palette-optimized GIF creation, scene detection, optional URL/Whisper/upscaler adapters, image-sibling optimization, and battery-aware encoding.
- Moved hardware encoder verification off the GUI's first-window path so packaged startup stays responsive while retaining verified hardware defaults.
- Added a bundled-FFmpeg PyInstaller build script, portable local queue storage, a read-only release update check, and four-worker CLI batch processing.

## [v0.1.0] - %Y->- (HEAD -> main, origin/main, origin/HEAD)

- Added: Add screenshot to README
- Added: Add screenshot to README
- Added: Add screenshot to README
- docs: add Related Tools cross-reference to MediaForge
- Added: Add comprehensive README
- Added: Add files via upload

## Roadmap archive — 2026-08-10 — ROADMAP.md

<details>
<summary>Original roadmap snapshot</summary>

```markdown
# Roadmap

Forward-looking plans for VideoCrush — Python GUI video compressor with FFmpeg backend, preset profiles, and batch queue.

## Planned Features

### Encoding

### UX

### Presets

### Automation

### Distribution

## Competitive Research

- **HandBrake**: the gold standard. Queue, presets, CRF slider wording — mimic as far as sensible, differentiate on Windows-first integration and drag-drop.
- **Shutter Encoder**: broad format coverage, dense UI. We stay leaner but can borrow their preset library format for compatibility.
- **FFmpeg Batch AV Converter**: lightweight Windows wrapper. Proves there's a niche for a simpler-than-HandBrake Windows tool.
- **Compressor.io / Squoosh for video**: web-based. Good UX reference for the "drag a file, watch the size drop" first-time experience.

## Nice-to-Haves


## Open-Source Research (Round 2)

### Related OSS Projects
- https://github.com/codeforreal1/compressO — Tauri + React FFmpeg compressor, cross-platform
- https://github.com/eibols/ffmpeg_batch — FFmpeg Batch AV Converter, drag/drop + pause/resume + shutdown
- https://github.com/MattMcManis/Axiom — FFmpeg GUI for Windows, command script generator
- https://github.com/zbabac/VCT — Video Converter & Transcoder, full manual ffmpeg edit
- https://github.com/nikmedoed/easy-video-compress — ThreadPoolExecutor parallel + explorer context menu
- https://github.com/addyosmani/video-compress — ffmpeg.wasm in-browser reference
- https://github.com/awesomelistsio/awesome-ffmpeg — curated tool/resource index
- https://github.com/Mordekai66/Video-Compressor — Tkinter + real-time preview baseline

### Features to Borrow

### Patterns & Architectures Worth Studying
- Tauri shell over FFmpeg CLI (compressO) — keeps the heavy codec out of app memory, lets Rust drive progress parsing
- Frontend progress parse via ffmpeg `-progress pipe:1` + key=value lines instead of stderr regex (compressO, easy-video-compress)
- Queue model with per-item state (pending/running/paused/error/done) + persistable job spec JSON so crashes resume (ffmpeg_batch)
- NVENC/QSV/AMF autodetect at launch, cache result, expose in UI (Axiom)
- Dual encode path: quality-target (CRF) vs size-target (two-pass bitrate) — switch based on user intent, not codec (VCT)
```

</details>
