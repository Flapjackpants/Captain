"""Paths, settings persistence, and logging setup."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

APP_NAME = "Captain"

DEFAULTS: dict[str, Any] = {
    "whisper_model": "small",
    "whisper_device": "auto",
    "whisper_compute_type": "int8",
    "language": None,  # autodetect
    "silence_min_duration": 0.8,   # gaps >= this are considered trimmable silence
    "silence_max_pause": 0.25,     # silence retained at each trimmed junction
    "repeat_min_ngram": 4,         # shortest phrase treated as a retake
    "repeat_max_ngram": 8,
    "repeat_min_pause": 0.35,      # pause between copies to count as a retake
    "new_timeline_suffix": " [Captain]",
    # "replace_in_place" (non-ripple) | "replace_ripple" | "new_timeline"
    "apply_mode": "replace_in_place",
    # Transcript / script typography
    "transcript_font_family": "",  # empty = Qt default
    "transcript_font_size": 14,
    "transcript_word_spacing": 0,
    "transcript_word_pad_x": 1,
    "transcript_word_pad_y": 2,
}

APPLY_MODES = frozenset({"replace_in_place", "replace_ripple", "new_timeline"})


def normalize_apply_mode(mode: Any) -> str:
    """Return a valid apply_mode; unknown values fall back to the default."""
    if mode in APPLY_MODES:
        return str(mode)
    return str(DEFAULTS["apply_mode"])


def data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def sessions_dir() -> Path:
    d = data_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def models_dir() -> Path:
    d = data_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_path() -> Path:
    return data_dir() / "config.json"


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(_config_path().read_text()))
    except (OSError, ValueError):
        pass
    cfg["apply_mode"] = normalize_apply_mode(cfg.get("apply_mode"))
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    _config_path().write_text(json.dumps(cfg, indent=2))


def setup_logging() -> logging.Logger:
    log_path = data_dir() / "captain.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler()],
    )
    return logging.getLogger(APP_NAME)
