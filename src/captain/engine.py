"""Audio extraction and offline transcription (faster-whisper)."""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

from .transcript import Transcript, Word

log = logging.getLogger("Captain.engine")

# fraction 0..1 = determinate progress; fraction < 0 = indeterminate (busy)
ProgressFn = Callable[[float, str], None]

# Files faster-whisper needs from a HF model repo (mirrors its own allow list).
_MODEL_FILE_PATTERNS = (
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
)

# GUI apps launched from Resolve inherit a minimal PATH without Homebrew.
_EXTRA_BIN_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def _find_tool(name: str) -> str | None:
    """Locate an executable on PATH or in common Homebrew locations."""
    found = shutil.which(name)
    if found:
        return found
    for directory in _EXTRA_BIN_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def find_ffmpeg() -> str:
    path = _find_tool("ffmpeg")
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
    ffprobe = _find_tool("ffprobe")
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


def _repo_for_model(model_name: str) -> str:
    if "/" in model_name:
        return model_name
    return f"Systran/faster-whisper-{model_name}"


def _wanted_file(filename: str) -> bool:
    return any(fnmatch.fnmatch(filename, p) for p in _MODEL_FILE_PATTERNS)


def _dir_bytes(directory: Path) -> int:
    if not directory.exists():
        return 0
    return sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())


def _model_downloaded(directory: Path) -> bool:
    return (directory / "model.bin").is_file() and (directory / "config.json").is_file()


def _hub_snapshot_dir(models_dir: str | Path, repo_id: str) -> Path | None:
    """Return a complete HF hub-cache snapshot under models_dir, if any."""
    hub = Path(models_dir) / f"models--{repo_id.replace('/', '--')}"
    snapshots = hub / "snapshots"
    if not snapshots.is_dir():
        return None
    for snap in snapshots.iterdir():
        if snap.is_dir() and _model_downloaded(snap):
            return snap
    return None


def find_local_model(model_name: str, models_dir: str) -> str | None:
    """Locate an already-downloaded model (flat local_dir or HF hub cache)."""
    repo_id = _repo_for_model(model_name)
    flat = Path(models_dir) / repo_id.replace("/", "--")
    if _model_downloaded(flat):
        return str(flat)
    snap = _hub_snapshot_dir(models_dir, repo_id)
    return str(snap) if snap is not None else None


def download_model(
    model_name: str,
    models_dir: str,
    progress: ProgressFn | None = None,
) -> str:
    """Download a Whisper model to models_dir, reporting byte-level progress.

    Returns the local directory to pass to WhisperModel. Progress is measured
    by polling on-disk size against the total reported by the Hugging Face API,
    which works regardless of huggingface_hub's internal tqdm handling.
    """
    repo_id = _repo_for_model(model_name)
    existing = find_local_model(model_name, models_dir)
    if existing:
        return existing

    from huggingface_hub import HfApi, snapshot_download

    total_bytes = 0
    try:
        info = HfApi().model_info(repo_id, files_metadata=True)
        total_bytes = sum(
            s.size or 0 for s in info.siblings if _wanted_file(s.rfilename)
        )
    except Exception:
        log.warning("Could not fetch model size for %s", repo_id, exc_info=True)

    stop = threading.Event()
    hub_cache = Path(models_dir) / f"models--{repo_id.replace('/', '--')}"

    if progress and total_bytes:
        def _poll() -> None:
            while not stop.wait(0.3):
                # Absolute on-disk size (includes incomplete blobs as they grow).
                done = min(_dir_bytes(hub_cache), total_bytes)
                fraction = min(done / total_bytes, 0.99) if total_bytes else 0.0
                progress(
                    fraction,
                    f"Downloading Whisper model '{model_name}'... "
                    f"{done / 1e6:,.0f} / {total_bytes / 1e6:,.0f} MB",
                )

        threading.Thread(target=_poll, daemon=True).start()
    elif progress:
        progress(-1.0, f"Downloading Whisper model '{model_name}'...")

    try:
        # Prefer hub-cache layout so we share storage with faster-whisper's
        # default downloader and can resume across runs.
        snapshot_download(
            repo_id,
            allow_patterns=list(_MODEL_FILE_PATTERNS),
            cache_dir=models_dir,
        )
    finally:
        stop.set()

    resolved = find_local_model(model_name, models_dir)
    if not resolved:
        raise RuntimeError(
            f"Whisper model '{model_name}' download finished but files were not found "
            f"under {models_dir}."
        )
    return resolved


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
            from faster_whisper import WhisperModel

            model_ref = self.model_name
            if self.models_dir:
                try:
                    model_ref = download_model(
                        self.model_name, self.models_dir, progress
                    )
                except Exception:
                    # Fall back to faster-whisper's own downloader (no progress).
                    log.warning("Model pre-download failed", exc_info=True)
                    if progress:
                        progress(-1.0, f"Downloading Whisper model "
                                       f"'{self.model_name}'...")
            if progress:
                progress(-1.0, "Loading Whisper model into memory...")
            self._model = WhisperModel(
                model_ref,
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
        for seg_id, seg in enumerate(segments):
            for w in seg.words or []:
                text = w.word.strip()
                if not text:
                    continue
                words.append(
                    Word(
                        index=len(words),
                        text=text,
                        start=float(w.start),
                        end=float(w.end),
                        segment_id=seg_id,
                    )
                )
            if progress and duration > 0:
                progress(min(0.05 + 0.95 * (seg.end / duration), 0.99),
                         f"Transcribing... {seg.end:.0f}s / {duration:.0f}s")

        if progress:
            progress(1.0, f"Done: {len(words)} words")
        log.info("Transcribed %s: %d words, %.1fs", audio_path, len(words), duration)
        return Transcript(words=words, duration=duration, source_path=audio_path)
