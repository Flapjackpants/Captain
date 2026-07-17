"""Transcript data model and edit operations.

The transcript is a list of immutable timed words. Edits are expressed as:
  - a set of removed word indices
  - an output order (list of word indices), which differs from source order
    only when the user cuts and pastes words elsewhere
  - a list of silence cut ranges (seconds) to excise inside kept spans

Applying an edit reduces to a list of (start, end) keep-ranges in output
order, which the assembler turns into a new Resolve timeline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Word:
    index: int
    text: str
    start: float  # seconds, relative to the analyzed media
    end: float


@dataclass
class Transcript:
    words: list[Word]
    duration: float
    source_path: str = ""
    order: list[int] = field(default_factory=list)
    removed: set[int] = field(default_factory=set)
    silence_cuts: list[tuple[float, float]] = field(default_factory=list)

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
                    {"text": w.text, "start": w.start, "end": w.end} for w in self.words
                ],
                "order": self.order,
                "removed": sorted(self.removed),
                "silence_cuts": self.silence_cuts,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "Transcript":
        d = json.loads(text)
        words = [
            Word(index=i, text=w["text"], start=w["start"], end=w["end"])
            for i, w in enumerate(d["words"])
        ]
        return cls(
            words=words,
            duration=d["duration"],
            source_path=d.get("source_path", ""),
            order=d.get("order") or [w.index for w in words],
            removed=set(d.get("removed", [])),
            silence_cuts=[tuple(c) for c in d.get("silence_cuts", [])],
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


def find_repeats(transcript: Transcript, max_ngram: int = 8) -> list[list[int]]:
    """Detect immediately repeated phrases (retakes / stutters).

    Scans for n-grams (longest first) whose normalized text is immediately
    repeated; suggests removing the *first* occurrence, since a re-take
    usually means the speaker's later attempt is the good one. Returns
    groups of word indices to remove.
    """
    norms = [_norm(w.text) for w in transcript.words]
    n_words = len(norms)
    suggested: list[list[int]] = []
    claimed: set[int] = set()

    for n in range(max_ngram, 0, -1):
        i = 0
        while i + 2 * n <= n_words:
            first = norms[i : i + n]
            second = norms[i + n : i + 2 * n]
            span = set(range(i, i + 2 * n))
            if first == second and all(t for t in first) and not (span & claimed):
                group = list(range(i, i + n))
                suggested.append(group)
                claimed |= span
                i += 2 * n
            else:
                i += 1

    suggested.sort(key=lambda g: g[0])
    return suggested
