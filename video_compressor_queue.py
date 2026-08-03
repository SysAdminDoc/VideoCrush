"""Queue-oriented PyQt6 window for VideoCrush."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from videocrush_core import (
    PRESET_PROFILES,
    VideoCrushError,
    choose_hardware_encoder,
    collect_input_files,
    hardware_accelerators,
    settings_from_profile,
    supported_encoders,
)
from videocrush_queue import JobQueue, QueueStore, default_queue_path
from videocrush_automation import export_presets, import_presets, perform_power_action

# video_compressor imports this module only after its legacy helpers and shared
# CompressionWorker have been defined. When launched as a script those helpers
# live in __main__; when imported they live under the normal module name.

_app = sys.modules.get("video_compressor") or sys.modules["__main__"]
PRESETS = _app.PRESETS
VIDEO_CODECS = _app.VIDEO_CODECS
VIDEO_EXTENSIONS = _app.VIDEO_EXTENSIONS
CompressionWorker = _app.CompressionWorker
DropZone = _app.DropZone
find_ffmpeg = _app.find_ffmpeg
format_duration = _app.format_duration
format_size = _app.format_size
probe_video = _app.probe_video


class HardwareProbe(QThread):
    """Discover and verify hardware encoders without delaying the first window."""

    completed = pyqtSignal(dict, object, list)

    def __init__(self, ffmpeg_path, parent=None):
        super().__init__(parent)
        self.ffmpeg_path = ffmpeg_path

    def run(self):
        try:
            available = supported_encoders(self.ffmpeg_path)
            preferred = choose_hardware_encoder(available)
            accelerators = hardware_accelerators(self.ffmpeg_path)
        except (OSError, subprocess.SubprocessError, ValueError):
            available, preferred, accelerators = {}, None, []
        self.completed.emit(available, preferred, accelerators)


class QueueVideoCompressorWindow(QMainWindow):
    """Queue-first desktop workflow backed by the shared encoding core."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"VideoCrush v{self._version()}")
        self.setMinimumSize(820, 860)
        self.resize(900, 940)
        self.input_path = None
        self.video_info = None
        self.worker = None
        self._current_job_id = None
        self._stop_after_current = False
        self._log_visible = True
        self.profiles = PRESET_PROFILES
        self.queue_store = QueueStore(default_queue_path())
        try:
            self.queue = self.queue_store.load()
            self.queue.reset_running()
        except VideoCrushError:
            self.queue = JobQueue()

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(20, 16, 20, 16)
        main_layout.setSpacing(10)

        header = QHBoxLayout()
        title_block = QVBoxLayout()
        title = QLabel("VideoCrush")
        title.setObjectName("titleLabel")
        title_block.addWidget(title)
        subtitle = QLabel(f"v{self._version()}  —  Queue-based target-size and quality compression")
        subtitle.setObjectName("subtitleLabel")
        title_block.addWidget(subtitle)
        header.addLayout(title_block)
        header.addStretch()
        ffmpeg_path = find_ffmpeg()
        ffmpeg_available = bool(ffmpeg_path)
        available_encoders = {}
        preferred_hardware = None
        self.ffmpeg_status = QLabel("FFmpeg ready" if ffmpeg_available else "FFmpeg not found!")
        self.ffmpeg_status.setStyleSheet(
            "color: #a6e3a1; font-size: 12px; font-weight: bold;"
            if ffmpeg_available
            else "color: #f38ba8; font-size: 12px; font-weight: bold;"
        )
        header.addWidget(self.ffmpeg_status)
        main_layout.addLayout(header)

        input_group = QGroupBox("Input")
        input_layout = QVBoxLayout(input_group)
        self.drop_zone = DropZone()
        self.drop_zone.file_dropped.connect(self.add_dropped_file)
        input_layout.addWidget(self.drop_zone)

        path_row = QHBoxLayout()
        self.path_label = QLineEdit()
        self.path_label.setPlaceholderText("No file selected...")
        self.path_label.setReadOnly(True)
        path_row.addWidget(self.path_label, 1)
        add_files_btn = QPushButton("Add Files")
        add_files_btn.setObjectName("secondaryBtn")
        add_files_btn.clicked.connect(self.add_files)
        path_row.addWidget(add_files_btn)
        add_folder_btn = QPushButton("Add Folder")
        add_folder_btn.setObjectName("secondaryBtn")
        add_folder_btn.clicked.connect(self.add_folder)
        path_row.addWidget(add_folder_btn)
        input_layout.addLayout(path_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Folder extensions"))
        self.extension_filter = QLineEdit()
        self.extension_filter.setPlaceholderText("All video formats, or: mp4,mkv,mov")
        filter_row.addWidget(self.extension_filter, 1)
        add_current_btn = QPushButton("Add Selected")
        add_current_btn.setObjectName("secondaryBtn")
        add_current_btn.clicked.connect(self.add_current_file)
        filter_row.addWidget(add_current_btn)
        input_layout.addLayout(filter_row)

        self.info_label = QLabel("")
        self.info_label.setObjectName("fileInfoLabel")
        self.info_label.setWordWrap(True)
        self.info_label.setVisible(False)
        input_layout.addWidget(self.info_label)
        main_layout.addWidget(input_group)

        queue_group = QGroupBox("Queue")
        queue_layout = QVBoxLayout(queue_group)
        self.queue_list = QListWidget()
        self.queue_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_list.setMinimumHeight(125)
        self.queue_list.currentItemChanged.connect(self.on_queue_selected)
        queue_layout.addWidget(self.queue_list)

        queue_controls = QHBoxLayout()
        for label, handler in (
            ("Remove", self.remove_selected),
            ("Move Up", lambda: self.move_selected(-1)),
            ("Move Down", lambda: self.move_selected(1)),
            ("Retry Failed", self.retry_selected),
        ):
            button = QPushButton(label)
            button.setObjectName("secondaryBtn")
            button.clicked.connect(handler)
            queue_controls.addWidget(button)
        queue_controls.addWidget(QLabel("Priority"))
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(-100, 100)
        queue_controls.addWidget(self.priority_spin)
        priority_btn = QPushButton("Apply")
        priority_btn.setObjectName("secondaryBtn")
        priority_btn.clicked.connect(self.apply_priority)
        queue_controls.addWidget(priority_btn)
        queue_layout.addLayout(queue_controls)
        main_layout.addWidget(queue_group)

        settings_group = QGroupBox("Compression Settings — applied per queue item")
        settings_grid = QGridLayout(settings_group)
        settings_grid.setSpacing(8)
        settings_grid.addWidget(QLabel("Profile"), 0, 0)
        self.profile_combo = QComboBox()
        for name in self.profiles:
            self.profile_combo.addItem(name, name)
        self.profile_combo.currentIndexChanged.connect(self.apply_profile_defaults)
        settings_grid.addWidget(self.profile_combo, 0, 1)
        export_btn = QPushButton("Export JSON")
        export_btn.setObjectName("secondaryBtn")
        export_btn.clicked.connect(self.export_preset_file)
        settings_grid.addWidget(export_btn, 0, 2)
        import_btn = QPushButton("Import JSON")
        import_btn.setObjectName("secondaryBtn")
        import_btn.clicked.connect(self.import_preset_file)
        settings_grid.addWidget(import_btn, 0, 3)

        settings_grid.addWidget(QLabel("Mode"), 1, 0)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Target file size (two-pass)", "target-size")
        self.mode_combo.addItem("Quality target (CRF/CQ)", "quality")
        self.mode_combo.currentIndexChanged.connect(self.update_mode_controls)
        settings_grid.addWidget(self.mode_combo, 1, 1)

        settings_grid.addWidget(QLabel("Target Size"), 2, 0)
        self.target_size_spin = QDoubleSpinBox()
        self.target_size_spin.setRange(0.5, 10000)
        self.target_size_spin.setValue(25.0)
        self.target_size_spin.setSuffix(" MB")
        self.target_size_spin.setDecimals(1)
        self.target_size_spin.setSingleStep(5)
        settings_grid.addWidget(self.target_size_spin, 2, 1)

        settings_grid.addWidget(QLabel("Quality CRF/CQ"), 3, 0)
        self.crf_spin = QDoubleSpinBox()
        self.crf_spin.setRange(0, 63)
        self.crf_spin.setValue(23)
        self.crf_spin.setDecimals(1)
        settings_grid.addWidget(self.crf_spin, 3, 1)

        settings_grid.addWidget(QLabel("Video Codec"), 4, 0)
        self.codec_combo = QComboBox()
        for name, codec in VIDEO_CODECS.items():
            software = codec in {"libx264", "libx265", "libvpx-vp9", "libsvtav1", "libaom-av1", "ffv1"}
            if software or codec in available_encoders.values():
                self.codec_combo.addItem(name, codec)
        if preferred_hardware:
            self._set_combo_data(self.codec_combo, preferred_hardware)
        settings_grid.addWidget(self.codec_combo, 4, 1)

        settings_grid.addWidget(QLabel("Encode Preset"), 5, 0)
        self.preset_combo = QComboBox()
        for name in PRESETS:
            self.preset_combo.addItem(name)
        self.preset_combo.setCurrentText("medium")
        settings_grid.addWidget(self.preset_combo, 5, 1)

        settings_grid.addWidget(QLabel("Resolution"), 6, 0)
        self.res_combo = QComboBox()
        self.res_combo.addItems(["Original", "2160p", "1440p", "1080p", "720p", "480p", "360p"])
        settings_grid.addWidget(self.res_combo, 6, 1)

        settings_grid.addWidget(QLabel("Audio"), 7, 0)
        audio_row = QHBoxLayout()
        self.audio_combo = QComboBox()
        self.audio_combo.addItems(["AAC", "Opus", "Copy Original", "No Audio"])
        audio_row.addWidget(self.audio_combo)
        audio_row.addWidget(QLabel("Bitrate:"))
        self.audio_bitrate_spin = QSpinBox()
        self.audio_bitrate_spin.setRange(32, 320)
        self.audio_bitrate_spin.setValue(128)
        self.audio_bitrate_spin.setSuffix(" kbps")
        audio_row.addWidget(self.audio_bitrate_spin)
        settings_grid.addLayout(audio_row, 7, 1)

        settings_grid.addWidget(QLabel("Output Format"), 8, 0)
        self.format_combo = QComboBox()
        for name in (".mp4", ".mkv", ".webm"):
            self.format_combo.addItem(name, name)
        settings_grid.addWidget(self.format_combo, 8, 1)

        settings_grid.addWidget(QLabel("Crop / HDR"), 9, 0)
        advanced_row = QHBoxLayout()
        self.crop_combo = QComboBox()
        self.crop_combo.addItem("Keep frame", "none")
        self.crop_combo.addItem("Auto-remove letterbox", "auto")
        advanced_row.addWidget(self.crop_combo)
        self.hdr_combo = QComboBox()
        self.hdr_combo.addItem("HDR passthrough", "passthrough")
        self.hdr_combo.addItem("Tone-map to SDR", "tone-map-sdr")
        advanced_row.addWidget(self.hdr_combo)
        settings_grid.addLayout(advanced_row, 9, 1)

        settings_grid.addWidget(QLabel("Subtitles / Audio"), 10, 0)
        advanced_audio_row = QHBoxLayout()
        self.subtitle_combo = QComboBox()
        self.subtitle_combo.addItem("Pass subtitles", "passthrough")
        self.subtitle_combo.addItem("Strip subtitles", "strip")
        self.subtitle_combo.addItem("Burn selected file", "burn-in")
        advanced_audio_row.addWidget(self.subtitle_combo)
        self.subtitle_track_check = QCheckBox("Track only")
        advanced_audio_row.addWidget(self.subtitle_track_check)
        self.subtitle_track_spin = QSpinBox()
        self.subtitle_track_spin.setRange(0, 99)
        self.subtitle_track_spin.setPrefix("#")
        advanced_audio_row.addWidget(self.subtitle_track_spin)
        self.subtitle_path_edit = QLineEdit()
        self.subtitle_path_edit.setReadOnly(True)
        self.subtitle_path_edit.setPlaceholderText("No burn-in file")
        advanced_audio_row.addWidget(self.subtitle_path_edit, 1)
        subtitle_btn = QPushButton("Choose")
        subtitle_btn.setObjectName("secondaryBtn")
        subtitle_btn.clicked.connect(self.choose_subtitle_file)
        advanced_audio_row.addWidget(subtitle_btn)
        self.downmix_check = QCheckBox("Downmix stereo")
        advanced_audio_row.addWidget(self.downmix_check)
        self.loudness_check = QCheckBox("EBU R128 normalize")
        advanced_audio_row.addWidget(self.loudness_check)
        settings_grid.addLayout(advanced_audio_row, 10, 1)

        settings_grid.addWidget(QLabel("AV1 / VBR"), 11, 0)
        quality_row = QHBoxLayout()
        self.constrained_vbr_check = QCheckBox("Constrained VBR")
        quality_row.addWidget(self.constrained_vbr_check)
        quality_row.addWidget(QLabel("Max kbps"))
        self.max_bitrate_spin = QSpinBox()
        self.max_bitrate_spin.setRange(50, 500000)
        self.max_bitrate_spin.setValue(4000)
        quality_row.addWidget(self.max_bitrate_spin)
        self.scene_crf_check = QCheckBox("Scene-aware CRF")
        quality_row.addWidget(self.scene_crf_check)
        settings_grid.addLayout(quality_row, 11, 1)
        settings_grid.addWidget(QLabel("Power"), 12, 0)
        self.battery_check = QCheckBox("Pause on battery; resume on AC")
        settings_grid.addWidget(self.battery_check, 12, 1)
        main_layout.addWidget(settings_group)

        progress_group = QGroupBox("Progress")
        progress_layout = QVBoxLayout(progress_group)
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("accentLabel")
        progress_layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        progress_layout.addWidget(self.progress_bar)
        control_row = QHBoxLayout()
        self.compress_btn = QPushButton("🔥 Start Queue")
        self.compress_btn.setFixedHeight(40)
        self.compress_btn.clicked.connect(self.start_compression)
        control_row.addWidget(self.compress_btn)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setFixedHeight(40)
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self.toggle_pause)
        control_row.addWidget(self.pause_btn)
        self.cancel_btn = QPushButton("Cancel Job")
        self.cancel_btn.setObjectName("dangerBtn")
        self.cancel_btn.setFixedHeight(40)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_compression)
        control_row.addWidget(self.cancel_btn)
        self.open_folder_btn = QPushButton("Open Output Folder")
        self.open_folder_btn.setObjectName("secondaryBtn")
        self.open_folder_btn.setFixedHeight(40)
        self.open_folder_btn.setVisible(False)
        self.open_folder_btn.clicked.connect(self.open_output_folder)
        control_row.addWidget(self.open_folder_btn)
        control_row.addWidget(QLabel("After:"))
        self.after_queue_combo = QComboBox()
        self.after_queue_combo.addItem("Keep running", "none")
        self.after_queue_combo.addItem("Sleep", "sleep")
        self.after_queue_combo.addItem("Shut down", "shutdown")
        control_row.addWidget(self.after_queue_combo)
        progress_layout.addLayout(control_row)
        main_layout.addWidget(progress_group)

        log_group = QGroupBox("Per-job FFmpeg log")
        log_layout = QVBoxLayout(log_group)
        log_toggle = QPushButton("Hide log")
        log_toggle.setObjectName("secondaryBtn")
        log_toggle.clicked.connect(self.toggle_log)
        log_layout.addWidget(log_toggle)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(110)
        log_layout.addWidget(self.log_text)
        main_layout.addWidget(log_group)

        self._last_output = None
        self.refresh_queue()
        self.apply_profile_defaults()
        self.update_mode_controls()
        self._hardware_probe = None
        if ffmpeg_path and "--smoke" not in sys.argv:
            self._hardware_probe = HardwareProbe(ffmpeg_path, self)
            self._hardware_probe.completed.connect(self._apply_hardware_probe)
            self._hardware_probe.finished.connect(self._hardware_probe.deleteLater)
            self._hardware_probe.start()

    @staticmethod
    def _version():
        from videocrush_core import VERSION

        return VERSION

    def _apply_hardware_probe(self, available, preferred, accelerators):
        existing = {self.codec_combo.itemData(index) for index in range(self.codec_combo.count())}
        for label, codec in VIDEO_CODECS.items():
            if codec in available.values() and codec not in existing:
                self.codec_combo.addItem(label, codec)
                existing.add(codec)
        if preferred:
            self._set_combo_data(self.codec_combo, preferred)
            self.ffmpeg_status.setText(f"FFmpeg ready · HW default: {preferred}")
        elif accelerators:
            self.ffmpeg_status.setText("FFmpeg ready · HW: " + ", ".join(accelerators[:2]))

    def _save_queue(self):
        try:
            self.queue_store.save(self.queue)
        except VideoCrushError as exc:
            self.log_text.append(f"⚠ Could not save queue: {exc}")

    def refresh_queue(self, selected_id=None):
        selected_id = selected_id or self._current_job_id
        self.queue_list.blockSignals(True)
        self.queue_list.clear()
        selected_item = None
        for job in self.queue.jobs:
            label = f"[{job.state.upper():9}] {job.name}  priority={job.priority}"
            if job.error:
                label += f" — {job.error[:80]}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, job.id)
            self.queue_list.addItem(item)
            if job.id == selected_id:
                selected_item = item
        if selected_item:
            self.queue_list.setCurrentItem(selected_item)
        elif self.queue_list.count() and not self._current_job_id:
            self.queue_list.setCurrentRow(0)
        self.queue_list.blockSignals(False)

    def _current_overrides(self):
        audio_map = {"AAC": "aac", "Opus": "libopus", "Copy Original": "copy", "No Audio": "an"}
        return {
            "video_codec": self.codec_combo.currentData(),
            "audio_codec": audio_map.get(self.audio_combo.currentText(), "aac"),
            "encode_preset": self.preset_combo.currentText(),
            "resolution": self.res_combo.currentText(),
            "audio_bitrate": self.audio_bitrate_spin.value(),
            "mode": self.mode_combo.currentData(),
            "target_mb": self.target_size_spin.value(),
            "crf": self.crf_spin.value(),
            "two_pass": True,
            "crop_mode": self.crop_combo.currentData(),
            "hdr_mode": self.hdr_combo.currentData(),
            "subtitle_mode": self.subtitle_combo.currentData(),
            "subtitle_path": self.subtitle_path_edit.text() or None,
            "subtitle_track": self.subtitle_track_spin.value() if self.subtitle_track_check.isChecked() else None,
            "audio_downmix": self.downmix_check.isChecked(),
            "loudness_normalize": self.loudness_check.isChecked(),
            "constrained_vbr": self.constrained_vbr_check.isChecked(),
            "max_bitrate_kbps": self.max_bitrate_spin.value() if self.constrained_vbr_check.isChecked() else None,
            "scene_crf": self.scene_crf_check.isChecked(),
            "pause_on_battery": self.battery_check.isChecked(),
        }

    @staticmethod
    def _set_combo_data(combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def apply_profile_defaults(self, _index=0):
        if not hasattr(self, "target_size_spin"):
            return
        profile = self.profiles[self.profile_combo.currentData()]
        self._set_combo_data(self.mode_combo, profile.mode)
        if profile.target_mb is not None:
            self.target_size_spin.setValue(profile.target_mb)
        self.crf_spin.setValue(profile.crf)
        self._set_combo_data(self.codec_combo, profile.video_codec)
        self.preset_combo.setCurrentText(profile.encode_preset)
        self.res_combo.setCurrentText(profile.resolution)
        self.audio_bitrate_spin.setValue(profile.audio_bitrate)

    def update_mode_controls(self, _index=0):
        target_mode = self.mode_combo.currentData() == "target-size"
        self.target_size_spin.setEnabled(target_mode)
        self.crf_spin.setEnabled(not target_mode)

    def export_preset_file(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Presets", "videocrush-presets.json", "JSON (*.json)")
        if not path:
            return
        try:
            export_presets(Path(path), self.profiles)
            self.status_label.setText(f"Presets exported: {path}")
        except VideoCrushError as exc:
            self.status_label.setText(f"Preset export failed: {exc}")

    def import_preset_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Presets", "", "JSON (*.json)")
        if not path:
            return
        try:
            imported = import_presets(Path(path))
            self.profiles.update(imported)
            current = self.profile_combo.currentData()
            self.profile_combo.clear()
            for name in self.profiles:
                self.profile_combo.addItem(name, name)
            self._set_combo_data(self.profile_combo, current)
            self.status_label.setText(f"Imported {len(imported)} preset(s)")
        except VideoCrushError as exc:
            self.status_label.setText(f"Preset import failed: {exc}")

    def _load_file_info(self, path):
        if not os.path.isfile(path):
            return
        self.input_path = path
        self.path_label.setText(path)
        self.drop_zone.text_label.setText(os.path.basename(path))
        self.drop_zone.icon_label.setText("🎬")
        info = probe_video(path)
        self.video_info = info
        file_size = os.path.getsize(path)
        if not info:
            self.info_label.setText(f"Size: {format_size(file_size)}  (could not probe details)")
            self.info_label.setVisible(True)
            return
        parts = [f"Size: {format_size(file_size)}"]
        duration = float(info.get("format", {}).get("duration", 0) or 0)
        if duration > 0:
            parts.append(f"Duration: {format_duration(duration)}")
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "video":
                parts.append(f"Video: {stream.get('width', '?')}x{stream.get('height', '?')}  {stream.get('codec_name', '?')}")
                break
        self.info_label.setText("   |   ".join(parts))
        self.info_label.setVisible(True)

    def add_current_file(self):
        if self.input_path:
            self.add_paths([Path(self.input_path)])

    def add_dropped_file(self, path):
        self.add_paths([Path(path)])

    def add_files(self):
        ext_filter = "Video Files (" + " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS)) + ");;All Files (*)"
        paths, _ = QFileDialog.getOpenFileNames(self, "Add Videos", "", ext_filter)
        if paths:
            self.add_paths([Path(path) for path in paths])

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Add Video Folder")
        if not folder:
            return
        raw_extensions = self.extension_filter.text().strip()
        extensions = [part.strip() for part in raw_extensions.split(",") if part.strip()] or None
        self.add_paths(collect_input_files(Path(folder), recursive=True, extensions=extensions))

    def add_paths(self, paths):
        paths = [Path(path) for path in paths if Path(path).is_file() and Path(path).suffix.lower() in VIDEO_EXTENSIONS]
        if not paths:
            self.status_label.setText("No supported video files found")
            return
        preset = self.profile_combo.currentData()
        overrides = self._current_overrides()
        for path in paths:
            self._load_file_info(str(path))
            if any(Path(job.input_path) == path for job in self.queue.jobs):
                continue
            self.queue.add_file(
                path,
                path.parent,
                preset=preset,
                output_format=self.format_combo.currentData(),
                overrides=overrides,
            )
        self._save_queue()
        self.refresh_queue()
        self.status_label.setText(f"{len(paths)} file(s) ready")

    def choose_subtitle_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Subtitle File",
            "",
            "Subtitles (*.srt *.ass *.ssa *.vtt);;All Files (*)",
        )
        if path:
            self.subtitle_path_edit.setText(path)
            self._set_combo_data(self.subtitle_combo, "burn-in")

    def on_queue_selected(self, item, _previous=None):
        if item is None:
            return
        self._current_job_id = item.data(Qt.ItemDataRole.UserRole)
        try:
            job = self.queue.get(self._current_job_id)
        except KeyError:
            return
        self._set_combo_data(self.profile_combo, job.preset)
        self._load_file_info(job.input_path)
        self.priority_spin.setValue(job.priority)
        self.log_text.setPlainText("\n".join(job.logs))
        audio_labels = {"aac": "AAC", "libopus": "Opus", "copy": "Copy Original", "an": "No Audio"}
        for name, value in job.overrides.items():
            if name == "video_codec":
                self._set_combo_data(self.codec_combo, value)
            elif name == "audio_codec":
                self.audio_combo.setCurrentText(audio_labels.get(value, "AAC"))
            elif name == "encode_preset":
                self.preset_combo.setCurrentText(str(value))
            elif name == "resolution":
                self.res_combo.setCurrentText(str(value))
            elif name == "audio_bitrate":
                self.audio_bitrate_spin.setValue(int(value))
            elif name == "mode":
                self._set_combo_data(self.mode_combo, value)
            elif name == "target_mb":
                self.target_size_spin.setValue(float(value))
            elif name == "crf":
                self.crf_spin.setValue(float(value))
            elif name == "crop_mode":
                self._set_combo_data(self.crop_combo, value)
            elif name == "hdr_mode":
                self._set_combo_data(self.hdr_combo, value)
            elif name == "subtitle_mode":
                self._set_combo_data(self.subtitle_combo, value)
            elif name == "subtitle_path":
                self.subtitle_path_edit.setText(str(value or ""))
            elif name == "subtitle_track":
                self.subtitle_track_check.setChecked(value is not None)
                if value is not None:
                    self.subtitle_track_spin.setValue(int(value))
            elif name == "audio_downmix":
                self.downmix_check.setChecked(bool(value))
            elif name == "loudness_normalize":
                self.loudness_check.setChecked(bool(value))
            elif name == "constrained_vbr":
                self.constrained_vbr_check.setChecked(bool(value))
            elif name == "max_bitrate_kbps" and value is not None:
                self.max_bitrate_spin.setValue(int(value))
            elif name == "scene_crf":
                self.scene_crf_check.setChecked(bool(value))
            elif name == "pause_on_battery":
                self.battery_check.setChecked(bool(value))
        self.update_mode_controls()

    def remove_selected(self):
        item = self.queue_list.currentItem()
        if not item:
            return
        job_id = item.data(Qt.ItemDataRole.UserRole)
        try:
            if self.queue.get(job_id).state == "running":
                return
            self.queue.remove(job_id)
        except KeyError:
            return
        self._current_job_id = None
        self._save_queue()
        self.refresh_queue()

    def move_selected(self, delta):
        item = self.queue_list.currentItem()
        if not item:
            return
        job_id = item.data(Qt.ItemDataRole.UserRole)
        self.queue.move(job_id, delta)
        self._save_queue()
        self.refresh_queue(job_id)

    def apply_priority(self):
        item = self.queue_list.currentItem()
        if not item:
            return
        job_id = item.data(Qt.ItemDataRole.UserRole)
        self.queue.set_priority(job_id, self.priority_spin.value())
        self._save_queue()
        self.refresh_queue(job_id)

    def retry_selected(self):
        item = self.queue_list.currentItem()
        if not item:
            return
        job_id = item.data(Qt.ItemDataRole.UserRole)
        try:
            self.queue.retry(job_id)
        except (KeyError, VideoCrushError):
            return
        self._save_queue()
        self.refresh_queue(job_id)

    def start_compression(self):
        if self.worker:
            return
        if not self.queue.jobs and self.input_path:
            self.add_current_file()
        self._stop_after_current = False
        self._start_next_job()

    def _start_next_job(self):
        if self.worker:
            return
        job = self.queue.next_pending()
        if job is None:
            self.status_label.setText("Queue complete" if self.queue.jobs else "Ready")
            self.compress_btn.setEnabled(True)
            self.pause_btn.setEnabled(False)
            self.cancel_btn.setEnabled(False)
            self._save_queue()
            action = self.after_queue_combo.currentData()
            if action != "none" and not self._stop_after_current:
                try:
                    perform_power_action(action)
                except VideoCrushError as exc:
                    self.log_text.append(f"⚠ Post-queue action failed: {exc}")
            return
        self._current_job_id = job.id
        job.state = "running"
        job.attempts += 1
        settings = settings_from_profile(
            job.preset,
            Path(job.input_path),
            Path(job.output_path),
            profiles=self.profiles,
            **job.overrides,
        )
        self.log_text.setPlainText("")
        self.progress_bar.setValue(0)
        self.status_label.setText(f"Starting {job.name}")
        self.compress_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.open_folder_btn.setVisible(False)
        self.worker = CompressionWorker(
            input_path=str(settings.input_path),
            output_path=str(settings.output_path),
            target_mb=settings.target_mb or 25,
            video_codec=settings.video_codec,
            audio_codec=settings.audio_codec,
            preset=settings.encode_preset,
            resolution=settings.resolution,
            audio_bitrate=settings.audio_bitrate,
            mode=settings.mode,
            crf=settings.crf,
            two_pass=settings.two_pass,
            crop_mode=settings.crop_mode,
            hdr_mode=settings.hdr_mode,
            subtitle_mode=settings.subtitle_mode,
            subtitle_path=str(settings.subtitle_path) if settings.subtitle_path else None,
            subtitle_track=settings.subtitle_track,
            audio_downmix=settings.audio_downmix,
            loudness_normalize=settings.loudness_normalize,
            constrained_vbr=settings.constrained_vbr,
            max_bitrate_kbps=settings.max_bitrate_kbps,
            scene_crf=settings.scene_crf,
            pause_on_battery=settings.pause_on_battery,
        )
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.log.connect(self.append_job_log)
        self.worker.status.connect(self.status_label.setText)
        self.worker.finished.connect(self.on_finished)
        self.worker.error.connect(self.on_error)
        self.worker.cancelled.connect(self.on_cancelled)
        self._save_queue()
        self.refresh_queue(job.id)
        self.worker.start()

    def append_job_log(self, message):
        if self._current_job_id:
            try:
                self.queue.get(self._current_job_id).append_log(message)
            except KeyError:
                pass
        self.log_text.append(message)

    def toggle_pause(self):
        if not self.worker or not self._current_job_id:
            return
        job = self.queue.get(self._current_job_id)
        if job.state == "paused":
            self.worker.resume()
            job.state = "running"
            self.pause_btn.setText("Pause")
            self.status_label.setText("Resuming...")
        else:
            self.worker.pause()
            job.state = "paused"
            self.pause_btn.setText("Resume")
            self.status_label.setText("Paused")
        self._save_queue()
        self.refresh_queue(job.id)

    def cancel_compression(self):
        if self.worker:
            self._stop_after_current = True
            self.cancel_btn.setEnabled(False)
            self.status_label.setText("Cancelling...")
            self.worker.cancel()

    def on_finished(self, result):
        if self._current_job_id:
            job = self.queue.get(self._current_job_id)
            job.state = "done"
            job.error = ""
            self._last_output = result.get("output")
        self.worker = None
        self.pause_btn.setText("Pause")
        self.open_folder_btn.setVisible(bool(self._last_output))
        self._save_queue()
        self.refresh_queue(self._current_job_id)
        QTimer.singleShot(0, self._start_next_job)

    def on_error(self, message):
        if self._current_job_id:
            job = self.queue.get(self._current_job_id)
            job.state = "failed"
            job.error = str(message)
            job.append_log(f"ERROR: {message}")
        self.worker = None
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.status_label.setText("Job failed — continuing queue")
        self._save_queue()
        self.refresh_queue(self._current_job_id)
        QTimer.singleShot(0, self._start_next_job)

    def on_cancelled(self):
        if self._current_job_id:
            self.queue.get(self._current_job_id).state = "cancelled"
        self.worker = None
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.status_label.setText("Job cancelled")
        self._save_queue()
        self.refresh_queue(self._current_job_id)
        if not self._stop_after_current:
            QTimer.singleShot(0, self._start_next_job)
        else:
            self.compress_btn.setEnabled(True)

    def open_output_folder(self):
        if self._last_output and os.path.exists(self._last_output):
            folder = os.path.dirname(self._last_output)
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", self._last_output.replace("/", "\\")])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", self._last_output])
            else:
                subprocess.Popen(["xdg-open", folder])

    def toggle_log(self):
        self._log_visible = not self._log_visible
        self.log_text.setVisible(self._log_visible)
