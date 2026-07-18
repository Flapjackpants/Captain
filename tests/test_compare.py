"""Tests for script parsing, DP alignment, and script-aware retakes."""

from __future__ import annotations

from captain.compare import (
    align_transcript,
    align_words,
    detect_script_format,
    find_script_retakes,
    merge_repeat_groups,
    parse_script,
)
from captain.transcript import Transcript, Word, find_repeats


def test_parse_plain():
    tokens = parse_script("Hello, world! This is a test.")
    assert tokens == ["Hello,", "world!", "This", "is", "a", "test."]


def test_parse_srt():
    srt = """1
00:00:00,000 --> 00:00:02,000
Hello world

2
00:00:02,500 --> 00:00:04,000
This is <i>great</i>
"""
    assert detect_script_format(srt) == "srt"
    tokens = parse_script(srt, "clip.srt")
    assert tokens == ["Hello", "world", "This", "is", "great"]


def test_parse_vtt():
    vtt = """WEBVTT

00:00:00.000 --> 00:00:01.500
Opening line

00:00:02.000 --> 00:00:03.000
Second line
"""
    assert detect_script_format(vtt) == "vtt"
    tokens = parse_script(vtt)
    assert tokens == ["Opening", "line", "Second", "line"]


def test_parse_fountain():
    fountain = """
INT. COFFEE SHOP - DAY

ALICE
(whispering)
Hello there Bob.

BOB
Hi Alice, good to see you.

CUT TO:

EXT. STREET - NIGHT

They walk away.
"""
    assert detect_script_format(fountain) == "fountain"
    tokens = parse_script(fountain, "scene.fountain")
    assert "Hello" in tokens
    assert "Bob." in tokens or "Bob" in [t.rstrip(".") for t in tokens]
    assert "ALICE" not in tokens
    assert "INT." not in " ".join(tokens)
    assert "CUT" not in tokens
    # Action lines kept
    assert "They" in tokens
    assert "walk" in tokens


def test_align_perfect_match():
    script = ["hello", "world", "foo"]
    video = ["Hello", "world!", "foo"]
    result = align_words(script, video)
    assert all(op.kind == "match" for op in result.ops)
    assert result.video_statuses() == {0: "match", 1: "match", 2: "match"}
    assert result.script_statuses() == {0: "match", 1: "match", 2: "match"}


def test_align_script_only_blue():
    result = align_words(["a", "b", "c"], ["a", "c"])
    kinds = [op.kind for op in result.ops]
    assert "script_only" in kinds
    assert result.script_statuses()[1] == "missing"  # "b"


def test_align_video_only_magenta():
    result = align_words(["a", "c"], ["a", "um", "c"])
    assert result.video_statuses()[1] == "extra"


def test_align_mismatch_red():
    result = align_words(["hello", "world"], ["hello", "word"])
    # "world" vs "word" — soft edit distance → mismatch
    assert result.ops[-1].kind == "mismatch"
    assert result.video_statuses()[1] == "mismatch"
    assert result.script_statuses()[1] == "mismatch"


def test_align_exact_vs_soft():
    exact = align_words(["cat"], ["cat"])
    soft = align_words(["cats"], ["cat"])
    assert exact.ops[0].kind == "match"
    assert soft.ops[0].kind == "mismatch"


def test_vocabulary_prompt_truncates():
    tokens = [f"Word{i}" for i in range(200)]
    result = align_words(tokens, tokens)
    prompt = result.vocabulary_prompt(max_chars=80)
    assert len(prompt) <= 80
    assert "Word0" in prompt


def test_video_script_link_maps():
    result = align_words(["a", "c"], ["a", "um", "c"])
    v2s = result.video_to_script()
    s2v = result.script_to_video()
    assert v2s[0] == 0
    assert v2s[2] == 1
    assert 1 not in v2s  # video-only "um"
    assert s2v[0] == 0
    assert s2v[1] == 2
    assert result.video_statuses()[1] == "extra"


def test_find_script_retakes():
    # Script once; video says the phrase twice then continues.
    texts = [
        "I", "went", "home", "early",
        "I", "went", "home", "early", "then",
    ]
    words = []
    t = 0.0
    for i, text in enumerate(texts):
        words.append(Word(index=i, text=text, start=t, end=t + 0.3))
        t += 0.35
    tr = Transcript(words=words, duration=t)
    script = ["I", "went", "home", "early", "then"]
    alignment = align_transcript(tr, script)
    groups = find_script_retakes(tr, alignment, min_run=3)
    assert groups
    # The video-only duplicate copy is removed (whichever NW left as extra).
    assert len(groups[0]) >= 3
    # Must be a contiguous run of the repeated phrase indices.
    assert groups[0] == list(range(groups[0][0], groups[0][0] + len(groups[0])))
    statuses = alignment.video_statuses()
    assert all(statuses.get(i) in ("extra", "mismatch") for i in groups[0])


def test_merge_repeat_groups_with_find_repeats():
    texts = ["so", "I", "went", "home", "early", "I", "went", "home", "early", "then"]
    words = []
    t = 0.0
    for i, text in enumerate(texts):
        words.append(Word(index=i, text=text, start=t, end=t + 0.3))
        if i == 4:
            t += 0.3 + 0.5
        else:
            t += 0.3 + 0.05
    tr = Transcript(words=words, duration=t + 0.5)
    heuristic = find_repeats(tr, min_ngram=4, max_ngram=8, min_pause=0.35)
    script = ["so", "I", "went", "home", "early", "then"]
    alignment = align_transcript(tr, script)
    script_groups = find_script_retakes(tr, alignment, min_run=3)
    merged = merge_repeat_groups(heuristic + script_groups)
    assert merged
    # No overlapping indices across groups
    claimed: set[int] = set()
    for g in merged:
        assert not (set(g) & claimed)
        claimed |= set(g)


def test_script_text_json_roundtrip():
    words = [Word(index=0, text="hi", start=0.0, end=0.2)]
    tr = Transcript(words=words, duration=1.0, script_text="Hello there")
    tr2 = Transcript.from_json(tr.to_json())
    assert tr2.script_text == "Hello there"
