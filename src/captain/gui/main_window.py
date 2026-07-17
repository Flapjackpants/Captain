"""Captain main window: clip picker, transcript editor, auto-trim, Apply."""

from __future__ import annotations

import hashlib
import logging
import tempfile
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
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
        self.resize(900, 640)

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

        self._build_ui()
        self._connect_resolve()

    # ---- UI --------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)

        top = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh Clips")
        self.refresh_btn.clicked.connect(self._load_clips)
        self.clip_combo = QComboBox()
        self.clip_combo.setMinimumWidth(320)
        self.transcribe_btn = QPushButton("Transcribe")
        self.transcribe_btn.clicked.connect(self._transcribe)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.clip_combo, stretch=1)
        top.addWidget(self.transcribe_btn)
        layout.addLayout(top)

        self.view = TranscriptView()
        self.view.edited.connect(self._on_edited)
        self.view.word_activated.connect(self._jump_to_word)
        layout.addWidget(self.view, stretch=1)

        hint = QLabel(
            "Select words, then: Delete removes • Cmd/Ctrl+X cuts • "
            "Cmd/Ctrl+V pastes after the current word • Cmd/Ctrl+Z restores "
            "selection • double-click jumps the Resolve playhead"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        layout.addWidget(hint)

        bottom = QHBoxLayout()
        self.trim_silence_btn = QPushButton("Trim Silence")
        self.trim_silence_btn.clicked.connect(self._trim_silence)
        self.trim_repeats_btn = QPushButton("Remove Repeats")
        self.trim_repeats_btn.clicked.connect(self._trim_repeats)
        self.apply_btn = QPushButton("Apply → New Timeline")
        self.apply_btn.setStyleSheet("font-weight: bold;")
        self.apply_btn.clicked.connect(self._apply)
        bottom.addWidget(self.trim_silence_btn)
        bottom.addWidget(self.trim_repeats_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.apply_btn)
        layout.addLayout(bottom)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self._set_editing_enabled(False)

    def _set_editing_enabled(self, on: bool) -> None:
        for widget in (self.trim_silence_btn, self.trim_repeats_btn, self.apply_btn):
            widget.setEnabled(on)

    def _status(self, message: str) -> None:
        self.statusBar().showMessage(message)

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
        except ResolveError as e:
            QMessageBox.warning(self, "Captain", str(e))
            return
        self.clip_combo.clear()
        for c in self.clips:
            label = f"[{c.track_type[0].upper()}{c.track_index}] {c.name}"
            self.clip_combo.addItem(label)
        self._status(f"{len(self.clips)} clips found in '{self.resolve.timeline_name()}'")

    # ---- transcription ------------------------------------------------------

    def _session_path(self, clip: ClipInfo) -> Path:
        key = hashlib.sha1(
            f"{clip.file_path}:{clip.source_start_frame}:{clip.source_end_frame}".encode()
        ).hexdigest()[:16]
        return config.sessions_dir() / f"{key}.json"

    def _transcribe(self) -> None:
        row = self.clip_combo.currentIndex()
        if row < 0 or row >= len(self.clips):
            QMessageBox.information(self, "Captain", "Select a clip first.")
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
        self.progress.setVisible(True)
        self.worker = TranscribeWorker(
            self.current_clip, self.transcriber, self.cfg["language"]
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_transcribed)
        self.worker.failed.connect(self._on_transcribe_failed)
        self.worker.start()

    def _on_progress(self, fraction: float, message: str) -> None:
        self.progress.setValue(int(fraction * 100))
        self._status(message)

    def _on_transcribed(self, transcript: Transcript) -> None:
        self.transcribe_btn.setEnabled(True)
        self.progress.setVisible(False)
        self._save_session(transcript)
        self._show_transcript(transcript)

    def _on_transcribe_failed(self, message: str) -> None:
        self.transcribe_btn.setEnabled(True)
        self.progress.setVisible(False)
        QMessageBox.critical(self, "Captain", f"Transcription failed:\n{message}")

    def _show_transcript(self, transcript: Transcript) -> None:
        self.view.set_transcript(transcript)
        self._set_editing_enabled(True)
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
                    self, "Captain",
                    f"New timeline '{new_name}' created with {len(frames)} segments.\n"
                    "Your original timeline is untouched.",
                )
            else:
                QMessageBox.critical(
                    self, "Captain",
                    "Assembly failed via both XML import and AppendToTimeline. "
                    "See the log for details.",
                )
        except ResolveError as e:
            QMessageBox.critical(self, "Captain", str(e))
