# Captain

Text-based timeline editing for DaVinci Resolve, powered by local, offline AI.

Captain transcribes a clip from your timeline with word-level timestamps
(faster-whisper), lets you edit the audio like a document — delete words,
cut/paste them around, auto-trim silence and repeated retakes — and then
assembles a **new** timeline in Resolve with all your cuts applied. Your
original timeline is never modified.

## Requirements

- DaVinci Resolve (Free or Studio) installed from
  [blackmagicdesign.com](https://www.blackmagicdesign.com/products/davinciresolve/)
  — **not** the Mac App Store version (it lacks scripting support)
- Scripting enabled in Resolve: Preferences > System > General >
  External scripting using = **Local**
- Python 3.11–3.13
- FFmpeg on your PATH (`brew install ffmpeg`)
- ~2 GB disk for the Whisper model (downloaded on first transcription)

## Install (macOS)

```bash
bash setupfiles/install-mac.sh
```

This creates a virtualenv, installs dependencies, and registers the launcher
with Resolve.

## Usage

1. Open DaVinci Resolve with a project and timeline.
2. **Workspace > Scripts > Captain**
3. Pick a clip from the dropdown and hit **Transcribe** (the first run
   downloads the Whisper model and is slow; later runs are fast).
4. Edit the transcript:
   - Select words, press **Delete** to remove them
   - **Cmd+X** cuts words, click a destination word, **Cmd+V** pastes after it
   - **Cmd+Z** restores the selected removed words
   - Double-click a word to jump the Resolve playhead there
   - **Trim Silence** marks long pauses for removal
   - **Remove Repeats** detects immediately repeated phrases (retakes) and
     removes the first take
5. Hit **Apply → New Timeline**. Captain builds an FCP7 XML cutlist and
   imports it as a new timeline in a "Captain" bin (falling back to direct
   timeline assembly if the import fails).

Transcripts are cached per clip under
`~/Library/Application Support/Captain/sessions/`, so reopening a clip offers
to load the saved edit session instead of re-transcribing.

## Configuration

`~/Library/Application Support/Captain/config.json`:

| Key | Default | Meaning |
| --- | --- | --- |
| `whisper_model` | `small` | Whisper size: `base`, `small`, `medium`, `large-v3` |
| `whisper_device` | `auto` | `cpu`, `cuda`, or `auto` |
| `language` | `null` | Force a language code, or autodetect |
| `silence_min_duration` | `0.8` | Gaps ≥ this many seconds are trimmable |
| `silence_max_pause` | `0.25` | Silence kept at each trimmed junction |
| `repeat_max_ngram` | `8` | Longest repeated phrase length to detect |

## Roadmap

- Phase 2: script import + colored compare view (match / missing / extra /
  incorrect / removed)
- Phase 3: auto-captions (word-by-word or by section) with SRT export and
  Text+ effects
- Phase 4: local AI voice — type words into the transcript and synthesize
  them with a voice cloned from imported audio or the timeline itself

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m captain.main   # requires Resolve running
```

*Not affiliated with Blackmagic Design.*
# Captain
