import pytest

from captain.transcript import Transcript, Word, find_repeats, find_silence_gaps


def make_transcript(texts, gap=0.1, word_len=0.4, tail=0.5):
    words = []
    t = 0.2
    for i, text in enumerate(texts):
        words.append(Word(index=i, text=text, start=t, end=t + word_len))
        t += word_len + gap
    return Transcript(words=words, duration=t + tail)


def test_keep_all_is_single_range():
    tr = make_transcript(["hello", "world", "foo"])
    ranges = tr.keep_ranges()
    assert len(ranges) == 1
    assert ranges[0][0] == 0.0
    assert ranges[0][1] == tr.duration


def test_delete_middle_splits_range():
    tr = make_transcript(["a", "b", "c", "d", "e"])
    tr.delete([2])
    ranges = tr.keep_ranges()
    assert len(ranges) == 2
    # cut points at midpoints of the gaps around word 2
    left_cut = (tr.words[1].end + tr.words[2].start) / 2
    right_cut = (tr.words[2].end + tr.words[3].start) / 2
    assert ranges[0] == (0.0, pytest.approx(left_cut))
    assert ranges[1] == (pytest.approx(right_cut), tr.duration)


def test_delete_first_and_last():
    tr = make_transcript(["a", "b", "c"])
    tr.delete([0, 2])
    ranges = tr.keep_ranges()
    assert len(ranges) == 1
    start, end = ranges[0]
    assert start > 0.0
    assert end < tr.duration


def test_delete_everything():
    tr = make_transcript(["a", "b"])
    tr.delete([0, 1])
    assert tr.keep_ranges() == []


def test_restore():
    tr = make_transcript(["a", "b", "c"])
    tr.delete([1])
    tr.restore([1])
    assert len(tr.keep_ranges()) == 1


def test_move_reorders_output():
    tr = make_transcript(["a", "b", "c", "d"])
    tr.move([3], 0)  # move "d" to the front
    assert tr.order == [3, 0, 1, 2]
    assert tr.is_reordered()
    ranges = tr.keep_ranges()
    assert len(ranges) == 2
    # first range is word "d", second range is words a..c
    assert ranges[0][0] > ranges[1][0]


def test_silence_cuts_excised():
    tr = make_transcript(["a", "b"], gap=2.0)
    cuts = find_silence_gaps(tr, min_duration=1.0, max_pause=0.25)
    assert len(cuts) >= 1
    tr.silence_cuts = cuts
    ranges = tr.keep_ranges()
    assert len(ranges) >= 2
    total = sum(e - s for s, e in ranges)
    assert total < tr.duration


def test_silence_respects_max_pause():
    tr = make_transcript(["a", "b"], gap=2.0)
    (cut,) = [
        c for c in find_silence_gaps(tr, 1.0, 0.25)
        if c[0] > tr.words[0].end - 1e-9 and c[1] < tr.words[1].start + 1e-9
    ]
    assert cut[0] == pytest.approx(tr.words[0].end + 0.25)
    assert cut[1] == pytest.approx(tr.words[1].start - 0.25)


def test_find_repeats_phrase_with_pause():
    # Four-word take repeated after a clear pause between copies.
    texts = ["so", "I", "went", "home", "early", "I", "went", "home", "early", "then"]
    words = []
    t = 0.0
    for i, text in enumerate(texts):
        words.append(Word(index=i, text=text, start=t, end=t + 0.3))
        if i == 4:
            t += 0.3 + 0.5  # pause between takes
        else:
            t += 0.3 + 0.05
    tr = Transcript(words=words, duration=t + 0.5)
    groups = find_repeats(tr, min_ngram=4, max_ngram=8, min_pause=0.35)
    assert groups == [[1, 2, 3, 4]]


def test_find_repeats_skips_single_word_stutter():
    tr = make_transcript(["the", "the", "cat"])
    assert find_repeats(tr) == []


def test_find_repeats_skips_digit_split():
    tr = make_transcript(["5", "5", "percent"], gap=0.5)
    assert find_repeats(tr, min_ngram=1, max_ngram=8, min_pause=0.35) == []


def test_find_repeats_none():
    tr = make_transcript(["all", "unique", "words", "here"])
    assert find_repeats(tr) == []


def test_find_repeats_skips_short_echo():
    tr = make_transcript(["Hello,", "hello", "there"], gap=0.5)
    assert find_repeats(tr) == []


def test_edit_history_undo_redo():
    from captain.transcript import (
        EditHistory,
        apply_snapshot,
        snapshot_transcript,
    )

    tr = make_transcript(["a", "b", "c", "d"])
    hist = EditHistory()
    hist.push(snapshot_transcript(tr))
    tr.delete([1, 2])
    assert tr.removed == {1, 2}

    current = snapshot_transcript(tr)
    snap = hist.undo(current)
    assert snap is not None
    apply_snapshot(tr, snap)
    assert tr.removed == set()

    current = snapshot_transcript(tr)
    snap = hist.redo(current)
    assert snap is not None
    apply_snapshot(tr, snap)
    assert tr.removed == {1, 2}

    # New edit clears redo
    hist.push(snapshot_transcript(tr))
    tr.silence_cuts = [(0.1, 0.2)]
    assert not hist.can_redo()


def test_json_roundtrip():
    tr = make_transcript(["a", "b", "c"])
    tr.delete([1])
    tr.move([2], 0)
    tr.silence_cuts = [(0.0, 0.1)]
    tr2 = Transcript.from_json(tr.to_json())
    assert tr2.order == tr.order
    assert tr2.removed == tr.removed
    assert tr2.silence_cuts == tr.silence_cuts
    assert tr2.keep_ranges() == tr.keep_ranges()


def test_segment_id_json_roundtrip():
    words = [
        Word(index=0, text="hello", start=0.0, end=0.3, segment_id=0),
        Word(index=1, text="world", start=0.35, end=0.6, segment_id=0),
        Word(index=2, text="again", start=1.2, end=1.5, segment_id=1),
    ]
    tr = Transcript(words=words, duration=2.0)
    tr2 = Transcript.from_json(tr.to_json())
    assert [w.segment_id for w in tr2.words] == [0, 0, 1]


def test_lines_by_segment_id():
    words = [
        Word(index=0, text="hello", start=0.0, end=0.3, segment_id=0),
        Word(index=1, text="world", start=0.35, end=0.6, segment_id=0),
        Word(index=2, text="again", start=1.2, end=1.5, segment_id=1),
    ]
    tr = Transcript(words=words, duration=2.0)
    lines = tr.lines()
    assert len(lines) == 2
    assert lines[0].word_indices == (0, 1)
    assert lines[1].word_indices == (2,)
    assert lines[0].start == 0.0
    assert lines[1].start == 1.2


def test_lines_by_pause_when_no_segments():
    # All segment_id=0 and large gap → two lines via pause grouping
    words = [
        Word(index=0, text="a", start=0.0, end=0.2, segment_id=0),
        Word(index=1, text="b", start=0.25, end=0.4, segment_id=0),
        Word(index=2, text="c", start=2.0, end=2.2, segment_id=0),
    ]
    tr = Transcript(words=words, duration=3.0)
    # has_segments is False when all segment_id are 0
    lines = tr.lines(pause_gap=0.6)
    assert len(lines) == 2
    assert lines[0].word_indices == (0, 1)
    assert lines[1].word_indices == (2,)


def test_find_matches_skips_removed():
    tr = make_transcript(["Hello", "world", "hello", "there"])
    tr.delete([2])
    assert tr.find_matches("hello") == [0]
    assert tr.find_matches("hello", skip_removed=False) == [0, 2]
    assert tr.find_matches("WORLD") == [1]
    assert tr.find_matches("") == []


def test_frame_to_timecode_and_media_offset():
    from captain.transcript import frame_to_timecode, media_sec_to_timeline_frame

    assert frame_to_timecode(0, 24) == "00:00:00:00"
    assert frame_to_timecode(24, 24) == "00:00:01:00"
    assert frame_to_timecode(25, 24) == "00:00:01:01"
    frame = media_sec_to_timeline_frame(1.0, timeline_start_frame=100, fps=24.0)
    assert frame == 124
    assert frame_to_timecode(frame, 24) == "00:00:05:04"


def test_apply_mode_default():
    from captain import config

    assert config.DEFAULTS["apply_mode"] == "replace_in_place"
    assert "replace_ripple" in config.APPLY_MODES
    assert config.normalize_apply_mode("replace_ripple") == "replace_ripple"
    assert config.normalize_apply_mode("bogus") == "replace_in_place"
    assert config.normalize_apply_mode(None) == "replace_in_place"

