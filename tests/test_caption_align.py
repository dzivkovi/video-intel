"""`caption_align`: map a transcript's words onto a caption track's cue timings.

Pure module, standard library only. Every caption track here is synthetic and
hand-written; no real caption text enters the repo.
"""

import itertools
import pathlib
import subprocess
import sys
import textwrap

import caption_align as ca
import pytest


def _cues(*rows: tuple[float, str]) -> list[ca.Cue]:
    return [ca.Cue(start=start, duration=2.0, text=text) for start, text in rows]


def _talk_cues(sentences: list[str], *, every: int = 10, first: int = 5) -> list[ca.Cue]:
    """One cue per sentence, `every` seconds apart: a track whose timing is known exactly."""
    return [ca.Cue(start=float(first + i * every), duration=float(every), text=s) for i, s in enumerate(sentences)]


SENTENCES = [f"Sentence number {n} talks about topic {n} in plain words." for n in range(1, 31)]


class TestNormalizeWords:
    def test_normalize_words_mixed_case_and_punctuation_returns_lowercase_tokens(self):
        assert ca.normalize_words("Hello, World! It's 2.7x - ok?") == ["hello", "world", "its", "2", "7x", "ok"]

    def test_normalize_words_curly_apostrophe_matches_straight(self):
        assert ca.normalize_words("don\u2019t") == ca.normalize_words("don't") == ["dont"]

    def test_normalize_words_empty_text_returns_empty_list(self):
        assert ca.normalize_words("  ...  ") == []


class TestAlignWordsToCues:
    def test_align_identical_words_maps_every_word_to_its_cue(self):
        cues = _cues((0.0, "alpha beta"), (3.0, "gamma delta"))
        result = ca.align_words_to_cues(["alpha", "beta", "gamma", "delta"], cues)
        assert result == [0, 0, 1, 1]

    def test_align_missing_and_extra_words_keeps_order_and_marks_unmatched_none(self):
        cues = _cues((0.0, "one two three"), (5.0, "four five six"), (9.0, "seven eight nine"))
        words = ["um", "one", "two", "three", "four", "extra", "six", "seven", "eight", "nine"]
        result = ca.align_words_to_cues(words, cues)
        assert result[0] is None  # a filler the captions dropped
        assert result[1:5] == [0, 0, 0, 1]
        assert result[5] is None  # a word the captions do not have
        assert result[6:] == [1, 2, 2, 2]

    def test_align_result_cue_indices_never_decrease(self):
        cues = _talk_cues(SENTENCES)
        words = ca.normalize_words(" ".join(SENTENCES))
        result = [c for c in ca.align_words_to_cues(words, cues) if c is not None]
        assert result == sorted(result)

    def test_align_long_input_with_common_words_between_content_words_still_aligns(self):
        # Every other word is a very common one ("is", "about", "and"), and the
        # track carries cues the transcript skipped: the shape of real speech.
        # Over 200 words, difflib's autojunk stops those words from seeding or
        # bridging a match, and this aligned 4% of its words.
        vocab = [f"topic{k}" for k in range(31)]
        lines, cues = [], []
        for i in range(50):
            said = f"point {i} is about {vocab[i % 31]} and {vocab[(i * 7 + 3) % 31]}"
            lines.append(said)
            cues.append(ca.Cue(float(i * 11), 5.0, said))
            cues.append(ca.Cue(float(i * 11 + 6), 5.0, f"we keep {vocab[(i * 3 + 1) % 31]} in mind"))
        words = [w for line in lines for w in ca.normalize_words(line)]
        result = ca.align_words_to_cues(words, cues)
        assert sum(c is not None for c in result) / len(words) > 0.95

    def test_align_no_common_words_returns_all_none(self):
        assert ca.align_words_to_cues(["zz", "yy"], _cues((0.0, "aa bb"))) == [None, None]


class TestEntryOffsets:
    def test_entry_offsets_shifted_entry_reports_signed_offset(self):
        cues = _talk_cues(SENTENCES[:3])  # starts 5, 15, 25
        entries = [(12, SENTENCES[0]), (14, SENTENCES[1]), (40, SENTENCES[2])]
        offsets = ca.entry_offsets(entries, cues)
        assert [o.caption_seconds for o in offsets] == [5.0, 15.0, 25.0]
        assert [o.offset_seconds for o in offsets] == [7.0, -1.0, 15.0]
        assert all(o.coverage == 1.0 for o in offsets)

    def test_entry_offsets_unalignable_opening_returns_none(self):
        cues = _talk_cues(["the quick brown fox jumps over the lazy dog"])
        entries = [(0, "Zebra yak walrus the quick brown fox jumps over the lazy dog")]
        offset = ca.entry_offsets(entries, cues)[0]
        assert offset.caption_seconds is None
        assert offset.offset_seconds is None
        assert offset.coverage > 0.6

    def test_entry_offsets_reports_without_editing_the_entries(self):
        entries = [(3, SENTENCES[0])]
        before = list(entries)
        ca.entry_offsets(entries, _talk_cues(SENTENCES[:1]))
        assert entries == before


def _plan(text, cues, original=0, **kw):
    return ca.plan_entry_split(text, cues, original_seconds=original, **kw)


class TestPlanEntrySplit:
    def test_plan_long_entry_splits_into_pieces_near_the_target(self):
        text = " ".join(SENTENCES)  # 30 sentences, one per 10 s cue: 290 s of caption time
        plan = _plan(text, _talk_cues(SENTENCES), original=5)
        assert plan.candidate and plan.repairable
        stamps = [p.stamp_seconds for p in plan.pieces]
        assert stamps[0] == 5  # the original stamp
        gaps = [b - a for a, b in itertools.pairwise(stamps)]
        assert all(20 <= g <= 30 for g in gaps), gaps

    def test_plan_first_piece_keeps_original_stamp_and_has_no_cue(self):
        plan = _plan(" ".join(SENTENCES), _talk_cues(SENTENCES), original=12)
        assert plan.pieces[0].stamp_seconds == 12
        assert plan.pieces[0].cue_index is None

    def test_plan_every_new_stamp_is_the_truncated_start_of_its_cue(self):
        cues = [ca.Cue(start=5.7 + i * 10, duration=10.0, text=s) for i, s in enumerate(SENTENCES)]
        plan = _plan(" ".join(SENTENCES), cues, original=5)
        for piece in plan.pieces[1:]:
            assert piece.stamp_seconds == int(cues[piece.cue_index].start)

    def test_plan_pieces_rejoined_reproduce_the_text(self):
        text = "  ".join(SENTENCES)  # double spaces between sentences
        plan = _plan(text, _talk_cues(SENTENCES), original=5)
        assert " ".join(" ".join(p.text for p in plan.pieces).split()) == " ".join(text.split())

    def test_plan_splits_only_after_sentence_punctuation(self):
        plan = _plan(" ".join(SENTENCES), _talk_cues(SENTENCES), original=5)
        for piece in plan.pieces[:-1]:
            assert piece.text.rstrip()[-1] in ".?!"

    def test_plan_boundary_without_anchor_in_first_three_words_is_skipped(self):
        # Sentence 4 opens with three words the track does not have, so no piece may start there.
        sentences = list(SENTENCES)
        sentences[3] = "Xq zv wk " + sentences[3]
        plan = _plan(" ".join(sentences), _talk_cues(SENTENCES), original=5)
        assert all(not p.text.startswith("Xq zv wk") for p in plan.pieces)

    def test_plan_anchor_on_third_word_is_used(self):
        sentences = list(SENTENCES)
        sentences[2] = "Xq zv " + sentences[2]
        plan = _plan(" ".join(sentences), _talk_cues(SENTENCES), original=0)
        starts = [p for p in plan.pieces if p.text.startswith("Xq zv")]
        assert starts and starts[0].stamp_seconds == 25

    def test_plan_boundary_that_would_go_backwards_is_skipped(self):
        # Original stamp 100: every boundary whose cue starts at or before 100 s is unusable.
        plan = _plan(" ".join(SENTENCES), _talk_cues(SENTENCES), original=100)
        stamps = [p.stamp_seconds for p in plan.pieces]
        assert stamps[0] == 100
        assert all(b > a for a, b in itertools.pairwise(stamps))

    def test_plan_upper_bound_keeps_every_new_stamp_below_it(self):
        text = " ".join(SENTENCES)
        plan = _plan(text, _talk_cues(SENTENCES), original=5, max_stamp_exclusive=100)
        stamps = [p.stamp_seconds for p in plan.pieces]
        assert plan.repairable and all(s < 100 for s in stamps), stamps
        assert " ".join(" ".join(p.text for p in plan.pieces).split()) == " ".join(text.split())
        assert plan.pieces[-1].text.endswith(SENTENCES[-1])  # the rest stays with the last piece

    def test_plan_short_span_is_not_a_candidate(self):
        plan = _plan(" ".join(SENTENCES[:5]), _talk_cues(SENTENCES[:5]))  # 40 s of caption time
        assert not plan.candidate
        assert not plan.repairable
        assert [p.text for p in plan.pieces] == [" ".join(SENTENCES[:5])]

    def test_plan_span_threshold_boundary_60_is_not_candidate_61_is(self):
        two = ["alpha beta gamma.", "delta epsilon zeta."]
        at_60 = [ca.Cue(0.0, 2.0, two[0]), ca.Cue(60.0, 2.0, two[1])]
        at_61 = [ca.Cue(0.0, 2.0, two[0]), ca.Cue(61.0, 2.0, two[1])]
        assert _plan(" ".join(two), at_60).candidate is False
        assert _plan(" ".join(two), at_61).candidate is True

    def test_plan_coverage_threshold_boundary(self):
        # 10 words, 6 aligned -> exactly 0.6: repairable. 5 aligned -> 0.5: declined.
        cues = [ca.Cue(0.0, 2.0, "aa bb cc."), ca.Cue(100.0, 2.0, "dd ee ff.")]
        at_06 = "Aa bb cc qq rr. Dd ee ff ss tt."
        below = "Aa bb cc qq rr. Dd ee yy ss tt."
        plan_ok = _plan(at_06, cues)
        plan_low = _plan(below, cues)
        assert plan_ok.coverage == pytest.approx(0.6)
        assert plan_ok.repairable
        assert plan_low.coverage == pytest.approx(0.5)
        assert plan_low.candidate and not plan_low.repairable
        assert "coverage" in plan_low.reason

    def test_plan_unpunctuated_candidate_has_no_usable_boundary(self):
        text = " ".join(s.rstrip(".") for s in SENTENCES)
        plan = _plan(text, _talk_cues(SENTENCES))
        assert plan.candidate and not plan.repairable
        assert "boundary" in plan.reason

    def test_plan_precomputed_word_cues_must_match_word_count(self):
        with pytest.raises(ValueError):
            _plan("one two.", _talk_cues(["one two"]), word_cues=[0])


class TestAlignEntries:
    def test_align_entries_slices_one_global_alignment_per_entry(self):
        cues = _talk_cues(SENTENCES[:4])
        texts = [SENTENCES[0] + " " + SENTENCES[1], SENTENCES[2], SENTENCES[3]]
        slices = ca.align_entries(texts, cues)
        assert [len(s) for s in slices] == [len(ca.normalize_words(t)) for t in texts]
        assert {c for c in slices[1] if c is not None} == {2}


class TestStandaloneImport:
    def test_caption_align_imports_with_video_intel_blocked(self):
        child = textwrap.dedent("""
            import sys, importlib.abc
            BLOCK = {'video_intel', 'googleapiclient', 'google', 'yaml', 'httpx'}

            class Blocker(importlib.abc.MetaPathFinder):
                def find_spec(self, name, path=None, target=None):
                    if name.split('.')[0] in BLOCK:
                        raise ImportError('blocked: ' + name)
                    return None

            sys.meta_path.insert(0, Blocker())
            import caption_align
            print('OK')
            print('video_intel pulled:', 'video_intel' in sys.modules)
            """)
        result = subprocess.run(
            [sys.executable, "-c", child],
            cwd=str(pathlib.Path(__file__).resolve().parent.parent / "scripts"),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout
        assert "video_intel pulled: False" in result.stdout
