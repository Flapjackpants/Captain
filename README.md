# Captain

**Edit Resolve timelines like a document.**

Captain turns a clip into a word-level transcript, lets you delete, reorder, and
auto-trim like you’re editing text, then applies those cuts back into DaVinci
Resolve — replace in place, ripple, or a new timeline. Transcription runs
locally with Whisper. Works on **Resolve Free and Studio**.

## Why Us?

Video editing still forces you to scrub waveforms and guess where words start.
Captain flips that: speak once, read the transcript, cut with the keyboard.
Silence markers and retake detection handle manual cleanup that would've taken hours
in seconds. Powered by local, offline Whisper AI, so you know your data never leaves your
machine. 

## Install (macOS)

**Requirements:** DaVinci Resolve Free or Studio (not the Mac App Store build),
Python 3.11–3.13, FFmpeg on your PATH (`brew install ffmpeg`), ~2 GB for the
Whisper model on first use.

```bash
bash setupfiles/install-mac.sh
```

Fully quit and reopen Resolve, open a project, then:

**Workspace → Scripts → Captain**

Leave the script running until you quit Captain.

## Usage

1. **Workspace → Scripts → Captain**
2. Pick a clip (or **Use Playhead Clip**) → **Transcribe** → edit → **Apply**
3. Optional: **Import Script…** (`.txt` / `.fountain` / `.srt` / `.vtt`) to
   color-compare against what was said

**Edit:** Delete removes words (or toggles silence trim) · Cmd+X / Cmd+V
cut-paste · Cmd+Z / Cmd+Shift+Z undo/redo · click jumps the playhead ·
**Trim Silence** / **Remove Repeats** for auto-trims · silence markers (`…`)
show long gaps; struck = will be trimmed.

**Script colors:** white = match · blue = in script only · magenta = in video
only · red = mismatch · gray strikethrough = removed.

## Technical breakdown

```
Workspace → Scripts → Captain.lua   (Resolve Lua, live resolve handle)
        │
        ├─ JSON-RPC over files in
        │  ~/Library/Application Support/Captain/bridge/
        │
        └─ spawns Captain UI (venv + PySide6 + faster-whisper)
```

| Layer | Role |
| --- | --- |
| **Captain.lua** | Resolve Scripts entry; owns the live `resolve` API and the bridge loop |
| **Bridge** | File-based JSON-RPC between Lua and the Python UI |
| **faster-whisper** | Offline transcription with word-level timestamps |
| **Transcript model** | Ordered words, removed set, silence cuts; `keep_ranges()` drives assembly |
| **GUI** | PySide6 transcript editor: lines, silence markers, search, script compare |
| **Assemble** | Maps keep ranges to Resolve timeline ops (replace / ripple / new timeline) |

Apply modes: **replace in place** (default, non-ripple), **replace with ripple**,
or **new timeline** (`{clip} [Captain] {n}`).

Config lives at `~/Library/Application Support/Captain/config.json`
(Whisper model/device, language, silence thresholds, repeat n-gram size).

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -e ".[dev]"
.venv/bin/python -m pytest
```

*Not affiliated with Blackmagic Design.*
