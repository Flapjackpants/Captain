"""Audio extraction and offline transcription (faster-whisper)."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from .transcript import Transcript, Word

log = logging.getLogger("Captain.engine")

ProgressFn = Callable[[float, str], None]  # (fraction 0..1, message)


def find_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError(
            "FFmpeg not found on PATH. Install it (e.g. `brew install ffmpeg`) "
            "and restart Captain."
        )
    return path


def extract_audio(
    media_path: str,
    start_sec: float | None = None,
    duration_sec: float | None = None,
    out_path: str | None = None,
) -> str:
    """Extract mono 16 kHz WAV from a media file (optionally a sub-range)."""
    ffmpeg = find_ffmpeg()
    if out_path is None:
        out_path = tempfile.mktemp(suffix=".wav", prefix="captain_")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    if start_sec:
        cmd += ["-ss", f"{start_sec:.3f}"]
    cmd += ["-i", media_path]
    if duration_sec:
        cmd += ["-t", f"{duration_sec:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out_path]
    log.info("Extracting audio: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out_path


def probe_duration(media_path: str) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found on PATH (comes with FFmpeg).")
    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            media_path,
        ],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


class Transcriber:
    """Wraps a lazily-loaded faster-whisper model."""

    def __init__(
        self,
        model_name: str = "small",
        device: str = "auto",
        compute_type: str = "int8",
        models_dir: str | None = None,
    ):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.models_dir = models_dir
        self._model = None

    def _load(self, progress: ProgressFn | None = None):
        if self._model is None:
            if progress:
                progress(0.0, f"Loading Whisper model '{self.model_name}' "
                              "(first run downloads it)...")
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
                download_root=self.models_dir,
            )
        return self._model

    def transcribe(
        self,
        audio_path: str,
        language: str | None = None,
        progress: ProgressFn | None = None,
    ) -> Transcript:
        model = self._load(progress)
        duration = probe_duration(audio_path)
        if progress:
            progress(0.05, "Transcribing...")

        segments, _info = model.transcribe(
            audio_path,
            language=language,
            word_timestamps=True,
            vad_filter=True,
        )

        words: list[Word] = []
        for seg in segments:
            for w in seg.words or []:
                words.append(
                    Word(
                        index=len(words),
                        text=w.word.strip(),
                        start=float(w.start),
                        end=float(w.end),
                    )
                )
            if progress and duration > 0:
                progress(min(0.05 + 0.95 * (seg.end / duration), 0.99),
                         f"Transcribing... {seg.end:.0f}s / {duration:.0f}s")

        if progress:
            progress(1.0, f"Done: {len(words)} words")
        log.info("Transcribed %s: %d words, %.1fs", audio_path, len(words), duration)
        return Transcript(words=words, duration=duration, source_path=audio_path)
