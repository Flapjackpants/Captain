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


def test_find_repeats_phrase():
    tr = make_transcript(["so", "I", "went", "to", "I", "went", "to", "the", "store"])
    groups = find_repeats(tr)
    assert groups == [[1, 2, 3]]  # first take of "I went to" removed


def test_find_repeats_single_word_stutter():
    tr = make_transcript(["the", "the", "cat"])
    groups = find_repeats(tr)
    assert groups == [[0]]


def test_find_repeats_none():
    tr = make_transcript(["all", "unique", "words", "here"])
    assert find_repeats(tr) == []


def test_repeats_ignore_punctuation_case():
    tr = make_transcript(["Hello,", "hello", "there"])
    groups = find_repeats(tr)
    assert groups == [[0]]


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
