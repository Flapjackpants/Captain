import pytest

from captain import config
from captain.compare import align_transcript, parse_script
from captain.retakes import RETAKE_LEVELS, _words_similar, find_retakes, find_trim_groups
from captain.transcript import Transcript, Word


def build(text, pause_after=None, removed=None, reverse=False):
    words = []
    t = 0.0
    for i, raw in enumerate(text.split()):
        words.append(Word(index=i, text=raw, start=t, end=t + 0.3))
        t += 0.3 + 0.05
        if pause_after is not None and i + 1 == pause_after:
            t += 0.5
    tr = Transcript(words=words, duration=t + 0.5)
    if removed:
        tr.removed = set(removed)
    if reverse:
        tr.order = list(reversed(tr.order))
    return tr


def run(text, level="balanced", **kw):
    return find_retakes(build(text, **kw), sensitivity=level)


def test_reworded_tail_strikes_first_copy():
    assert run("I went to the store I went to the shop today") == [[0, 1, 2, 3, 4]]


def test_conservative_rejects_reworded_tail():
    assert run("I went to the store I went to the shop today", "conservative") == []


def test_truncated_first_copy():
    assert run("we have successfully we have successfully covered the roof") == [[0, 1, 2]]


def test_inserted_word_in_second_copy():
    assert run("we'll be using it to we'll probably be using it to handle auth") == [
        [0, 1, 2, 3, 4]
    ]


def test_chained_restarts_strike_all_but_last():
    assert run("we need to fix we need to fix we need to fix the roof") == [
        [0, 1, 2, 3],
        [4, 5, 6, 7],
    ]


def test_parallel_clause_kept():
    assert run("one in five flights, one in five factories today") == []


def test_comma_with_exact_echo_is_retake():
    assert run("things like the volatility, things like the volatility and beta") == [
        [0, 1, 2, 3]
    ]


def test_sentence_end_inside_first_copy_rejected():
    assert run("we saw the dam. then we saw the lake") == []


def test_period_inside_first_copy_rejected():
    assert run("well. we could go well. we could go home") == []


def test_ellipsis_inside_first_copy_allowed():
    assert run("well... we could go well... we could go home") == [[0, 1, 2, 3]]


def test_filler_before_repair_is_struck():
    assert run("we need to um we need to go now") == [[0, 1, 2, 3]]


def test_no_shared_opening_kept():
    assert run("the cat sat here a dog sat here") == []


def test_single_word_stutter_kept():
    assert run("the the cat") == []


def test_digit_phrase_kept():
    assert run("1 2 3 4 1 2 3 4 then") == []


BORDERLINE = "big red dogs ran to town big red cats ran to sea"


def test_borderline_echo_without_pause_kept():
    assert run(BORDERLINE) == []


def test_pause_accepts_borderline_echo():
    assert run(BORDERLINE, pause_after=6) == [[0, 1, 2, 3, 4, 5]]


def test_aggressive_accepts_borderline_echo():
    assert run(BORDERLINE, "aggressive") == [[0, 1, 2, 3, 4, 5]]


PREFIX = "the old barn by rivers flooded badly last spring now the old barn by rivers stayed dry"


def test_long_shared_prefix_overrides_echo():
    assert run(PREFIX) == [list(range(10))]


def test_conservative_needs_longer_prefix():
    assert run(PREFIX, "conservative") == []


def test_case_insensitive_match():
    assert run("Fast API is very strong fast API is very strong for models") == [
        [0, 1, 2, 3, 4]
    ]


def test_cut_off_word_matches():
    assert run("we wen to town we went to town now") == [[0, 1, 2, 3]]


def test_already_removed_copy_ignored():
    assert run("I went to the store I went to the shop today", removed={0, 1, 2, 3, 4}) == []


def test_removed_word_between_copies_skipped():
    assert run("we need to fix xx we need to fix it", removed={4}) == [[0, 1, 2, 3]]


def test_source_order_used():
    assert run("I went to the store I went to the shop today", reverse=True) == [
        [0, 1, 2, 3, 4]
    ]


def test_second_pass_finds_nothing():
    tr = build("I went to the store I went to the shop today")
    groups = find_retakes(tr)
    assert groups
    for g in groups:
        tr.delete(g)
    assert find_retakes(tr) == []


def test_empty_transcript():
    assert find_retakes(Transcript(words=[], duration=0.0)) == []


def test_unknown_sensitivity_raises():
    with pytest.raises(KeyError):
        find_retakes(Transcript(words=[], duration=0.0), sensitivity="nope")


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("walls", "wall", True),
        ("stone", "stony", True),
        ("pot", "potato", True),
        ("cat", "car", False),
        ("to", "too", False),
        ("", "a", False),
    ],
)
def test_words_similar(a, b, expected):
    assert _words_similar(a, b) is expected


REWORDED = "I went to the store I went to the shop today"


def test_config_sensitivities_match_levels():
    assert set(config.REPEAT_SENSITIVITIES) == set(RETAKE_LEVELS)


def test_trim_groups_defaults_to_smart():
    assert find_trim_groups(build(REWORDED), {}) == [[0, 1, 2, 3, 4]]


def test_trim_groups_exact_ignores_reworded():
    assert find_trim_groups(build(REWORDED), {"repeat_mode": "exact"}) == []


def test_trim_groups_exact_finds_identical():
    tr = build("so I went home early I went home early then", pause_after=5)
    assert find_trim_groups(tr, {"repeat_mode": "exact"}) == [[1, 2, 3, 4]]


def test_trim_groups_uses_sensitivity():
    tr = build(REWORDED)
    assert find_trim_groups(tr, {"repeat_sensitivity": "conservative"}) == []


def _alignment(tr, script):
    return align_transcript(tr, parse_script(script))


def test_trim_groups_script_keeps_parallel_phrases():
    text = "we need to fix it we need to paint it"
    tr = build(text)
    assert find_trim_groups(tr, {}, _alignment(tr, text)) == []
    assert find_trim_groups(tr, {}) == [[0, 1, 2, 3, 4]]


def test_trim_groups_script_confirms_retake():
    tr = build("we need to fix it we need to paint it")
    assert find_trim_groups(tr, {}, _alignment(tr, "we need to paint it")) == [
        [0, 1, 2, 3, 4]
    ]


def test_trim_groups_exact_merges_script_retakes():
    tr = build("I went home early I went home early then")
    groups = find_trim_groups(
        tr, {"repeat_mode": "exact"}, _alignment(tr, "I went home early then")
    )
    assert groups
    for g in groups:
        assert g == list(range(g[0], g[-1] + 1))
