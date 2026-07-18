"""Captain main window: clip picker, transcript editor, auto-trim, Apply."""

from __future__ import annotations

import hashlib
import logging
import tempfile
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
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
from ..compare import (
    AlignmentResult,
    align_transcript,
    find_script_retakes,
    load_script,
    merge_repeat_groups,
    parse_script,
)
from ..engine import Transcriber, extract_audio
from ..transcript import Transcript, find_repeats, find_silence_gaps
from .script_view import ScriptView
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
        parent=None,
        *,
        initial_prompt: str | None = None,
    ):
        super().__init__(parent)
        self.clip = clip
        self.transcriber = transcriber
        self.language = language
        self.initial_prompt = initial_prompt

    def run(self) -> None:
        try:
            self.progress.emit(0.0, "Extracting audio...")
            wav = extract_audio(
                self.clip.file_path,
                start_sec=self.clip.source_start_sec,
                duration_sec=self.clip.duration_sec,
            )
            transcript = self.transcriber.transcribe(
                wav,
                language=self.language,
                progress=lambda f, m: self.progress.emit(f, m),
                initial_prompt=self.initial_prompt,
            )
            self.finished_ok.emit(transcript)
        except Exception as e:  # surfaced to the user in the UI
            log.error("Transcription failed: %s", traceback.format_exc())
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
        self.transcribe_btn = QPushButton("Transcribe")
        self.transcribe_btn.clicked.connect(self._transcribe)
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
        top.addWidget(self.playhead_btn)
        top.addWidget(self.clip_combo, stretch=1)
        top.addWidget(self.transcribe_btn)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.import_script_btn)
        top.addWidget(self.clear_script_btn)
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
        self.view.edited.connect(self._on_edited)
        self.view.word_activated.connect(self._on_transcript_word)
        self.view.time_activated.connect(self._jump_to_media_second)

        self.script_view = ScriptView()
        self.script_view.token_activated.connect(self._on_script_token)
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
            "Select words, then: Delete removes • Cmd/Ctrl+X cuts • "
            "Cmd/Ctrl+V pastes • Cmd/Ctrl+Z undo • Cmd/Ctrl+Shift+Z redo • "
            "click a word/timecode jumps the playhead • Trim Silence shows "
            "··· markers (Delete a marker to keep that silence) • Cmd/Ctrl+F search • "
            "Import Script for color compare (white=match, blue=missing, "
            "magenta=extra, red=mismatch, gray=removed)"
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
        bottom.addWidget(self.trim_silence_btn)
        bottom.addWidget(self.trim_repeats_btn)
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
            self.apply_btn,
            self.search_edit,
            self.search_prev_btn,
            self.search_next_btn,
        ):
            widget.setEnabled(on)

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
            self.clips = [c for c in self.resolve.list_clips() if c.file_path]
        except Exception as e:
            QMessageBox.warning(self, "Captain", f"Could not list clips:\n{e}")
            return
        prev_id = self.current_clip.clip_id if self.current_clip else None
        self.clip_combo.clear()
        for c in self.clips:
            label = f"[{c.track_type[0].upper()}{c.track_index}] {c.name}"
            self.clip_combo.addItem(label)
        if prev_id:
            for i, c in enumerate(self.clips):
                if c.clip_id == prev_id:
                    self.clip_combo.setCurrentIndex(i)
                    break
        try:
            tname = self.resolve.timeline_name()
        except Exception:
            tname = "(unknown)"
        self._status(f"{len(self.clips)} clips found in '{tname}'")

    def _select_clip_in_combo(self, clip: ClipInfo) -> None:
        for i, c in enumerate(self.clips):
            if c.clip_id == clip.clip_id:
                self.clip_combo.setCurrentIndex(i)
                return
        # Not in list (e.g. filtered); keep current_clip separately and show label.
        self.clips.append(clip)
        self.clip_combo.addItem(
            f"[{clip.track_type[0].upper()}{clip.track_index}] {clip.name}"
        )
        self.clip_combo.setCurrentIndex(self.clip_combo.count() - 1)

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
        self.current_clip = clip
        self._select_clip_in_combo(clip)
        self._status(f"Using playhead clip: {clip.name}")

    # ---- transcription ------------------------------------------------------

    def _session_path(self, clip: ClipInfo) -> Path:
        key = hashlib.sha1(
            f"{clip.file_path}:{clip.source_start_frame}:{clip.source_end_frame}".encode()
        ).hexdigest()[:16]
        return config.sessions_dir() / f"{key}.json"

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
                self._show_transcript(Transcript.load(session))
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
        self._show_progress(False)
        if self.view.transcript and self.view.transcript.script_text:
            transcript.script_text = self.view.transcript.script_text
        elif self._script_raw:
            transcript.script_text = self._script_raw
        self._save_session(transcript)
        self._show_transcript(transcript)

    def _on_transcribe_failed(self, message: str) -> None:
        self.transcribe_btn.setEnabled(True)
        self._show_progress(False)
        QMessageBox.critical(self, "Captain", f"Transcription failed:\n{message}")

    def _show_transcript(self, transcript: Transcript) -> None:
        clip = self.current_clip
        if clip is not None:
            self.view.set_timeline_context(clip.timeline_start_frame, clip.fps)
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
            "edit, then Apply"
        )

    def _save_session(self, transcript: Transcript) -> None:
        if self.current_clip is not None:
            transcript.save(self._session_path(self.current_clip))

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
            min_duration=self.cfg["silence_min_duration"],
            max_pause=self.cfg["silence_max_pause"],
        )
        transcript.silence_cuts = cuts
        self.view.refresh()
        self._save_session(transcript)
        total = sum(e - s for s, e in cuts)
        self._status(
            f"Marked {len(cuts)} silence gaps ({total:.1f}s) — "
            "Delete a ··· marker to keep that silence"
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
