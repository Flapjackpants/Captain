"""Smart retake detection: strike false starts that the next phrase re-says.

Disfluency model: a speaker begins a phrase (the false start), optionally
trails off or says a filler ("um"), then restarts and repairs it. The repair
usually re-uses most of the same words but not always the exact same ones, so
unlike ``find_repeats`` this matches fuzzily. The false start (first copy) is
the part to strike; the repair (second copy) is kept.

A candidate first copy ``live[a:b]`` is compared against the window of words
right after it (``live[b : b + len(first) + 4]``) using a weighted LCS, where
fillers weigh nothing and stopwords weigh half. Sensitivity levels tune how
much of the first copy must be echoed.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

from .compare import (
    AlignmentResult,
    _edit_distance,
    drop_script_matched,
    find_script_retakes,
    merge_repeat_groups,
)
from .transcript import Transcript, _is_content_ngram, _norm, find_repeats


@dataclass(frozen=True)
class RetakeLevel:
    min_echo: float
    min_matched: int
    min_density: float
    prefix_override: int


RETAKE_LEVELS: dict[str, RetakeLevel] = {
    "conservative": RetakeLevel(0.85, 4, 0.60, 6),
    "balanced": RetakeLevel(0.70, 3, 0.50, 5),
    "aggressive": RetakeLevel(0.55, 3, 0.45, 4),
}

_FILLERS = frozenset("um uh er ah erm hmm mm oops sorry okay".split())
_STOPWORDS = frozenset(
    "a an the and or but so to of in on at for with it its is are was were be "
    "been i you he she they we my your our this that these those there here as "
    "if then just like very really not do does did have has had will would can "
    "could should what which who how when where why".split()
)

_MAX_FIRST = 30  # longest first copy, in words
_WINDOW_SLACK = 4  # extra words of the second-copy window beyond the first copy
_PAUSE_BUMP = 0.10  # echo requirement relief when a pause precedes the repair
_MIN_WEIGHT = 0.01  # LCS objective floor so zero-weight fillers can still match


@lru_cache(maxsize=None)
def _words_similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    short = min(len(a), len(b))
    if short >= 4 and _edit_distance(a, b) <= 1:
        return True
    if short >= 3 and (a.startswith(b) or b.startswith(a)):
        return True
    return False


def _weight(norm: str) -> float:
    if norm in _FILLERS:
        return 0.0
    if norm in _STOPWORDS:
        return 0.5
    return 1.0


def _ends_sentence(text: str) -> bool:
    text = text.rstrip()
    return text.endswith((".", "?", "!")) and not text.endswith("...")


def _lcs(
    first: list[str], window: list[str], weights: list[float]
) -> tuple[float, int, int]:
    """Weighted LCS; returns (matched weight, matched count, last window pos)."""
    n, m = len(first), len(window)
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        w = max(weights[i - 1], _MIN_WEIGHT)
        for j in range(1, m + 1):
            best = max(dp[i - 1][j], dp[i][j - 1])
            if _words_similar(first[i - 1], window[j - 1]):
                best = max(best, dp[i - 1][j - 1] + w)
            dp[i][j] = best
    i, j = n, m
    matched_weight = 0.0
    count = 0
    last_pos = -1
    while i > 0 and j > 0:
        if (
            _words_similar(first[i - 1], window[j - 1])
            and dp[i][j]
            == dp[i - 1][j - 1] + max(weights[i - 1], _MIN_WEIGHT)
        ):
            matched_weight += weights[i - 1]
            count += 1
            if last_pos < 0:
                last_pos = j - 1
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1
    return matched_weight, count, last_pos


def find_retakes(
    transcript: Transcript,
    *,
    sensitivity: str = "balanced",
    min_pause: float = 0.35,
) -> list[list[int]]:
    """Return word-index groups (first copies) that a later phrase re-says.

    Reads spoken (source) order and skips words already removed. Groups are
    sorted by first index and never overlap.
    """
    level = RETAKE_LEVELS[sensitivity]
    words = transcript.words
    live = [i for i in range(len(words)) if i not in transcript.removed]
    norms = [_norm(words[i].text) for i in live]
    raws = [words[i].text for i in live]
    n_live = len(live)

    # (matched weight, echo, boundary order, first copy position range)
    candidates: list[tuple[float, float, int, int]] = []
    for b in range(2, n_live):
        gap = words[live[b]].start - words[live[b - 1]].end
        bump = _PAUSE_BUMP if gap >= min_pause else 0.0
        best: tuple[float, float, int, int] | None = None
        for a in range(b - 2, max(-1, b - _MAX_FIRST - 1), -1):
            if not _words_similar(norms[a], norms[b]):
                continue
            if any(_ends_sentence(raws[k]) for k in range(a, b - 1)):
                continue
            first = norms[a:b]
            if not _is_content_ngram(first):
                continue
            weights = [_weight(t) for t in first]
            first_weight = sum(weights)
            if first_weight <= 0:
                continue
            window = norms[b : b + len(first) + _WINDOW_SLACK]
            matched_weight, matched, last_pos = _lcs(first, window, weights)
            if matched == 0:
                continue
            echo = matched_weight / first_weight
            density = matched / (last_pos + 1)
            prefix = 0
            for x, y in zip(first, window):
                if not _words_similar(x, y):
                    break
                prefix += 1
            ok = (
                matched >= level.min_matched
                and echo >= level.min_echo - bump
                and density >= level.min_density
            ) or prefix >= level.prefix_override
            if not ok:
                continue
            if (
                raws[b - 1].rstrip().endswith(",")
                and echo < 1.0
                and prefix < level.prefix_override
            ):
                continue
            if best is None or (matched_weight, echo) > (best[0], best[1]):
                best = (matched_weight, echo, a, b)
        if best is not None:
            candidates.append(best)

    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    taken: set[int] = set()
    groups: list[list[int]] = []
    for _, _, a, b in candidates:
        span = range(a, b)
        if any(k in taken for k in span):
            continue
        taken.update(span)
        groups.append([live[k] for k in span])
    groups.sort(key=lambda g: g[0])
    return groups


def find_trim_groups(
    transcript: Transcript,
    cfg: Mapping[str, Any],
    alignment: AlignmentResult | None = None,
) -> list[list[int]]:
    """Groups to strike for Remove Repeats, per the repeat settings in ``cfg``."""
    min_pause = cfg.get("repeat_min_pause", 0.35)
    if cfg.get("repeat_mode") == "exact":
        groups = find_repeats(
            transcript,
            max_ngram=cfg.get("repeat_max_ngram", 8),
            min_ngram=cfg.get("repeat_min_ngram", 4),
            min_pause=min_pause,
        )
    else:
        groups = find_retakes(
            transcript,
            sensitivity=cfg.get("repeat_sensitivity", "balanced"),
            min_pause=min_pause,
        )
        if alignment is not None:
            groups = drop_script_matched(groups, alignment)
    if alignment is not None:
        groups = merge_repeat_groups(groups + find_script_retakes(transcript, alignment))
    return groups
