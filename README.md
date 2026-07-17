# Captain

Text-based timeline editing for DaVinci Resolve, powered by local, offline AI.

Captain transcribes a clip from your timeline with word-level timestamps
(faster-whisper), lets you edit the audio like a document — delete words,
cut/paste them around, auto-trim silence and repeated retakes — and then
assembles a **new** timeline in Resolve with all your cuts applied. Your
original timeline is never modified.

Works on **Resolve Free and Studio**.

## Launch model (same as BadWords)

There is **no lower-friction Free plugin API** than Workspace → Scripts.
[BadWords](https://github.com/veritus-git/BadWords) does the same thing: its
installer drops a thin wrapper into
`Fusion/Scripts/Utility/`, then you run **Workspace → Scripts → BadWords**
after restarting Resolve. Captain mirrors that with a single **Captain** entry
(`Captain.lua`) so it shows up on Free even without a python.org Python.

Studio-only alternatives (Workflow Integrations / Electron) still open a
separate window and do not remove the one-click Scripts step for Free users.

## Requirements

- DaVinci Resolve (**Free or Studio**) from
  [blackmagicdesign.com](https://www.blackmagicdesign.com/products/davinciresolve/)
  — **not** the Mac App Store build
- Python 3.11–3.13 for the Captain app venv (Homebrew is fine)
- FFmpeg on your PATH (`brew install ffmpeg`)
- ~2 GB disk for the Whisper model (first transcription)

You do **not** need the Studio “External scripting” preference.

## Install (macOS)

```bash
bash setupfiles/install-mac.sh
```

Then **fully quit and reopen Resolve**, open a project, and run:

**Workspace → Scripts → Captain**

That one entry starts the Resolve bridge and opens the Captain window. Leave
the script running until you quit Captain.

## Usage

1. **Workspace → Scripts → Captain**
2. Pick a clip → **Transcribe** → edit words → **Apply → New Timeline**

Edit shortcuts: Delete removes, Cmd+X / Cmd+V cut-paste words, Cmd+Z restores
selection, double-click jumps the playhead. **Trim Silence** / **Remove
Repeats** mark or apply auto-trims.

## How Free compatibility works

```
Workspace → Scripts → Captain.lua   (Resolve Lua, has live resolve)
        │
        ├─ file JSON-RPC in ~/Library/Application Support/Captain/bridge/
        │
        └─ spawns → Captain UI (venv + PySide6 + Whisper)
```

## Configuration

`~/Library/Application Support/Captain/config.json`:

| Key | Default | Meaning |
| --- | --- | --- |
| `whisper_model` | `small` | `base`, `small`, `medium`, `large-v3` |
| `whisper_device` | `auto` | `cpu`, `cuda`, or `auto` |
| `language` | `null` | Force a language code, or autodetect |
| `silence_min_duration` | `0.8` | Gaps ≥ this many seconds are trimmable |
| `silence_max_pause` | `0.25` | Silence kept at each trimmed junction |
| `repeat_max_ngram` | `8` | Longest repeated phrase length to detect |

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -e ".[dev]"
.venv/bin/python -m pytest
```

*Not affiliated with Blackmagic Design.*
