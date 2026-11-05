# Changelog

## v1.1.0 (2026-11-04)

### New features

**Captions**

- **Create Captions** turns the transcript into Text+ title clips on an available video track.
- Style captions in a live preview: font, size, alignment, spacing, layout, scale, rotation, and outline.
- Options include auto-fit to the frame, word-by-word, hold through silence, linked scale, and write-on.
- Before your first captions, drag Text+ from Effects → Titles into a `Captain` bin in the Media Pool.

**Transcription**

- **Transcribe Timeline…** transcribes each selected clip on the open timeline and merges the results by timeline position, which covers the children of a compound clip.
- Fusion and compound clips can be transcribed using audio rendered from their timeline range.
- The default Whisper model is now `large-v3-turbo`. Settings has a picker for `small`, `medium`, `large-v3-turbo`, and `large-v3`.

**Transcript editing**

- A caret moves between words with the arrow keys, Home, and End.
- Return splits a line at the caret. Backspace at the start of a line merges it into the previous line. Right-click a line for merge and split.
- Line breaks can be merged and now survive word moves.
- Delete toggles trimmed words between trimmed and not trimmed.
- Deleting a mixed selection now removes every item. It restores only when everything selected is already removed.

**Silence and retakes**

- **Remove Repeats** now catches false starts that the next phrase re-says, even with slightly different words.
- Settings switches repeat detection between Smart (the default) and Exact, and sets retake sensitivity.
- Silence markers are now much more granular, with one for every 0.1 seconds.
- Settings has new "Keep after word" and "Keep before word" padding for silence trims.

**Sessions**

- **Export State…** and **Import State…** save and reload a transcription session, including its edits.

**Install**

- One-line install for macOS.

### Fixes

- Audio and video are re-linked correctly after applying changes, including after a ripple replace.
- Silence trimming no longer clips the ends of words.
- Selecting a range now holds the original selected word.
- Moving or deleting the checkout no longer leaves a stale path. The installer rebuilds a virtualenv that a moved app folder has broken.
- Pinned `av<19`, because PyAV 19 removed an argument that faster-whisper passes to `av.open()`.
