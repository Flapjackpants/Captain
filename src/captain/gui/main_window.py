"""Captain main window: clip picker, transcript editor, auto-trim, Apply."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..api import ClipInfo, ResolveError, create_resolve_handler
from ..assemble import build_fcp7_xml, next_captain_timeline_name, seconds_to_source_frames
from ..captions import build_caption_segments, normalize_caption_settings
from ..compare import (
    AlignmentResult,
    align_transcript,
    find_script_retakes,
    load_script,
    merge_repeat_groups,
    parse_script,
)
from ..engine import Transcriber, extract_audio, extract_frame
from ..transcript import (
    Transcript,
    SILENCE_DISPLAY_MIN,
    find_repeats,
    find_silence_gaps,
    merge_timeline_transcripts,
)
from .script_view import ScriptView
from .caption_dialog import CaptionSettingsDialog
from .settings_dialog import SettingsDialog
from .transcript_view import TranscriptView

log = logging.getLogger("Captain.gui")

APPLY_REPLACE = "replace_in_place"
APPLY_RIPPLE = "replace_ripple"
APPLY_NEW = "new_timeline"

APPLY_LABELS = {
    APPLY_REPLACE: "Apply → Replace",
    APPLY_RIPPLE: "Apply → Ripple Replace",
    APPLY_NEW: "Apply → New Timeline",
}


class TranscribeWorker(QThread):
    progress = Signal(float, str)
    finished_ok = Signal(object)  # Transcript
    failed = Signal(str)

    def __init__(
        self,
        clip: ClipInfo,
        transcriber: Transcriber,
        language,
        resolve,
        parent=None,
        *,
        initial_prompt: str | None = None,
    ):
        super().__init__(parent)
        self.clip = clip
        self.transcriber = transcriber
        self.language = language
        self.resolve = resolve
        self.initial_prompt = initial_prompt

    def run(self) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="captain_transcribe_") as tmp:
                if self.clip.file_path:
                    self.progress.emit(0.0, "Extracting audio...")
                    wav = extract_audio(
                        self.clip.file_path,
                        start_sec=self.clip.source_start_sec,
                        duration_sec=self.clip.duration_sec,
                        out_path=str(Path(tmp) / "clip.wav"),
                    )
                else:
                    self.progress.emit(0.0, "Rendering selected timeline audio in Resolve...")
                    try:
                        rendered_media = self.resolve.render_clip_audio(
                            self.clip, str(Path(tmp) / "timeline-audio.wav")
                        )
                    except Exception as e:
                        raise RuntimeError(
                            f"Could not render audio for '{self.clip.name}' in Resolve: {e}"
                        ) from e
                    if Path(rendered_media).suffix.lower() == ".wav":
                        wav = rendered_media
                    else:
                        self.progress.emit(0.0, "Extracting audio from Resolve render...")
                        wav = extract_audio(
                            rendered_media,
                            out_path=str(Path(tmp) / "timeline-audio.wav"),
                        )
                transcript = self.transcriber.transcribe(
                    wav,
                    language=self.language,
                    progress=lambda f, m: self.progress.emit(f, m),
                    initial_prompt=self.initial_prompt,
                )
                transcript.source_path = self.clip.file_path or f"timeline:{self.clip.clip_id}"
            self.finished_ok.emit(transcript)
        except Exception as e:  # surfaced to the user in the UI
            log.error("Transcription failed: %s", traceback.format_exc())
            self.failed.emit(str(e))


class TimelineTranscribeWorker(QThread):
    progress = Signal(float, str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        clips: list[ClipInfo],
        transcriber: Transcriber,
        language: str,
        resolve,
        timeline_start_frame: int,
        fps: float,
        timeline_name: str,
        parent=None,
        *,
        initial_prompt: str | None = None,
    ):
        super().__init__(parent)
        self.clips = sorted(clips, key=lambda clip: (clip.timeline_start_frame, clip.track_index))
        self.transcriber = transcriber
        self.language = language
        self.resolve = resolve
        self.timeline_start_frame = timeline_start_frame
        self.fps = fps
        self.timeline_name = timeline_name
        self.initial_prompt = initial_prompt

    def run(self) -> None:
        try:
            parts: list[tuple[Transcript, float]] = []
            end_frame = self.timeline_start_frame
            with tempfile.TemporaryDirectory(prefix="captain_timeline_transcribe_") as tmp:
                for index, clip in enumerate(self.clips):
                    self.progress.emit(
                        index / len(self.clips),
                        f"Transcribing {index + 1}/{len(self.clips)}: {clip.name}",
                    )
                    clip_dir = Path(tmp) / str(index)
                    clip_dir.mkdir()
                    if clip.file_path:
                        wav = extract_audio(
                            clip.file_path,
                            start_sec=clip.source_start_sec,
                            duration_sec=clip.duration_sec,
                            out_path=str(clip_dir / "clip.wav"),
                        )
                    else:
                        rendered_media = self.resolve.render_clip_audio(
                            clip, str(clip_dir / "timeline-audio.wav")
                        )
                        if Path(rendered_media).suffix.lower() == ".wav":
                            wav = rendered_media
                        else:
                            wav = extract_audio(
                                rendered_media,
                                out_path=str(clip_dir / "clip.wav"),
                            )
                    transcript = self.transcriber.transcribe(
                        wav,
                        language=self.language,
                        progress=lambda fraction, message, i=index: self.progress.emit(
                            (i + fraction) / len(self.clips),
                            f"{self.clips[i].name}: {message}",
                        ),
                        initial_prompt=self.initial_prompt,
                    )
                    offset = (clip.timeline_start_frame - self.timeline_start_frame) / self.fps
                    parts.append((transcript, offset))
                    end_frame = max(end_frame, clip.timeline_end_frame)
            merged = merge_timeline_transcripts(
                parts,
                duration=(end_frame - self.timeline_start_frame) / self.fps,
                source_path=f"timeline:{self.timeline_name}",
            )
            self.finished_ok.emit(merged)
        except Exception as e:
            log.error("Timeline transcription failed: %s", traceback.format_exc())
            self.failed.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Captain")

        self.cfg = config.load_config()
        self.resolve = create_resolve_handler()
        self.transcriber = Transcriber(
            model_name=self.cfg["whisper_model"],
            device=self.cfg["whisper_device"],
            compute_type=self.cfg["whisper_compute_type"],
            models_dir=str(config.models_dir()),
        )
        self.clips: list[ClipInfo] = []
        self.current_clip: ClipInfo | None = None
        self._caption_anchor_clip: ClipInfo | None = None
        self._timeline_batch_mode = False
        self._pre_batch_context: tuple[ClipInfo | None, ClipInfo | None, bool] | None = None
        self.worker: TranscribeWorker | None = None
        self._search_matches: list[int] = []
        self._search_pos: int = -1
        self._script_tokens: list[str] = []
        self._script_raw: str = ""
        self._alignment: AlignmentResult | None = None
        self._syncing_selection = False

        self._build_ui()
        self._connect_resolve()

    # ---- UI --------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 8)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.playhead_btn = QPushButton("Use Playhead Clip")
        self.playhead_btn.setToolTip(
            "Select the video clip currently under the Resolve playhead"
        )
        self.playhead_btn.clicked.connect(self._use_playhead_clip)
        self.clip_combo = QComboBox()
        self.clip_combo.setMinimumWidth(240)
        self.clip_combo.setToolTip("Fallback: pick any clip from the current timeline")
        self.clip_combo.currentIndexChanged.connect(self._on_clip_combo_changed)
        self.transcribe_btn = QPushButton("Transcribe")
        self.transcribe_btn.clicked.connect(self._transcribe)
        self.transcribe_timeline_btn = QPushButton("Transcribe Timeline…")
        self.transcribe_timeline_btn.setToolTip(
            "Open a compound clip in its own timeline, refresh, then transcribe its child clips separately"
        )
        self.transcribe_timeline_btn.clicked.connect(self._transcribe_timeline)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._load_clips)
        self.import_script_btn = QPushButton("Import Script…")
        self.import_script_btn.setToolTip(
            "Compare transcript to a plain text, Fountain, or SRT/VTT script"
        )
        self.import_script_btn.clicked.connect(self._import_script)
        self.clear_script_btn = QPushButton("Clear Script")
        self.clear_script_btn.clicked.connect(self._clear_script)
        self.clear_script_btn.setVisible(False)
        self.export_state_btn = QPushButton("Export State…")
        self.export_state_btn.setToolTip(
            "Save this clip's transcript and edits as a JSON file"
        )
        self.export_state_btn.clicked.connect(self._export_state)
        self.import_state_btn = QPushButton("Import State…")
        self.import_state_btn.setToolTip(
            "Load a previously exported transcript state for this clip"
        )
        self.import_state_btn.clicked.connect(self._import_state)
        self.settings_btn = QPushButton("Settings…")
        self.settings_btn.setToolTip("Transcript font, size, and spacing")
        self.settings_btn.clicked.connect(self._open_settings)
        top.addWidget(self.playhead_btn)
        top.addWidget(self.clip_combo, stretch=1)
        top.addWidget(self.transcribe_btn)
        top.addWidget(self.transcribe_timeline_btn)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.import_script_btn)
        top.addWidget(self.clear_script_btn)
        top.addWidget(self.export_state_btn)
        top.addWidget(self.import_state_btn)
        top.addWidget(self.settings_btn)
        layout.addLayout(top)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.addWidget(QLabel("Search"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Find words in the transcript…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search_text)
        self.search_edit.returnPressed.connect(self._find_next)
        self.search_prev_btn = QPushButton("Prev")
        self.search_prev_btn.clicked.connect(self._find_prev)
        self.search_next_btn = QPushButton("Next")
        self.search_next_btn.clicked.connect(self._find_next)
        self.search_count = QLabel("")
        self.search_count.setObjectName("stage")
        self.search_count.setMinimumWidth(64)
        search_row.addWidget(self.search_edit, stretch=1)
        search_row.addWidget(self.search_prev_btn)
        search_row.addWidget(self.search_next_btn)
        search_row.addWidget(self.search_count)
        self._search_bar = QWidget()
        self._search_bar.setLayout(search_row)
        self._search_bar.setVisible(False)
        layout.addWidget(self._search_bar)

        QShortcut(QKeySequence("Ctrl+G"), self, self._find_next)
        QShortcut(QKeySequence("Ctrl+Shift+G"), self, self._find_prev)
        QShortcut(QKeySequence.StandardKey.Find, self, self._show_search)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self._hide_search)
        QShortcut(QKeySequence.StandardKey.Undo, self, self._undo)
        QShortcut(QKeySequence.StandardKey.Redo, self, self._redo)

        self.view = TranscriptView()
        self.view.setObjectName("transcript")
        self.view.set_silence_thresholds(
            SILENCE_DISPLAY_MIN,
            self.cfg["silence_max_pause"],
        )
        self.view.edited.connect(self._on_edited)
        self.view.word_activated.connect(self._on_transcript_word)
        self.view.time_activated.connect(self._jump_to_media_second)

        self.script_view = ScriptView()
        self.script_view.token_activated.connect(self._on_script_token)
        self._apply_typography_from_cfg()
        self.script_panel = QWidget()
        script_layout = QVBoxLayout(self.script_panel)
        script_layout.setContentsMargins(0, 0, 0, 0)
        script_layout.setSpacing(4)
        script_header = QLabel("Script")
        script_header.setObjectName("stage")
        script_layout.addWidget(script_header)
        script_layout.addWidget(self.script_view, stretch=1)
        self.script_panel.setVisible(False)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.script_panel)
        self.splitter.addWidget(self.view)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        layout.addWidget(self.splitter, stretch=1)

        hint = QLabel(
            "Select words, then: Delete toggles remove/restore • "
            "Return adds a caption break before selection/current word • "
            "Backspace merges a manual break when no words are selected • "
            "Cmd/Ctrl+X cuts • Cmd/Ctrl+V pastes • Cmd/Ctrl+Z undo • "
            "Cmd/Ctrl+Shift+Z redo • "
            "click a word/timecode/silence jumps the playhead • "
            "silence markers (…) show long gaps; struck = will be trimmed; "
            "Delete toggles trim vs keep • Trim Silence marks all gaps • "
            "Cmd/Ctrl+F search • Import Script for color compare "
            "(white=match, blue=missing, magenta=extra, red=mismatch, gray=removed)"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.trim_silence_btn = QPushButton("Trim Silence")
        self.trim_silence_btn.clicked.connect(self._trim_silence)
        self.trim_repeats_btn = QPushButton("Remove Repeats")
        self.trim_repeats_btn.clicked.connect(self._trim_repeats)
        self.create_captions_btn = QPushButton("Create Captions")
        self.create_captions_btn.clicked.connect(self._create_captions)
        bottom.addWidget(self.trim_silence_btn)
        bottom.addWidget(self.trim_repeats_btn)
        bottom.addWidget(self.create_captions_btn)
        bottom.addStretch(1)

        self.apply_btn = QToolButton()
        self.apply_btn.setObjectName("accent")
        self.apply_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.apply_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.apply_btn.clicked.connect(self._apply)

        apply_menu = QMenu(self.apply_btn)
        self._apply_action_group = QActionGroup(self)
        self._apply_action_group.setExclusive(True)
        self._apply_actions: dict[str, QAction] = {}
        for mode, title in (
            (APPLY_REPLACE, "Replace in place (keep other tracks)"),
            (APPLY_RIPPLE, "Replace in place (ripple)"),
            (APPLY_NEW, "New timeline"),
        ):
            action = QAction(title, self)
            action.setCheckable(True)
            action.setData(mode)
            self._apply_action_group.addAction(action)
            apply_menu.addAction(action)
            self._apply_actions[mode] = action
            action.triggered.connect(self._on_apply_mode_action)
        self.apply_btn.setMenu(apply_menu)

        mode = config.normalize_apply_mode(self.cfg.get("apply_mode"))
        self.cfg["apply_mode"] = mode
        bottom.addWidget(self.apply_btn)
        layout.addLayout(bottom)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(8)
        self.stage_label = QLabel("")
        self.stage_label.setObjectName("stage")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFixedHeight(14)
        progress_row.addWidget(self.stage_label)
        progress_row.addWidget(self.progress, stretch=1)
        self._progress_widgets = QWidget()
        self._progress_widgets.setLayout(progress_row)
        self._progress_widgets.setVisible(False)
        layout.addWidget(self._progress_widgets)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self._set_editing_enabled(False)
        self._update_apply_button()

    def _show_progress(self, visible: bool) -> None:
        self._progress_widgets.setVisible(visible)
        if not visible:
            self.stage_label.setText("")
            self.progress.setRange(0, 100)
            self.progress.setValue(0)

    def _set_editing_enabled(self, on: bool) -> None:
        for widget in (
            self.trim_silence_btn,
            self.trim_repeats_btn,
            self.create_captions_btn,
            self.search_edit,
            self.search_prev_btn,
            self.search_next_btn,
            self.export_state_btn,
        ):
            widget.setEnabled(on)
        self.apply_btn.setEnabled(on and not self._timeline_batch_mode)

    def _status(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def _apply_mode(self) -> str:
        return config.normalize_apply_mode(self.cfg.get("apply_mode"))

    def _on_apply_mode_action(self) -> None:
        action = self._apply_action_group.checkedAction()
        if action is None:
            return
        mode = action.data()
        if not isinstance(mode, str):
            return
        self.cfg["apply_mode"] = config.normalize_apply_mode(mode)
        config.save_config(self.cfg)
        self._update_apply_button()

    def _update_apply_button(self) -> None:
        mode = self._apply_mode()
        self.apply_btn.setText(APPLY_LABELS.get(mode, APPLY_LABELS[APPLY_REPLACE]))
        action = self._apply_actions.get(mode)
        if action is not None:
            action.setChecked(True)
        # Size for the longest label so the menu-button never clips text.
        metrics = self.apply_btn.fontMetrics()
        text_w = max(metrics.horizontalAdvance(t) for t in APPLY_LABELS.values())
        self.apply_btn.setMinimumWidth(text_w + 52)  # padding + menu-button strip

    # ---- Resolve ----------------------------------------------------------

    def _connect_resolve(self) -> None:
        try:
            self.resolve.connect()
            mode = getattr(self.resolve, "mode", "direct")
            if mode == "bridge":
                self._status("Connected to DaVinci Resolve via Scripts bridge (Free/Studio)")
            else:
                self._status("Connected to DaVinci Resolve (direct)")
            self._load_clips()
        except ResolveError as e:
            self._status(str(e))
            QMessageBox.warning(self, "Captain", str(e))

    def _load_clips(self) -> None:
        if not self.resolve.connected:
            self._connect_resolve()
            if not self.resolve.connected:
                return
        try:
            self.clips = self.resolve.list_clips()
        except Exception as e:
            QMessageBox.warning(self, "Captain", f"Could not list clips:\n{e}")
            return
        prev_id = self.current_clip.clip_id if self.current_clip else None
        self.clip_combo.blockSignals(True)
        self.clip_combo.clear()
        for c in self.clips:
            label = f"[{c.track_type[0].upper()}{c.track_index}] {c.name}"
            self.clip_combo.addItem(label, c.clip_id)
        if prev_id:
            for i, c in enumerate(self.clips):
                if c.clip_id == prev_id:
                    self.clip_combo.setCurrentIndex(i)
                    break
        self.clip_combo.blockSignals(False)
        try:
            tname = self.resolve.timeline_name()
        except Exception:
            tname = "(unknown)"
        self._status(f"{len(self.clips)} clips found in '{tname}'")

    def _clip_label(self, clip: ClipInfo) -> str:
        return f"[{clip.track_type[0].upper()}{clip.track_index}] {clip.name}"

    def _find_clip_index(self, clip: ClipInfo) -> int:
        """Index in self.clips matching clip_id, else timeline/source/track."""
        for i, c in enumerate(self.clips):
            if c.clip_id == clip.clip_id:
                return i
        for i, c in enumerate(self.clips):
            if (
                c.track_type == clip.track_type
                and c.timeline_start_frame == clip.timeline_start_frame
                and c.source_start_frame == clip.source_start_frame
            ):
                return i
        return -1

    def _select_clip_in_combo(self, clip: ClipInfo) -> None:
        self._timeline_batch_mode = False
        idx = self._find_clip_index(clip)
        self.clip_combo.blockSignals(True)
        if idx >= 0:
            # Prefer the listed clip object so combo data stays consistent.
            self.current_clip = self.clips[idx]
            self._caption_anchor_clip = self.current_clip
            self.clip_combo.setCurrentIndex(idx)
        else:
            self.clips.append(clip)
            self.current_clip = clip
            self._caption_anchor_clip = clip
            self.clip_combo.addItem(self._clip_label(clip), clip.clip_id)
            self.clip_combo.setCurrentIndex(self.clip_combo.count() - 1)
        self.clip_combo.blockSignals(False)

    def _on_clip_combo_changed(self, index: int) -> None:
        if index < 0 or index >= len(self.clips):
            return
        if self._timeline_batch_mode:
            anchor_index = self._find_clip_index(self._caption_anchor_clip) if self._caption_anchor_clip else -1
            if anchor_index >= 0 and anchor_index != index:
                self.clip_combo.blockSignals(True)
                self.clip_combo.setCurrentIndex(anchor_index)
                self.clip_combo.blockSignals(False)
            return
        self.current_clip = self.clips[index]
        self._caption_anchor_clip = self.current_clip

    def _use_playhead_clip(self) -> None:
        if not self.resolve.connected:
            self._connect_resolve()
            if not self.resolve.connected:
                return
        try:
            # Warm the host clip cache / combo list.
            self._load_clips()
            clip = self.resolve.clip_under_playhead()
        except ResolveError as e:
            QMessageBox.warning(self, "Captain", str(e))
            return
        except Exception as e:
            QMessageBox.warning(self, "Captain", f"Could not read playhead clip:\n{e}")
            return
        self._select_clip_in_combo(clip)
        name = self.current_clip.name if self.current_clip else clip.name
        self._status(f"Using playhead clip: {name}")

    def _apply_typography_from_cfg(self) -> None:
        kwargs = dict(
            family=str(self.cfg.get("transcript_font_family") or ""),
            size=int(self.cfg.get("transcript_font_size", 14)),
            spacing=int(self.cfg.get("transcript_word_spacing", 0)),
            pad_x=int(self.cfg.get("transcript_word_pad_x", 1)),
            pad_y=int(self.cfg.get("transcript_word_pad_y", 2)),
        )
        self.view.apply_typography(**kwargs)
        self.script_view.apply_typography(**kwargs)

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        self.cfg.update(dlg.values())
        config.save_config(self.cfg)
        self._apply_typography_from_cfg()
        self.view.set_silence_thresholds(
            SILENCE_DISPLAY_MIN,
            self.cfg["silence_max_pause"],
        )
        self._status("Settings saved")

    # ---- transcription ------------------------------------------------------

    def _session_path(self, clip: ClipInfo) -> Path:
        key = hashlib.sha1(
            f"{clip.file_path}:{clip.clip_id}:{clip.timeline_start_frame}:"
            f"{clip.timeline_end_frame}:{clip.source_start_frame}:{clip.source_end_frame}".encode()
        ).hexdigest()[:16]
        return config.sessions_dir() / f"{key}.json"

    def _choose_load_source(self) -> str | None:
        """Ask whether to load the on-device session or import a file.

        Returns ``"saved"``, ``"import"``, or ``None`` if cancelled.
        """
        box = QMessageBox(self)
        box.setWindowTitle("Captain")
        box.setText(
            "Load the saved transcript from this device, or import a state file?"
        )
        saved_btn = box.addButton("Load saved", QMessageBox.ButtonRole.AcceptRole)
        import_btn = box.addButton("Import…", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is saved_btn:
            return "saved"
        if clicked is import_btn:
            return "import"
        return None

    def _load_transcript_file(self, path: Path) -> Transcript | None:
        """Load a Captain transcript JSON; show a warning and return None on error."""
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("Transcript file must be a JSON object")
            if "words" not in data or "duration" not in data:
                raise ValueError("Missing required fields: words and duration")
            if not isinstance(data["words"], list):
                raise ValueError("words must be a list")
            transcript = Transcript.from_json(text)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            QMessageBox.warning(
                self, "Captain", f"Could not import transcript state:\n{e}"
            )
            return None
        if not transcript.words:
            QMessageBox.warning(
                self,
                "Captain",
                "That file has no words. Apply may have nothing to keep.",
            )
        return transcript

    def _prompt_import_state(self) -> Transcript | None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Import State",
            "",
            "Captain State (*.json);;All Files (*)",
        )
        if not path:
            return None
        return self._load_transcript_file(Path(path))

    def _export_state_default_name(self) -> str:
        name = self.current_clip.name if self.current_clip else "transcript"
        safe = re.sub(r"[^\w.\-]+", "_", name).strip("._") or "transcript"
        return f"{safe}-captain.json"

    def _export_state(self) -> None:
        transcript = self.view.transcript
        if transcript is None:
            QMessageBox.information(self, "Captain", "No transcript to export.")
            return
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "Export State",
            self._export_state_default_name(),
            "Captain State (*.json);;All Files (*)",
        )
        if not path:
            return
        out = Path(path)
        if out.suffix.lower() != ".json":
            out = out.with_suffix(".json")
        try:
            transcript.save(out, clean=False)
        except OSError as e:
            QMessageBox.warning(self, "Captain", f"Could not export state:\n{e}")
            return
        self._status(f"Exported state to {out.name}")

    def _import_state(self) -> None:
        if self.current_clip is None:
            row = self.clip_combo.currentIndex()
            if 0 <= row < len(self.clips):
                self.current_clip = self.clips[row]
        if self.current_clip is None:
            QMessageBox.information(
                self,
                "Captain",
                "Select a clip first (Use Playhead Clip, or pick from the dropdown).",
            )
            return
        transcript = self._prompt_import_state()
        if transcript is None:
            return
        self._show_transcript(transcript)
        self._save_session(transcript)
        self._status(f"Imported state ({len(transcript.words)} words)")

    def _transcribe(self) -> None:
        row = self.clip_combo.currentIndex()
        if row < 0 or row >= len(self.clips):
            QMessageBox.information(
                self,
                "Captain",
                "Select a clip first (Use Playhead Clip, or pick from the dropdown).",
            )
            return
        self.current_clip = self.clips[row]
        self._caption_anchor_clip = self.current_clip
        self._timeline_batch_mode = False
        self._pre_batch_context = None

        session = self._session_path(self.current_clip)
        if session.exists():
            answer = QMessageBox.question(
                self,
                "Captain",
                "A saved transcript exists for this clip. Load it instead of "
                "re-transcribing?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                choice = self._choose_load_source()
                if choice is None:
                    return
                if choice == "saved":
                    self._show_transcript(Transcript.load(session, clean=False))
                    return
                transcript = self._prompt_import_state()
                if transcript is None:
                    return
                self._show_transcript(transcript)
                self._save_session(transcript)
                return

        self.transcribe_btn.setEnabled(False)
        self._show_progress(True)
        prompt = None
        if self._alignment is not None:
            prompt = self._alignment.vocabulary_prompt() or None
        elif self._script_tokens:
            prompt = AlignmentResult(
                script_tokens=self._script_tokens
            ).vocabulary_prompt() or None
        self.worker = TranscribeWorker(
            self.current_clip,
            self.transcriber,
            self.cfg["language"],
            self.resolve,
            initial_prompt=prompt,
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_transcribed)
        self.worker.failed.connect(self._on_transcribe_failed)
        self.worker.start()

    def _transcribe_timeline(self) -> None:
        if not self.resolve.connected:
            self._connect_resolve()
            if not self.resolve.connected:
                return
        if not self.clips:
            self._load_clips()
        candidates = list(self.clips)
        if not candidates:
            QMessageBox.information(self, "Transcribe Timeline", "The open timeline has no clips.")
            return
        has_path_backed_video = any(
            clip.track_type == "video" and clip.file_path for clip in candidates
        )

        dialog = QDialog(self)
        dialog.setWindowTitle("Transcribe Open Timeline")
        layout = QVBoxLayout(dialog)
        hint = QLabel(
            "For a compound clip, open it in its own timeline in Resolve first, then refresh Captain. "
            "Each selected child clip is transcribed from its source and merged at its timeline position. "
            "Select only clips that contain the dialogue you want."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        clip_list = QListWidget(dialog)
        for clip in candidates:
            item = QListWidgetItem(self._clip_label(clip))
            item.setData(Qt.ItemDataRole.UserRole, clip.clip_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = bool(clip.file_path) and (
                clip.track_type == "video" or not has_path_backed_video
            )
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
            if not clip.file_path:
                item.setToolTip("No source path; including it requires Resolve to render this child clip.")
            clip_list.addItem(item)
        layout.addWidget(clip_list)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        selected_ids = {
            clip_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(clip_list.count())
            if clip_list.item(i).checkState() == Qt.CheckState.Checked
        }
        selected = [clip for clip in candidates if clip.clip_id in selected_ids]
        if not selected:
            QMessageBox.information(self, "Transcribe Timeline", "Select at least one clip.")
            return
        selected.sort(key=lambda clip: (clip.timeline_start_frame, clip.track_index))
        fps = selected[0].fps
        if fps <= 0:
            QMessageBox.warning(self, "Transcribe Timeline", "Resolve reported an invalid timeline frame rate.")
            return
        start_frame = min(clip.timeline_start_frame for clip in selected)
        end_frame = max(clip.timeline_end_frame for clip in selected)
        anchor = next((clip for clip in selected if clip.track_type == "video"), selected[0])
        try:
            timeline_name = self.resolve.timeline_name()
        except Exception:
            timeline_name = "Open Timeline"
        digest = hashlib.sha1("|".join(clip.clip_id for clip in selected).encode()).hexdigest()[:16]
        aggregate = ClipInfo(
            clip_id=f"timeline:{digest}",
            name=timeline_name,
            track_type=anchor.track_type,
            track_index=anchor.track_index,
            timeline_start_frame=start_frame,
            timeline_end_frame=end_frame,
            source_start_frame=0,
            source_end_frame=end_frame - start_frame,
            file_path="",
            fps=fps,
        )
        self._pre_batch_context = (
            self.current_clip,
            self._caption_anchor_clip,
            self._timeline_batch_mode,
        )
        self.current_clip = aggregate
        self._caption_anchor_clip = anchor
        self._timeline_batch_mode = True
        anchor_index = self._find_clip_index(anchor)
        if anchor_index >= 0:
            self.clip_combo.blockSignals(True)
            self.clip_combo.setCurrentIndex(anchor_index)
            self.clip_combo.blockSignals(False)
        self.transcribe_btn.setEnabled(False)
        self.transcribe_timeline_btn.setEnabled(False)
        self.transcribe_timeline_btn.setEnabled(False)
        self._show_progress(True)
        prompt = None
        if self._alignment is not None:
            prompt = self._alignment.vocabulary_prompt() or None
        elif self._script_tokens:
            prompt = AlignmentResult(script_tokens=self._script_tokens).vocabulary_prompt() or None
        self.worker = TimelineTranscribeWorker(
            selected,
            self.transcriber,
            self.cfg["language"],
            self.resolve,
            start_frame,
            fps,
            timeline_name,
            initial_prompt=prompt,
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_transcribed)
        self.worker.failed.connect(self._on_transcribe_failed)
        self.worker.start()

    def _on_progress(self, fraction: float, message: str) -> None:
        if fraction < 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            # Keep a visible tick once download has started (int(0.5%) == 0).
            pct = int(fraction * 100)
            if 0 < fraction < 1 and pct == 0:
                pct = 1
            self.progress.setValue(pct)
        self.stage_label.setText(message)
        self._status(message)

    def _on_transcribed(self, transcript: Transcript) -> None:
        self.transcribe_btn.setEnabled(True)
        self.transcribe_timeline_btn.setEnabled(True)
        self._pre_batch_context = None
        self._show_progress(False)
        if self.view.transcript and self.view.transcript.script_text:
            transcript.script_text = self.view.transcript.script_text
        elif self._script_raw:
            transcript.script_text = self._script_raw
        self._save_session(transcript)
        self._show_transcript(transcript)

    def _on_transcribe_failed(self, message: str) -> None:
        self.transcribe_btn.setEnabled(True)
        self.transcribe_timeline_btn.setEnabled(True)
        if self._pre_batch_context is not None:
            self.current_clip, self._caption_anchor_clip, self._timeline_batch_mode = self._pre_batch_context
        self._pre_batch_context = None
        self._set_editing_enabled(self.view.transcript is not None)
        self._show_progress(False)
        QMessageBox.critical(self, "Captain", f"Transcription failed:\n{message}")

    def _show_transcript(self, transcript: Transcript) -> None:
        clip = self.current_clip
        if clip is not None:
            self.view.set_timeline_context(clip.timeline_start_frame, clip.fps)
        self.view.set_silence_thresholds(
            SILENCE_DISPLAY_MIN,
            self.cfg["silence_max_pause"],
        )
        self._apply_typography_from_cfg()
        self.view.set_transcript(transcript)
        self._set_editing_enabled(True)
        if transcript.script_text:
            self._script_raw = transcript.script_text
            self._script_tokens = parse_script(transcript.script_text)
            self._apply_alignment(transcript)
        elif self._script_tokens:
            # Script imported before transcript existed.
            if self._script_raw:
                transcript.script_text = self._script_raw
                self._save_session(transcript)
            self._apply_alignment(transcript)
        else:
            self._alignment = None
            self.view.clear_compare_statuses()
            self.script_panel.setVisible(False)
            self.clear_script_btn.setVisible(False)
        if self._search_bar.isVisible():
            self._on_search_text(self.search_edit.text())
        self._status(
            f"{len(transcript.words)} words • {transcript.duration:.1f}s • "
            + ("edit, then Create Captions" if self._timeline_batch_mode else "edit, then Apply")
        )

    def _create_captions(self) -> None:
        transcript = self.view.transcript
        clip = self.current_clip
        if transcript is None or clip is None:
            QMessageBox.information(
                self,
                "Create Captions",
                "Load a transcript and select its video clip first.",
            )
            return
        if clip.track_type != "video":
            QMessageBox.warning(
                self, "Create Captions", "Captions can only be added above a video clip."
            )
            return

        saved = normalize_caption_settings(self.cfg.get("caption_settings"))
        preview_texts = [
            " ".join(
                transcript.words[i].text.strip()
                for i in line.word_indices
                if i not in transcript.removed
            ).strip()
            for line in transcript.lines()
        ]
        preview_texts = [text for text in preview_texts if text]
        word_texts = [
            transcript.words[i].text.strip()
            for i in transcript.order
            if i not in transcript.removed
        ]

        timeline_info: dict = {"width": 1920, "height": 1080}
        preview = QPixmap()
        preview_source = "neutral"
        try:
            timeline_info = self.resolve.timeline_info()
            with tempfile.TemporaryDirectory(prefix="captain_caption_preview_") as tmp:
                image_path = str(Path(tmp) / "timeline-frame.png")
                try:
                    captured_path = self.resolve.capture_current_frame(image_path)
                except Exception as capture_error:
                    log.warning("Could not capture Resolve timeline frame: %s", capture_error)
                    captured_path = None
                if captured_path:
                    preview_path = (
                        captured_path if isinstance(captured_path, str) else image_path
                    )
                    preview = QPixmap(preview_path)
                    if not preview.isNull():
                        preview_source = "timeline"
                if preview.isNull() and clip.file_path:
                    frame_path = str(Path(tmp) / "clip-frame.png")
                    midpoint = clip.source_start_sec + clip.duration_sec / 2.0
                    try:
                        preview = QPixmap(extract_frame(clip.file_path, midpoint, frame_path))
                        if not preview.isNull():
                            preview_source = "clip"
                    except Exception as frame_error:
                        log.warning("Could not extract selected clip preview frame: %s", frame_error)
        except Exception as e:
            log.warning("Could not capture Resolve preview frame: %s", e)

        dlg = CaptionSettingsDialog(
            int(timeline_info.get("width", 1920)),
            int(timeline_info.get("height", 1080)),
            preview,
            preview_texts,
            word_texts,
            saved,
            parent=self,
            preview_source=preview_source,
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return

        settings = dlg.values()
        captions = build_caption_segments(
            transcript,
            clip,
            word_by_word=bool(settings["word_by_word"]),
            hold_to_next=bool(settings["hold_to_next"]),
        )
        if not captions:
            QMessageBox.information(
                self,
                "Create Captions",
                "There are no kept words to turn into captions.",
            )
            return
        self.cfg["caption_settings"] = settings
        config.save_config(self.cfg)
        try:
            count = self.resolve.create_captions(
                self._caption_anchor_clip or clip,
                [segment.to_dict() for segment in captions],
                settings,
            )
        except ResolveError as e:
            QMessageBox.critical(self, "Create Captions", str(e))
            return
        except Exception as e:
            log.exception("Caption generation failed")
            QMessageBox.critical(self, "Create Captions", f"Caption generation failed:\n{e}")
            return

        self._status(f"Created {count} Text+ caption clips on the timeline")
        QMessageBox.information(
            self,
            "Create Captions",
            f"Created {count} Text+ caption clips above '{clip.name}'.",
        )

    def _save_session(self, transcript: Transcript) -> None:
        if self.current_clip is not None:
            # Sessions keep words plus edit state (order, removed, silence_cuts)
            # and script_text so reopenings can restore the working cut.
            transcript.save(self._session_path(self.current_clip), clean=False)

    def _on_edited(self) -> None:
        transcript = self.view.transcript
        if transcript is None:
            return
        self._save_session(transcript)
        kept = len([i for i in transcript.order if i not in transcript.removed])
        self._status(f"{kept}/{len(transcript.words)} words kept")
        if self._search_bar.isVisible():
            self._on_search_text(self.search_edit.text())

    # ---- search -------------------------------------------------------------

    def _show_search(self) -> None:
        self._search_bar.setVisible(True)
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def _hide_search(self) -> None:
        if not self._search_bar.isVisible():
            return
        self.search_edit.clear()
        self.view.clear_search_matches()
        self._search_matches = []
        self._search_pos = -1
        self.search_count.setText("")
        self._search_bar.setVisible(False)
        self.view.setFocus()

    def _on_search_text(self, text: str) -> None:
        transcript = self.view.transcript
        if transcript is None or not text.strip():
            self._search_matches = []
            self._search_pos = -1
            self.view.clear_search_matches()
            self.search_count.setText("")
            return
        self._search_matches = transcript.find_matches(text)
        self.view.set_search_matches(self._search_matches)
        self._search_pos = -1
        n = len(self._search_matches)
        self.search_count.setText(f"0 / {n}" if n else "0 / 0")
        if n:
            self._find_next()

    def _find_next(self) -> None:
        if not self._search_bar.isVisible():
            return
        if not self._search_matches:
            return
        self._search_pos = (self._search_pos + 1) % len(self._search_matches)
        self._activate_search_hit()

    def _find_prev(self) -> None:
        if not self._search_bar.isVisible():
            return
        if not self._search_matches:
            return
        self._search_pos = (self._search_pos - 1) % len(self._search_matches)
        self._activate_search_hit()

    def _activate_search_hit(self) -> None:
        if not self._search_matches or self._search_pos < 0:
            return
        widx = self._search_matches[self._search_pos]
        self.view.select_word(widx)
        self.search_count.setText(
            f"{self._search_pos + 1} / {len(self._search_matches)}"
        )
        self._jump_to_word(widx)

    def _undo(self) -> None:
        if self.view.undo():
            self._status("Undid last edit")

    def _redo(self) -> None:
        if self.view.redo():
            self._status("Redid last edit")

    # ---- auto-trim -----------------------------------------------------------

    def _trim_silence(self) -> None:
        transcript = self.view.transcript
        if transcript is None:
            return
        self.view.push_history()
        cuts = find_silence_gaps(
            transcript,
            min_duration=SILENCE_DISPLAY_MIN,
            max_pause=self.cfg["silence_max_pause"],
        )
        transcript.silence_cuts = cuts
        self.view.refresh()
        self._save_session(transcript)
        total = sum(e - s for s, e in cuts)
        self._status(
            f"Marked {len(cuts)} silence gaps ({total:.1f}s) — "
            "struck markers will be trimmed; Delete toggles keep"
        )

    def _trim_repeats(self) -> None:
        transcript = self.view.transcript
        if transcript is None:
            return
        groups = find_repeats(
            transcript,
            max_ngram=self.cfg["repeat_max_ngram"],
            min_ngram=self.cfg.get("repeat_min_ngram", 4),
            min_pause=self.cfg.get("repeat_min_pause", 0.35),
        )
        if self._alignment is not None:
            groups = merge_repeat_groups(
                groups + find_script_retakes(transcript, self._alignment)
            )
        if not groups:
            self._status("No retakes found")
            return
        self.view.push_history()
        count = 0
        for group in groups:
            transcript.delete(group)
            count += len(group)
        self.view.refresh()
        self._save_session(transcript)
        self._status(f"Removed {count} words in {len(groups)} abandoned take(s)")
        if self._search_bar.isVisible():
            self._on_search_text(self.search_edit.text())

    # ---- script compare -----------------------------------------------------

    def _import_script(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Import Script",
            "",
            "Scripts (*.txt *.fountain *.srt *.vtt);;All Files (*)",
        )
        if not path:
            return
        try:
            raw, tokens = load_script(path)
        except OSError as e:
            QMessageBox.warning(self, "Captain", f"Could not read script:\n{e}")
            return
        if not tokens:
            QMessageBox.information(
                self, "Captain", "No spoken words found in that file."
            )
            return
        self._script_tokens = tokens
        self._script_raw = raw
        transcript = self.view.transcript
        if transcript is not None:
            transcript.script_text = raw
            self._save_session(transcript)
            self._apply_alignment(transcript)
        else:
            # Show script pane alone until a transcript exists.
            self.script_view.set_script(tokens)
            self.script_panel.setVisible(True)
            self.clear_script_btn.setVisible(True)
            self._status(f"Imported script ({len(tokens)} words) — transcribe a clip to compare")

    def _clear_script(self) -> None:
        self._script_tokens = []
        self._script_raw = ""
        self._alignment = None
        self.script_view.clear()
        self.script_panel.setVisible(False)
        self.clear_script_btn.setVisible(False)
        self.view.clear_compare_statuses()
        transcript = self.view.transcript
        if transcript is not None:
            transcript.script_text = ""
            self._save_session(transcript)
        self._status("Script cleared")

    def _apply_alignment(self, transcript: Transcript) -> None:
        if not self._script_tokens:
            return
        self._alignment = align_transcript(transcript, self._script_tokens)
        self.script_view.set_script(
            self._script_tokens, self._alignment.script_statuses()
        )
        self.view.set_compare_statuses(self._alignment.video_statuses())
        self.script_panel.setVisible(True)
        self.clear_script_btn.setVisible(True)
        stats = self._alignment.video_statuses()
        n_match = sum(1 for s in stats.values() if s == "match")
        n_extra = sum(1 for s in stats.values() if s == "extra")
        n_mis = sum(1 for s in stats.values() if s == "mismatch")
        n_miss = sum(
            1 for s in self._alignment.script_statuses().values() if s == "missing"
        )
        self._status(
            f"Compare: {n_match} match • {n_miss} missing • "
            f"{n_extra} extra • {n_mis} mismatch"
        )

    def _on_script_token(self, script_index: int) -> None:
        if self._syncing_selection or self._alignment is None:
            return
        self._syncing_selection = True
        try:
            self.script_view.select_token(script_index)
            video_map = self._alignment.script_to_video()
            widx = video_map.get(script_index)
            if widx is not None:
                self.view.select_word(widx)
                self._jump_to_word(widx)
        finally:
            self._syncing_selection = False

    def _on_transcript_word(self, word_index: int) -> None:
        self._jump_to_word(word_index)
        if self._syncing_selection or self._alignment is None:
            return
        self._syncing_selection = True
        try:
            script_map = self._alignment.video_to_script()
            sidx = script_map.get(word_index)
            if sidx is not None:
                self.script_view.select_token(sidx)
            else:
                self.script_view.clear_highlight()
        finally:
            self._syncing_selection = False

    # ---- playhead sync ---------------------------------------------------------

    def _jump_to_word(self, word_index: int) -> None:
        transcript = self.view.transcript
        if transcript is None or self.current_clip is None:
            return
        word = transcript.words[word_index]
        self._jump_to_media_second(word.start)

    def _jump_to_media_second(self, media_sec: float) -> None:
        if self.current_clip is None:
            return
        try:
            if self._timeline_batch_mode:
                frame = self.current_clip.timeline_start_frame + int(
                    media_sec * self.current_clip.fps
                )
                self.resolve.jump_to_timeline_frame(frame)
            else:
                self.resolve.jump_to_clip_second(
                    self.current_clip, self.current_clip.source_start_sec + media_sec
                )
        except ResolveError as e:
            self._status(str(e))

    # ---- apply --------------------------------------------------------------

    def _apply(self) -> None:
        transcript = self.view.transcript
        clip = self.current_clip
        if transcript is None or clip is None:
            return
        if self._timeline_batch_mode:
            QMessageBox.information(
                self,
                "Apply",
                "Timeline transcripts combine several source clips. Use Create Captions to place the merged transcript on the open timeline.",
            )
            return
        keep = transcript.keep_ranges()
        if not keep:
            QMessageBox.information(self, "Captain", "Nothing left to keep.")
            return
        frames = seconds_to_source_frames(keep, clip)
        mode = self._apply_mode()

        if mode == APPLY_NEW:
            self._apply_new_timeline(clip, frames)
        else:
            self._apply_replace(clip, frames, ripple=(mode == APPLY_RIPPLE))

    def _apply_replace(
        self, clip: ClipInfo, frames: list[tuple[int, int]], *, ripple: bool
    ) -> None:
        if ripple:
            detail = (
                "This ripple-deletes the clip and may shift later clips on "
                "other tracks."
            )
        else:
            detail = (
                "This deletes the clip without rippling (other tracks keep "
                "their timing; a gap may remain if the edit is shorter)."
            )
        answer = QMessageBox.question(
            self,
            "Captain",
            f"Replace '{clip.name}' on the current timeline with "
            f"{len(frames)} edited segment(s)?\n\n{detail}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            # Ensure host has TimelineItem cached.
            self.resolve.list_clips()
            ok = self.resolve.replace_clip_in_place(clip, frames, ripple=ripple)
            if ok:
                kind = "ripple" if ripple else "in place"
                self._status(
                    f"Replaced '{clip.name}' {kind} ({len(frames)} segments)"
                )
                QMessageBox.information(
                    self,
                    "Captain",
                    f"Replaced '{clip.name}' with {len(frames)} segment(s) "
                    "on the current timeline.",
                )
                self._load_clips()
            else:
                QMessageBox.critical(
                    self,
                    "Captain",
                    "In-place replace failed. See the log for details.",
                )
        except ResolveError as e:
            QMessageBox.critical(self, "Captain", str(e))

    def _apply_new_timeline(
        self, clip: ClipInfo, frames: list[tuple[int, int]]
    ) -> None:
        suffix = self.cfg.get("new_timeline_suffix", " [Captain]")
        try:
            existing = self.resolve.list_timeline_names()
        except Exception:
            existing = []
        default_name = next_captain_timeline_name(clip.name, existing, suffix=suffix)
        new_name, ok_name = QInputDialog.getText(
            self,
            "Captain",
            "Name for the new timeline:",
            text=default_name,
        )
        if not ok_name:
            return
        new_name = new_name.strip()
        if not new_name:
            QMessageBox.information(self, "Captain", "Timeline name cannot be empty.")
            return
        try:
            xml = build_fcp7_xml(clip, frames, new_name)
            xml_path = Path(tempfile.mkdtemp(prefix="captain_")) / "captain.xml"
            xml_path.write_text(xml)
            ok = self.resolve.import_timeline_xml(str(xml_path))
            if not ok:
                log.warning("XML import failed; falling back to AppendToTimeline")
                ok = self.resolve.assemble_append(clip, frames, new_name)
            if ok:
                self._status(f"Created timeline '{new_name}' ({len(frames)} segments)")
                QMessageBox.information(
                    self,
                    "Captain",
                    f"New timeline '{new_name}' created with {len(frames)} segments.\n"
                    "Your original timeline is untouched.",
                )
            else:
                QMessageBox.critical(
                    self,
                    "Captain",
                    "Assembly failed via both XML import and AppendToTimeline. "
                    "See the log for details.",
                )
        except ResolveError as e:
            QMessageBox.critical(self, "Captain", str(e))
