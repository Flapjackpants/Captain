from __future__ import annotations

import pytest

from captain.api import ClipInfo
from captain.captions import build_caption_segments, normalize_caption_settings
from captain.transcript import Transcript, Word


def _transcript() -> Transcript:
    return Transcript(
        words=[
            Word(0, "Hello", 0.0, 0.4, 1),
            Word(1, "there", 0.5, 0.8, 1),
            Word(2, "good", 1.0, 1.3, 2),
            Word(3, "people", 2.0, 2.4, 2),
        ],
        duration=5.0,
    )


def _clip() -> ClipInfo:
    return ClipInfo(
        clip_id="video:1:100:48",
        name="Interview",
        track_type="video",
        track_index=1,
        timeline_start_frame=100,
        timeline_end_frame=220,
        source_start_frame=48,
        source_end_frame=168,
        file_path="/tmp/interview.mov",
        fps=24.0,
    )


def test_caption_segments_hold_through_gap_by_default():
    segments = build_caption_segments(_transcript(), _clip())
    assert [s.text for s in segments] == ["Hello there", "good people"]
    assert [(s.start_frame, s.end_frame) for s in segments] == [(100, 124), (124, 220)]
    assert segments[0].write_on_end_frame == 112
    assert segments[1].write_on_end_frame == 148


def test_caption_segments_can_end_on_last_word_and_skip_removed_words():
    tr = _transcript()
    tr.delete([1])
    segments = build_caption_segments(tr, _clip(), hold_to_next=False)
    assert [s.text for s in segments] == ["Hello", "good people"]
    assert segments[0].end_frame == 110
    assert segments[1].end_frame == 158


def test_word_by_word_is_one_caption_per_kept_word():
    segments = build_caption_segments(_transcript(), _clip(), word_by_word=True)
    assert [s.text for s in segments] == ["Hello", "there", "good", "people"]
    assert [(s.start_frame, s.end_frame) for s in segments] == [
        (100, 112), (112, 124), (124, 148), (148, 220)
    ]


def test_caption_break_splits_whisper_caption_segment():
    tr = _transcript()
    assert tr.add_caption_break(1)
    segments = build_caption_segments(tr, _clip())
    assert [s.text for s in segments] == ["Hello", "there", "good people"]


def test_caption_setting_normalization_clamps_and_overlays_defaults():
    settings = normalize_caption_settings({"font_size": 0, "position_x": 3, "write_on": True})
    assert settings["font_size"] == 1
    assert settings["position_x"] == 1.0
    assert settings["write_on"] is True
    assert settings["hold_to_next"] is True

