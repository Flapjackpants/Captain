"""Transcript data model and edit operations.

The transcript is a list of immutable timed words. Edits are expressed as:
  - a set of removed word indices
  - an output order (list of word indices), which differs from source order
    only when the user cuts and pastes words elsewhere
  - a list of silence cut ranges (seconds) to excise inside kept spans

Applying an edit reduces to a list of (start, end) keep-ranges in output
order, which the assembler turns into a Resolve timeline (or in-place replace).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


PAUSE_LINE_GAP = 0.6  # seconds; used when segment_id is missing


@dataclass(frozen=True)
class Word:
    index: int
    text: str
    start: float  # seconds, relative to the analyzed media
    end: float
    segment_id: int = 0


@dataclass(frozen=True)
class TranscriptLine:
    """A display line: contiguous words sharing a Whisper segment (or pause group)."""

    start_word: int  # word index of first word in the line
    end_word: int  # word index of last word (inclusive)
    start: float
    end: float
    word_indices: tuple[int, ...] = ()


@dataclass
class Transcript:
    words: list[Word]
    duration: float
    source_path: str = ""
    order: list[int] = field(default_factory=list)
    removed: set[int] = field(default_factory=set)
    silence_cuts: list[tuple[float, float]] = field(default_factory=list)
    script_text: str = ""  # raw imported script (Phase 2); alignment recomputed on load

    def __post_init__(self) -> None:
        if not self.order:
            self.order = [w.index for w in self.words]

    # ---- edit ops -------------------------------------------------------

    def delete(self, indices: list[int]) -> None:
        self.removed.update(indices)

    def restore(self, indices: list[int]) -> None:
        self.removed.difference_update(indices)

    def move(self, indices: list[int], dest_pos: int) -> None:
        """Cut `indices` (in current order) and paste before order position
        `dest_pos`, where dest_pos indexes the order list *after* removal."""
        moving = [i for i in self.order if i in set(indices)]
        rest = [i for i in self.order if i not in set(indices)]
        dest_pos = max(0, min(dest_pos, len(rest)))
        self.order = rest[:dest_pos] + moving + rest[dest_pos:]

    def is_reordered(self) -> bool:
        return self.order != sorted(self.order)

    # ---- lines / search -------------------------------------------------

    def lines(self, pause_gap: float = PAUSE_LINE_GAP) -> list[TranscriptLine]:
        """Group current order into display lines by segment_id or pause gaps."""
        if not self.order:
            return []
        has_segments = any(w.segment_id for w in self.words)
        groups: list[list[int]] = []
        current: list[int] = []
        prev: Word | None = None
        for widx in self.order:
            word = self.words[widx]
            if not current:
                current = [widx]
            elif has_segments and word.segment_id == prev.segment_id:  # type: ignore[union-attr]
                current.append(widx)
            elif (
                not has_segments
                and prev is not None
                and word.start - prev.end < pause_gap
            ):
                current.append(widx)
            else:
                groups.append(current)
                current = [widx]
            prev = word
        if current:
            groups.append(current)

        out: list[TranscriptLine] = []
        for g in groups:
            first, last = self.words[g[0]], self.words[g[-1]]
            out.append(
                TranscriptLine(
                    start_word=g[0],
                    end_word=g[-1],
                    start=first.start,
                    end=last.end,
                    word_indices=tuple(g),
                )
            )
        return out

    def find_matches(self, query: str, *, skip_removed: bool = True) -> list[int]:
        """Return word indices (in order) whose text contains query (case-insensitive)."""
        q = query.strip().lower()
        if not q:
            return []
        matches: list[int] = []
        for widx in self.order:
            if skip_removed and widx in self.removed:
                continue
            if q in self.words[widx].text.lower():
                matches.append(widx)
        return matches

    # ---- apply ----------------------------------------------------------

    def keep_ranges(self) -> list[tuple[float, float]]:
        """Compute (start, end) media ranges in output order.

        Consecutive-in-source kept words merge into one span. Cut points
        between a kept word and a removed/absent neighbor fall at the
        midpoint of the gap so natural spacing is preserved. Silence cuts
        are then excised from the spans.
        """
        kept = [i for i in self.order if i not in self.removed]
        if not kept:
            return []

        groups: list[list[int]] = [[kept[0]]]
        for i in kept[1:]:
            if i == groups[-1][-1] + 1:
                groups[-1].append(i)
            else:
                groups.append([i])

        ranges: list[tuple[float, float]] = []
        for g in groups:
            first, last = self.words[g[0]], self.words[g[-1]]
            start = 0.0 if first.index == 0 else self._midpoint(first.index - 1, first.index)
            end = (
                self.duration
                if last.index == len(self.words) - 1
                else self._midpoint(last.index, last.index + 1)
            )
            ranges.extend(self._excise_silence(start, end))
        return [(s, e) for s, e in ranges if e - s > 1e-4]

    def _midpoint(self, left: int, right: int) -> float:
        return (self.words[left].end + self.words[right].start) / 2.0

    def _excise_silence(self, start: float, end: float) -> list[tuple[float, float]]:
        pieces = [(start, end)]
        for cs, ce in sorted(self.silence_cuts):
            out: list[tuple[float, float]] = []
            for s, e in pieces:
                if ce <= s or cs >= e:
                    out.append((s, e))
                    continue
                if cs > s:
                    out.append((s, cs))
                if ce < e:
                    out.append((ce, e))
            pieces = out
        return pieces

    # ---- persistence ----------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(
            {
                "source_path": self.source_path,
                "duration": self.duration,
                "words": [
                    {
                        "text": w.text,
                        "start": w.start,
                        "end": w.end,
                        "segment_id": w.segment_id,
                    }
                    for w in self.words
                ],
                "order": self.order,
                "removed": sorted(self.removed),
                "silence_cuts": self.silence_cuts,
                "script_text": self.script_text,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "Transcript":
        d = json.loads(text)
        words = [
            Word(
                index=i,
                text=w["text"],
                start=w["start"],
                end=w["end"],
                segment_id=int(w.get("segment_id", 0)),
            )
            for i, w in enumerate(d["words"])
        ]
        return cls(
            words=words,
            duration=d["duration"],
            source_path=d.get("source_path", ""),
            order=d.get("order") or [w.index for w in words],
            removed=set(d.get("removed", [])),
            silence_cuts=[tuple(c) for c in d.get("silence_cuts", [])],
            script_text=d.get("script_text", "") or "",
        )

    def save(self, path: Path) -> None:
        path.write_text(self.to_json())

    @classmethod
    def load(cls, path: Path) -> "Transcript":
        return cls.from_json(path.read_text())


# ---- auto-trim helpers ----------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"[^\w']+", "", text.lower())


def find_silence_gaps(
    transcript: Transcript, min_duration: float, max_pause: float
) -> list[tuple[float, float]]:
    """Gaps between consecutive words longer than min_duration, shrunk to
    retain max_pause of silence on each side of the cut."""
    cuts: list[tuple[float, float]] = []
    words = transcript.words
    boundaries = [(0.0, words[0].start)] if words else []
    boundaries += [(words[i].end, words[i + 1].start) for i in range(len(words) - 1)]
    if words:
        boundaries.append((words[-1].end, transcript.duration))
    for gap_start, gap_end in boundaries:
        if gap_end - gap_start >= min_duration:
            cs, ce = gap_start + max_pause, gap_end - max_pause
            if ce - cs > 1e-3:
                cuts.append((cs, ce))
    return cuts


def find_repeats(
    transcript: Transcript,
    max_ngram: int = 8,
    *,
    min_ngram: int = 4,
    min_pause: float = 0.35,
) -> list[list[int]]:
    """Detect abandoned takes: repeated phrases where the first copy should go.

    Only matches n-grams of length ``min_ngram``..``max_ngram``. Skips
    digit-only / non-lexical phrases (e.g. splitting ``5.5``). Requires a
    pause between the two copies of at least ``min_pause`` seconds, unless
    the phrase is long (n >= 6).
    """
    words = transcript.words
    norms = [_norm(w.text) for w in words]
    n_words = len(norms)
    suggested: list[list[int]] = []
    claimed: set[int] = set()
    min_n = max(1, min_ngram)
    max_n = max(min_n, max_ngram)

    def _is_content_ngram(tokens: list[str]) -> bool:
        if not tokens or not all(tokens):
            return False
        if all(t.isdigit() for t in tokens):
            return False
        return any(sum(c.isalpha() for c in t) >= 2 for t in tokens)

    for n in range(max_n, min_n - 1, -1):
        i = 0
        while i + 2 * n <= n_words:
            first = norms[i : i + n]
            second = norms[i + n : i + 2 * n]
            span = set(range(i, i + 2 * n))
            if (
                first == second
                and _is_content_ngram(first)
                and not (span & claimed)
            ):
                gap = words[i + n].start - words[i + n - 1].end
                if gap >= min_pause or n >= 6:
                    group = list(range(i, i + n))
                    suggested.append(group)
                    claimed |= span
                    i += 2 * n
                    continue
            i += 1

    suggested.sort(key=lambda g: g[0])
    return suggested


@dataclass(frozen=True)
class EditSnapshot:
    order: tuple[int, ...]
    removed: frozenset[int]
    silence_cuts: tuple[tuple[float, float], ...]


class EditHistory:
    """Undo/redo stack of transcript edit snapshots."""

    def __init__(self, max_depth: int = 50):
        self.max_depth = max_depth
        self._undo: list[EditSnapshot] = []
        self._redo: list[EditSnapshot] = []

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    def push(self, snapshot: EditSnapshot) -> None:
        self._undo.append(snapshot)
        if len(self._undo) > self.max_depth:
            self._undo.pop(0)
        self._redo.clear()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self, current: EditSnapshot) -> EditSnapshot | None:
        if not self._undo:
            return None
        self._redo.append(current)
        return self._undo.pop()

    def redo(self, current: EditSnapshot) -> EditSnapshot | None:
        if not self._redo:
            return None
        self._undo.append(current)
        return self._redo.pop()


def snapshot_transcript(transcript: Transcript) -> EditSnapshot:
    return EditSnapshot(
        order=tuple(transcript.order),
        removed=frozenset(transcript.removed),
        silence_cuts=tuple(tuple(c) for c in transcript.silence_cuts),
    )


def apply_snapshot(transcript: Transcript, snap: EditSnapshot) -> None:
    transcript.order = list(snap.order)
    transcript.removed = set(snap.removed)
    transcript.silence_cuts = [tuple(c) for c in snap.silence_cuts]


def media_sec_to_timeline_frame(
    media_sec: float, timeline_start_frame: int, fps: float
) -> int:
    """Convert media-relative seconds (from clip in-point analysis) to a timeline frame."""
    return timeline_start_frame + int(round(media_sec * fps))


def frame_to_timecode(frame: int, fps: float) -> str:
    fps_i = max(1, int(round(fps)))
    ff = frame % fps_i
    ss = (frame // fps_i) % 60
    mm = (frame // (fps_i * 60)) % 60
    hh = frame // (fps_i * 3600)
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"
