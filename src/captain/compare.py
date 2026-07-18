"""Script ↔ transcript alignment (Needleman–Wunsch) and retake helpers.

Color semantics (Phase 2):
  match     — white  (correct)
  missing   — blue   (in script, not in video)
  extra     — magenta (in video, not in script)
  mismatch  — red    (substituted / incorrect)
  removed   — gray strikethrough (user ops; applied in the UI)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .transcript import Transcript, _norm

AlignKind = Literal["match", "mismatch", "script_only", "video_only"]

# Video-word / script-token statuses exposed to the UI.
VideoStatus = Literal["match", "mismatch", "extra"]
ScriptStatus = Literal["match", "mismatch", "missing"]

_MATCH = 2
_MISMATCH = -1
_GAP = -1
_SOFT_MATCH = 1

_TIMECODE_RE = re.compile(
    r"^\d{1,2}:\d{2}:\d{2}([.,]\d+)?\s*-->\s*\d{1,2}:\d{2}:\d{2}"
)
_SRT_INDEX_RE = re.compile(r"^\d+$")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_FOUNTAIN_SCENE_RE = re.compile(
    r"^(INT\.|EXT\.|EST\.|I/E\.|INT/EXT)", re.IGNORECASE
)
_PARENTHETICAL_RE = re.compile(r"^\([^)]+\)$")
_TOKEN_RE = re.compile(r"[^\s]+")


def normalize_token(text: str) -> str:
    return _norm(text)


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def _pair_score(a_norm: str, b_norm: str) -> tuple[int, AlignKind]:
    if not a_norm or not b_norm:
        return _MISMATCH, "mismatch"
    if a_norm == b_norm:
        return _MATCH, "match"
    # Soft: edit distance ≤ 1, or one is a prefix of the other (len ≥ 3).
    if _edit_distance(a_norm, b_norm) <= 1:
        return _SOFT_MATCH, "mismatch"
    if len(a_norm) >= 3 and len(b_norm) >= 3:
        if a_norm.startswith(b_norm) or b_norm.startswith(a_norm):
            return _SOFT_MATCH, "mismatch"
    return _MISMATCH, "mismatch"


def detect_script_format(text: str, path_hint: str | None = None) -> str:
    """Return 'srt', 'vtt', 'fountain', or 'plain'."""
    suffix = Path(path_hint or "").suffix.lower()
    if suffix in (".srt",):
        return "srt"
    if suffix in (".vtt",):
        return "vtt"
    if suffix in (".fountain", ".fadein"):
        return "fountain"

    head = text.lstrip()[:400]
    if head.upper().startswith("WEBVTT"):
        return "vtt"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:40]
    timecode_hits = sum(1 for ln in lines if _TIMECODE_RE.match(ln))
    if timecode_hits >= 2:
        return "srt"
    scene_hits = sum(1 for ln in lines if _FOUNTAIN_SCENE_RE.match(ln))
    caps_hits = sum(
        1
        for ln in lines
        if ln.isupper() and len(ln.split()) <= 6 and not _FOUNTAIN_SCENE_RE.match(ln)
    )
    if scene_hits >= 1 or caps_hits >= 3:
        return "fountain"
    return "plain"


def _tokenize_words(text: str) -> list[str]:
    return [m.group(0) for m in _TOKEN_RE.finditer(text) if normalize_token(m.group(0))]


def _strip_html(s: str) -> str:
    return _HTML_TAG_RE.sub("", s)


def _parse_plain(text: str) -> list[str]:
    return _tokenize_words(text)


def _parse_srt_like(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in text.splitlines():
        line = _strip_html(raw).strip()
        if not line:
            continue
        if line.upper().startswith("WEBVTT"):
            continue
        if line.upper().startswith("NOTE"):
            continue
        if _SRT_INDEX_RE.match(line):
            continue
        if _TIMECODE_RE.match(line):
            continue
        tokens.extend(_tokenize_words(line))
    return tokens


def _parse_fountain(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("!"):
            # Forced action/dialogue — keep the rest.
            tokens.extend(_tokenize_words(stripped[1:]))
            continue
        if stripped.startswith("@"):
            continue  # character cue
        if stripped.startswith("#") or stripped.startswith("="):
            continue  # section / synopses
        if stripped.startswith("~") or stripped.startswith("[["):
            continue
        if _FOUNTAIN_SCENE_RE.match(stripped):
            continue
        if _PARENTHETICAL_RE.match(stripped):
            continue
        # Character cues: ALL CAPS, short, not a transition ending with TO:
        if (
            stripped.isupper()
            and len(stripped.split()) <= 6
            and not stripped.endswith("TO:")
            and not stripped.startswith("(")
        ):
            continue
        if stripped.endswith("TO:") and stripped.isupper():
            continue  # CUT TO: etc.
        tokens.extend(_tokenize_words(stripped))
    return tokens


def parse_script(text: str, path_hint: str | None = None) -> list[str]:
    """Tokenize a script into display words (punctuation kept on tokens)."""
    fmt = detect_script_format(text, path_hint)
    if fmt in ("srt", "vtt"):
        return _parse_srt_like(text)
    if fmt == "fountain":
        return _parse_fountain(text)
    return _parse_plain(text)


def load_script(path: str | Path) -> tuple[str, list[str]]:
    """Read a script file; return (raw_text, tokens)."""
    p = Path(path)
    raw = p.read_text(encoding="utf-8", errors="replace")
    return raw, parse_script(raw, str(p))


@dataclass(frozen=True)
class AlignOp:
    kind: AlignKind
    script_index: int | None = None
    video_index: int | None = None
    script_text: str = ""
    video_text: str = ""


@dataclass
class AlignmentResult:
    ops: list[AlignOp] = field(default_factory=list)
    script_tokens: list[str] = field(default_factory=list)
    video_tokens: list[str] = field(default_factory=list)

    def status_for_video_word(self, video_index: int) -> VideoStatus | None:
        for op in self.ops:
            if op.video_index != video_index:
                continue
            if op.kind == "match":
                return "match"
            if op.kind == "mismatch":
                return "mismatch"
            if op.kind == "video_only":
                return "extra"
        return None

    def video_statuses(self) -> dict[int, VideoStatus]:
        out: dict[int, VideoStatus] = {}
        for op in self.ops:
            if op.video_index is None:
                continue
            if op.kind == "match":
                out[op.video_index] = "match"
            elif op.kind == "mismatch":
                out[op.video_index] = "mismatch"
            elif op.kind == "video_only":
                out[op.video_index] = "extra"
        return out

    def script_statuses(self) -> dict[int, ScriptStatus]:
        out: dict[int, ScriptStatus] = {}
        for op in self.ops:
            if op.script_index is None:
                continue
            if op.kind == "match":
                out[op.script_index] = "match"
            elif op.kind == "mismatch":
                out[op.script_index] = "mismatch"
            elif op.kind == "script_only":
                out[op.script_index] = "missing"
        return out

    def video_to_script(self) -> dict[int, int]:
        """Map video word index → script token index for linked pairs."""
        out: dict[int, int] = {}
        for op in self.ops:
            if (
                op.video_index is not None
                and op.script_index is not None
                and op.kind in ("match", "mismatch")
            ):
                out[op.video_index] = op.script_index
        return out

    def script_to_video(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for op in self.ops:
            if (
                op.video_index is not None
                and op.script_index is not None
                and op.kind in ("match", "mismatch")
            ):
                out[op.script_index] = op.video_index
        return out

    def vocabulary_prompt(self, max_chars: int = 800) -> str:
        """Unique script words suitable as a Whisper initial_prompt."""
        seen: set[str] = set()
        parts: list[str] = []
        for tok in self.script_tokens:
            n = normalize_token(tok)
            if not n or n in seen:
                continue
            if n.isdigit():
                continue
            seen.add(n)
            parts.append(tok.strip(".,!?;:\"'"))
        prompt = " ".join(parts)
        if len(prompt) <= max_chars:
            return prompt
        # Truncate on a word boundary.
        cut = prompt[:max_chars].rsplit(" ", 1)[0]
        return cut


def align_words(
    script_tokens: list[str],
    video_tokens: list[str],
) -> AlignmentResult:
    """Needleman–Wunsch global alignment of script vs video word lists."""
    s_norms = [normalize_token(t) for t in script_tokens]
    v_norms = [normalize_token(t) for t in video_tokens]
    n, m = len(s_norms), len(v_norms)

    # score[i][j] = best score aligning script[:i] with video[:j]
    score = [[0] * (m + 1) for _ in range(n + 1)]
    # ptr: 0=diag, 1=up (script_only), 2=left (video_only)
    ptr = [[0] * (m + 1) for _ in range(n + 1)]

    for i in range(1, n + 1):
        score[i][0] = i * _GAP
        ptr[i][0] = 1
    for j in range(1, m + 1):
        score[0][j] = j * _GAP
        ptr[0][j] = 2

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            pair, _kind = _pair_score(s_norms[i - 1], v_norms[j - 1])
            diag = score[i - 1][j - 1] + pair
            up = score[i - 1][j] + _GAP
            left = score[i][j - 1] + _GAP
            best = diag
            best_ptr = 0
            if up > best:
                best = up
                best_ptr = 1
            if left > best:
                best = left
                best_ptr = 2
            score[i][j] = best
            ptr[i][j] = best_ptr

    ops_rev: list[AlignOp] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ptr[i][j] == 0:
            _, kind = _pair_score(s_norms[i - 1], v_norms[j - 1])
            ops_rev.append(
                AlignOp(
                    kind=kind,
                    script_index=i - 1,
                    video_index=j - 1,
                    script_text=script_tokens[i - 1],
                    video_text=video_tokens[j - 1],
                )
            )
            i -= 1
            j -= 1
        elif i > 0 and (j == 0 or ptr[i][j] == 1):
            ops_rev.append(
                AlignOp(
                    kind="script_only",
                    script_index=i - 1,
                    script_text=script_tokens[i - 1],
                )
            )
            i -= 1
        else:
            ops_rev.append(
                AlignOp(
                    kind="video_only",
                    video_index=j - 1,
                    video_text=video_tokens[j - 1],
                )
            )
            j -= 1

    ops_rev.reverse()
    return AlignmentResult(
        ops=ops_rev,
        script_tokens=list(script_tokens),
        video_tokens=list(video_tokens),
    )


def align_transcript(
    transcript: Transcript,
    script_tokens: list[str],
) -> AlignmentResult:
    """Align script tokens to transcript words in source index order."""
    video_tokens = [w.text for w in transcript.words]
    return align_words(script_tokens, video_tokens)


def merge_repeat_groups(groups: list[list[int]]) -> list[list[int]]:
    """Merge overlapping index groups; prefer longer spans."""
    if not groups:
        return []
    sorted_groups = sorted(groups, key=lambda g: (g[0], -(len(g))))
    merged: list[list[int]] = []
    claimed: set[int] = set()
    for g in sorted_groups:
        if set(g) & claimed:
            continue
        merged.append(g)
        claimed |= set(g)
    return merged


def find_script_retakes(
    transcript: Transcript,
    alignment: AlignmentResult,
    *,
    min_run: int = 3,
) -> list[list[int]]:
    """Suggest removing abandoned takes: video-only/mismatch runs that echo
    a matched phrase from the script (before or after the run).

    Looks for a contiguous run of ``extra``/``mismatch`` video words whose
    normalized text also appears elsewhere as a mostly-``match`` span of the
    same length. The extra/mismatch copy is what gets cut.
    """
    statuses = alignment.video_statuses()
    words = transcript.words
    n = len(words)
    if n == 0:
        return []

    norms = [normalize_token(w.text) for w in words]
    runs: list[list[int]] = []
    current: list[int] = []
    for i in range(n):
        st = statuses.get(i)
        if st in ("extra", "mismatch") and i not in transcript.removed:
            current.append(i)
        else:
            if len(current) >= min_run:
                runs.append(current)
            current = []
    if len(current) >= min_run:
        runs.append(current)

    suggested: list[list[int]] = []
    claimed: set[int] = set()
    for run in runs:
        run_norms = [norms[i] for i in run]
        if not all(run_norms):
            continue
        length = len(run)
        run_set = set(run)

        def _match_span_at(j: int) -> bool:
            later = list(range(j, j + length))
            if any(idx in claimed or idx in transcript.removed for idx in later):
                return False
            if set(later) & run_set:
                return False
            if [norms[idx] for idx in later] != run_norms:
                return False
            match_count = sum(1 for idx in later if statuses.get(idx) == "match")
            return match_count >= max(1, length // 2)

        # Prefer a matching script-aligned copy anywhere else in the video.
        for j in range(0, n - length + 1):
            if _match_span_at(j):
                if not (run_set & claimed):
                    suggested.append(run)
                    claimed |= run_set
                break

    return suggested
