"""Captain main window: clip picker, transcript editor, auto-trim, Apply."""

from __future__ import annotations

import hashlib
import logging
import tempfile
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..api import ClipInfo, ResolveError, create_resolve_handler
from ..assemble import build_fcp7_xml, seconds_to_source_frames
from ..engine import Transcriber, extract_audio
from ..transcript import Transcript, find_repeats, find_silence_gaps
from .transcript_view import TranscriptView

log = logging.getLogger("Captain.gui")

APPLY_REPLACE = "replace_in_place"
APPLY_NEW = "new_timeline"


class TranscribeWorker(QThread):
    progress = Signal(float, str)
    finished_ok = Signal(object)  # Transcript
    failed = Signal(str)

    def __init__(self, clip: ClipInfo, transcriber: Transcriber, language, parent=None):
        super().__init__(parent)
        self.clip = clip
        self.transcriber = transcriber
        self.language = language

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
        top.addWidget(self.playhead_btn)
        top.addWidget(self.clip_combo, stretch=1)
        top.addWidget(self.transcribe_btn)
        top.addWidget(self.refresh_btn)
        layout.addLayout(top)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
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
        layout.addLayout(search_row)

        QShortcut(QKeySequence("Ctrl+G"), self, self._find_next)
        QShortcut(QKeySequence("Ctrl+Shift+G"), self, self._find_prev)
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.search_edit.setFocus())

        self.view = TranscriptView()
        self.view.setObjectName("transcript")
        self.view.edited.connect(self._on_edited)
        self.view.word_activated.connect(self._jump_to_word)
        layout.addWidget(self.view, stretch=1)

        hint = QLabel(
            "Select words, then: Delete removes • Cmd/Ctrl+X cuts • "
            "Cmd/Ctrl+V pastes after the current word • Cmd/Ctrl+Z restores "
            "selection • double-click a word (or click a line timecode) jumps "
            "the Resolve playhead • Cmd/Ctrl+F search"
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

        bottom.addWidget(QLabel("Apply"))
        self.apply_mode_combo = QComboBox()
        self.apply_mode_combo.addItem("Replace clip in place", APPLY_REPLACE)
        self.apply_mode_combo.addItem("New timeline", APPLY_NEW)
        mode = self.cfg.get("apply_mode", APPLY_REPLACE)
        idx = self.apply_mode_combo.findData(mode)
        self.apply_mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.apply_mode_combo.currentIndexChanged.connect(self._on_apply_mode_changed)
        bottom.addWidget(self.apply_mode_combo)

        self.apply_btn = QPushButton("Apply → Replace Clip")
        self.apply_btn.setObjectName("accent")
        self.apply_btn.clicked.connect(self._apply)
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
            self.apply_mode_combo,
            self.search_edit,
            self.search_prev_btn,
            self.search_next_btn,
        ):
            widget.setEnabled(on)

    def _status(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def _apply_mode(self) -> str:
        data = self.apply_mode_combo.currentData()
        return data if isinstance(data, str) else APPLY_REPLACE

    def _on_apply_mode_changed(self) -> None:
        self.cfg["apply_mode"] = self._apply_mode()
        config.save_config(self.cfg)
        self._update_apply_button()

    def _update_apply_button(self) -> None:
        if self._apply_mode() == APPLY_NEW:
            self.apply_btn.setText("Apply → New Timeline")
        else:
            self.apply_btn.setText("Apply → Replace Clip")

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
        self.worker = TranscribeWorker(
            self.current_clip, self.transcriber, self.cfg["language"]
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
        self._on_search_text(self.search_edit.text())

    # ---- search -------------------------------------------------------------

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
        if not self._search_matches:
            return
        self._search_pos = (self._search_pos + 1) % len(self._search_matches)
        self._activate_search_hit()

    def _find_prev(self) -> None:
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

    # ---- auto-trim -----------------------------------------------------------

    def _trim_silence(self) -> None:
        transcript = self.view.transcript
        if transcript is None:
            return
        cuts = find_silence_gaps(
            transcript,
            min_duration=self.cfg["silence_min_duration"],
            max_pause=self.cfg["silence_max_pause"],
        )
        transcript.silence_cuts = cuts
        self._save_session(transcript)
        total = sum(e - s for s, e in cuts)
        self._status(f"Marked {len(cuts)} silence gaps ({total:.1f}s) for removal")

    def _trim_repeats(self) -> None:
        transcript = self.view.transcript
        if transcript is None:
            return
        groups = find_repeats(transcript, max_ngram=self.cfg["repeat_max_ngram"])
        count = 0
        for group in groups:
            transcript.delete(group)
            count += len(group)
        self.view.refresh()
        self._save_session(transcript)
        self._status(f"Removed {count} repeated words in {len(groups)} phrases")
        self._on_search_text(self.search_edit.text())

    # ---- playhead sync ---------------------------------------------------------

    def _jump_to_word(self, word_index: int) -> None:
        transcript = self.view.transcript
        if transcript is None or self.current_clip is None:
            return
        word = transcript.words[word_index]
        try:
            self.resolve.jump_to_clip_second(
                self.current_clip, self.current_clip.source_start_sec + word.start
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

        if mode == APPLY_REPLACE:
            self._apply_replace(clip, frames)
        else:
            self._apply_new_timeline(clip, frames)

    def _apply_replace(self, clip: ClipInfo, frames: list[tuple[int, int]]) -> None:
        answer = QMessageBox.question(
            self,
            "Captain",
            f"Replace '{clip.name}' on the current timeline with "
            f"{len(frames)} edited segment(s)?\n\n"
            "This modifies the current timeline (ripple delete + insert).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            # Ensure host has TimelineItem cached.
            self.resolve.list_clips()
            ok = self.resolve.replace_clip_in_place(clip, frames)
            if ok:
                self._status(
                    f"Replaced '{clip.name}' in place ({len(frames)} segments)"
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
        new_name = f"{self.resolve.timeline_name()}{self.cfg['new_timeline_suffix']}"
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
