"""One validated accessor for `config["channels"]` (issue #213).

Thirteen sites read that key directly, each assuming a list of dicts carrying a
`name`. Four ordinary YAML mistakes therefore raised a raw traceback across
eight commands. Issue #205 hardened two of the thirteen; the Codex peer pass on
it confirmed by EXECUTION that the other eleven still crashed - so this is the
sweep, and `configured_channels` is the one definition.

The interesting part is not the guard, it is `strict`. A malformed entry must
NOT be silently dropped on a curate path: skipping a channel the operator
believes they configured is how a creator goes unscanned for weeks, and `scan`
/ `concepts` / `dedupe` / `prune-shorts` all spend money or mutate the corpus on
the strength of this list. Read-only consumers degrade instead, because
aborting `status` over one bad row is worse than the row.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import video_intel as vi

SOURCE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "video_intel.py"
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")

GOOD = {"name": "beta", "url": "https://youtube.com/@beta"}

# The four shapes, each an ordinary YAML slip rather than a hypothetical.
MALFORMED = {
    "empty key (parses as None)": None,
    "a mapping (the dashes omitted)": {"name": "alpha", "url": "https://youtube.com/@alpha"},
    "a bare string": "alpha",
}
MALFORMED_ENTRIES = {
    "a scalar list entry": ["alpha", GOOD],
    "an entry with no name": [{"url": "https://youtube.com/@nameless"}, GOOD],
    "an entry whose name is not a string": [{"name": 42, "url": "u"}, GOOD],
    "an entry whose name is blank": [{"name": "   ", "url": "u"}, GOOD],
    "a None entry": [None, GOOD],
}


class TestLenientDegrades:
    """The read-only contract: never raise, never exit, keep what is usable."""

    @pytest.mark.parametrize(("label", "raw"), sorted(MALFORMED.items()))
    def test_a_malformed_whole_value_yields_no_channels(self, label, raw):
        assert vi.configured_channels({"channels": raw}) == []

    @pytest.mark.parametrize(("label", "raw"), sorted(MALFORMED_ENTRIES.items()))
    def test_one_bad_entry_does_not_lose_the_good_ones(self, label, raw):
        """The half that matters. A guard that answered `[]` for any list
        containing a bad row would be 'safe' and would silently stop scanning
        every healthy channel below it."""
        assert vi.configured_channels({"channels": raw}) == [GOOD]

    def test_a_missing_key_is_not_an_error(self):
        assert vi.configured_channels({}) == []

    def test_a_healthy_config_passes_through_unchanged(self):
        cfg = {"channels": [GOOD, {"name": "gamma", "url": "u", "enabled": False}]}
        assert vi.configured_channels(cfg) == cfg["channels"]

    def test_the_entries_are_the_SAME_objects_not_copies(self):
        """Callers mutate the returned dicts (`record_alt_title_if_rotated`
        writes into the channel config it was handed). Copying here would make
        those writes vanish, which is a far quieter bug than the crash this
        accessor replaced."""
        cfg = {"channels": [GOOD]}
        assert vi.configured_channels(cfg)[0] is cfg["channels"][0]

    def test_every_rejection_is_reported(self, caplog):
        """A guard that stops guarding must never do it silently - the standing
        rule from the `prompt == 0` confab guards."""
        with caplog.at_level("WARNING"):
            vi.configured_channels({"channels": ["alpha", GOOD]})
        assert any("entry 0" in r.getMessage() for r in caplog.records), (
            f"the dropped entry was not reported: {[r.getMessage() for r in caplog.records]}"
        )


class TestStrictAborts:
    """The curate contract. Exit CODE asserted, never a bare `pytest.raises`
    (issue #185): `SystemExit(0)` satisfies that just as happily."""

    @pytest.mark.parametrize(("label", "raw"), sorted(MALFORMED.items()))
    def test_a_malformed_whole_value_exits_1(self, label, raw):
        if raw is None:
            # An ABSENT watchlist is not a malformed one. `require_channels_config`
            # owns that case and gives its own message; conflating them would
            # make a fresh checkout look like a syntax error.
            assert vi.configured_channels({"channels": raw}, strict=True) == []
            return
        with pytest.raises(SystemExit) as exc:
            vi.configured_channels({"channels": raw}, strict=True)
        assert exc.value.code == 1

    @pytest.mark.parametrize(("label", "raw"), sorted(MALFORMED_ENTRIES.items()))
    def test_one_bad_entry_aborts_rather_than_being_skipped(self, label, raw):
        """This is the design decision, not an implementation detail. Dropping
        the row would let a scan run to completion while quietly never touching
        a channel the operator configured."""
        with pytest.raises(SystemExit) as exc:
            vi.configured_channels({"channels": raw}, strict=True)
        assert exc.value.code == 1

    def test_a_healthy_config_is_untouched_by_strict(self):
        cfg = {"channels": [GOOD]}
        assert vi.configured_channels(cfg, strict=True) == [GOOD]

    def test_the_error_names_the_offending_entry_and_the_fix(self, caplog):
        """A diagnostic that says only 'invalid config' sends the operator
        hunting through a 74-channel file."""
        with caplog.at_level("ERROR"), pytest.raises(SystemExit):
            vi.configured_channels({"channels": [GOOD, "oops"]}, strict=True)
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "entry 1" in joined and "oops" in joined, joined
        assert "- name: oops" in joined, f"the message does not show the correction: {joined}"


class TestTheAccessorIsTheOnlyReader:
    """One definition, N consumers - the same shape as `ENTRY_TIMESTAMP_PATTERN`
    (#195), `TS_MINUTES` (#197) and `match_configured_channel` (#205).
    Scattering the guard is exactly how these thirteen sites drifted apart.
    """

    @staticmethod
    def _receiver_is_config(node) -> bool:
        """True when the thing being read looks like the config dict.

        Covers `config.get(...)`, `cfg[...]`, and `(config or {}).get(...)` -
        that last shape is why this is a walk over AST rather than a grep: the
        `config.get("channels"` text search that seeded this fix missed
        `_infer_profile` entirely.
        """
        target = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func.value
        elif isinstance(node, ast.Subscript):
            target = node.value
        if isinstance(target, ast.BoolOp) and target.values:
            target = target.values[0]
        name = getattr(target, "id", None)
        return isinstance(name, str) and ("config" in name.lower() or name.lower() in {"cfg", "conf"})

    def _direct_reads(self, source: str = SOURCE) -> list[str]:
        """Every `config["channels"]` / `.get("channels")` read OUTSIDE the
        accessor. An AST walk rather than a grep, because a comment mentioning
        the key must not count as a read - and this file's guardrails
        deliberately quote the old expression in prose.
        """
        tree = ast.parse(source)
        accessor_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "configured_channels":
                accessor_lines = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        hits = []
        for node in ast.walk(tree):
            # Module and a few node kinds carry no position; only positioned
            # nodes can be attributed to (or excluded from) the accessor.
            if getattr(node, "lineno", None) is None or node.lineno in accessor_lines:
                continue
            # Only reads off the CONFIG. `topics.json` structures carry their
            # own `channels` key (a topic's channel rollup) and are unrelated -
            # flagging those would be a false alarm, and a guard that cries
            # wolf gets its exclusions widened until it guards nothing.
            if not self._receiver_is_config(node):
                continue
            # config.get("channels", ...)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "channels"
            ):
                hits.append(f"line {node.lineno}: .get('channels')")
            # config["channels"]
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "channels"
            ):
                hits.append(f"line {node.lineno}: ['channels']")
        return hits

    def test_no_other_site_reads_the_key_directly(self):
        assert self._direct_reads() == [], (
            f"these sites bypass configured_channels() and will crash on a malformed watchlist: {self._direct_reads()}"
        )

    def test_the_walk_is_not_vacuous(self):
        """A REAL positive control: feed `_direct_reads` a synthetic module that
        contains a genuine bypassing read and require it to be found.

        The first version re-implemented the matcher inline, WITHOUT the
        receiver filter and WITHOUT the accessor line-exclusion - so it was a
        control for a different function. A reviewer proved it: inserting
        `return []` at the top of `_direct_reads` left all 29 tests green.
        Same tautology class as the issue #182 field-inventory walk.
        """
        synthetic = "def somewhere(config):\n    return config.get('channels', [])\n"
        assert self._direct_reads(synthetic), "the walk cannot see a plain bypassing read - it has stopped matching"
        subscript = "def somewhere(cfg):\n    return cfg['channels']\n"
        assert self._direct_reads(subscript), "the walk misses subscript reads"

    def test_the_receiver_filter_accepts_config_and_rejects_topics_data(self):
        """The filter is where a false alarm would come from, so pin both
        directions. Three of the four sites the unfiltered walk flagged were
        `topics_data.get("channels")` and `entry["channels"]` - a topic's own
        channel rollup, unrelated to the watchlist. Flagging those would get the
        exclusions widened until the guard guards nothing.
        """
        accept = ast.parse('config.get("channels", [])').body[0].value
        accept_or = ast.parse('(config or {}).get("channels", [])').body[0].value
        accept_sub = ast.parse('cfg["channels"]').body[0].value
        reject = ast.parse('topics_data.get("channels", {})').body[0].value
        reject_sub = ast.parse('entry["channels"]').body[0].value

        cls = TestTheAccessorIsTheOnlyReader
        assert cls._receiver_is_config(accept), "a plain config read is not recognized"
        assert cls._receiver_is_config(accept_or), (
            "the `(config or {})` shape is not recognized - this is the exact shape the grep missed in _infer_profile"
        )
        assert cls._receiver_is_config(accept_sub)
        assert not cls._receiver_is_config(reject), "a topics.json read would be a false alarm"
        assert not cls._receiver_is_config(reject_sub)

    def test_every_call_site_is_classified_by_NAME_not_by_a_slack_count(self):
        """The first version was `SOURCE.count(...) >= 4` / `>= 5`, which
        tolerated one strict flip and three lenient flips and never said which
        site should be which. A reviewer flipped `cmd_scan` to lenient and
        `cmd_status` to strict; the suite stayed green both times.

        So the classification is a TABLE, keyed by the enclosing function, and
        every call site in the module must appear in it. A new caller has to be
        classified deliberately, exactly like `CONFIG_BACKUP_COMMANDS`.
        """
        tree = ast.parse(SOURCE)
        found: dict[str, bool] = {}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "configured_channels"
                ):
                    strict_kw = next((k for k in node.keywords if k.arg == "strict"), None)
                    # `strict=True` -> always strict. `strict=not args.channel`
                    # -> strict for a whole-list run, which is what the rule is
                    # about, so it counts as strict here. No keyword -> lenient.
                    is_strict = strict_kw is not None
                    found[fn.name] = found.get(fn.name, False) or is_strict

        # The one place that is neither: the accessor's own recursion-free body.
        found.pop("configured_channels", None)
        assert found == EXPECTED_STRICTNESS, (
            "a configured_channels call site is unclassified or changed mode.\n"
            f"  found:    {dict(sorted(found.items()))}\n"
            f"  expected: {dict(sorted(EXPECTED_STRICTNESS.items()))}"
        )


class TestRequireChannelsConfigNoLongerAcceptsTruthyGarbage:
    """`if not config.get("channels")` passed for any TRUTHY value, so
    `channels: alpha` (a non-empty string) sailed through the guard whose whole
    job is to stop a curate command without a watchlist, and crashed further
    in with a less useful message."""

    def test_a_bare_string_is_refused_here_not_deeper(self):
        with pytest.raises(SystemExit) as exc:
            vi.require_channels_config({"channels": "alpha"})
        assert exc.value.code == 1

    def test_an_absent_watchlist_still_gets_its_own_message(self, caplog):
        with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
            vi.require_channels_config({})
        assert exc.value.code == 1
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "requires 'channels:'" in joined, joined

    def test_a_healthy_watchlist_passes(self):
        vi.require_channels_config({"channels": [GOOD]})


# Every function that reads the watchlist, and whether it aborts on a bad row.
#
# STRICT means "this run iterates the WHOLE watchlist, so a dropped row is a
# creator silently going unscanned". LENIENT means the command is read-only, is
# a convenience lookup, or the operator named one channel.
#
# `cmd_scan` and `cmd_concepts` pass `strict=not args.channel`: they are strict
# for a whole-list run and lenient when a channel is named, which is why they
# read as strict here.
EXPECTED_STRICTNESS = {
    "require_channels_config": False,
    "cmd_scan": True,
    "cmd_concepts": True,
    "cmd_dedupe": True,
    "cmd_prune_shorts": True,
    "cmd_status": False,
    "channel_config_by_name": False,
    "match_configured_channel": False,
    "infer_channel_from_file_path": False,
    "collect_headline_channels": False,
    "_infer_profile": False,
    "_cmd_mindmap_impl": False,
    "_cmd_transcript_impl": False,
    "_cmd_process_impl": False,
}


class TestTheClassificationHoldsAtTheCaller:
    """Source-level classification is not enough: a reviewer replaced
    `cmd_status`'s accessor call with `settings = config` /
    `settings.get("channels", [])` - reintroducing the ticket's exact crash -
    and all 29 tests stayed green, because every one called the accessor
    directly or walked source with a receiver filter blind to `settings`.

    This is verbatim what issue #205 invariant 6 calls "the sharpest lesson of
    the ticket". These drive the real commands.
    """

    @pytest.fixture
    def wired(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vi, "resolve_output_dir", lambda *a, **k: tmp_path)
        monkeypatch.setattr(vi, "load_taxonomy", lambda *a, **k: {"concepts": {}})
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        return tmp_path

    @pytest.mark.parametrize(("label", "raw"), sorted(MALFORMED_ENTRIES.items()))
    def test_status_degrades_rather_than_aborting(self, label, raw, wired):
        """Read-only. One bad row must never cost the operator their status."""
        from types import SimpleNamespace

        try:
            vi.cmd_status(SimpleNamespace(channel=None), {"channels": raw})
        except SystemExit as e:
            pytest.fail(f"status aborted (exit {e.code}) on {label}")

    @pytest.mark.parametrize(("label", "raw"), sorted(MALFORMED_ENTRIES.items()))
    def test_a_named_channel_run_is_not_blocked_by_an_unrelated_bad_row(self, label, raw, wired):
        """CONFIRMED REGRESSION in the first cut, A/B'd against origin/main:
        `dedupe --channel beta` and `prune-shorts --channel beta` went from
        running normally to SystemExit(1) when an UNRELATED row was malformed.

        Neither command even reads the list when a channel is named - the abort
        came from `require_channels_config` having been made strict. That
        removed the operator's escape hatch of working on one healthy channel
        while the watchlist is broken, and `prune-shorts --channel` is exactly
        the recovery this repo's docs recommend elsewhere.
        """
        from types import SimpleNamespace

        for name, fn in (("dedupe", vi.cmd_dedupe), ("prune-shorts", vi.cmd_prune_shorts)):
            args = SimpleNamespace(channel="beta", apply=False, dry_run=True)
            try:
                fn(args, {"channels": raw})
            except SystemExit as e:
                if e.code:
                    pytest.fail(f"{name} --channel beta aborted (exit {e.code}) on an unrelated {label}")
            except Exception:
                # Anything else (no corpus on disk, etc.) is not this test's
                # business - only a config-driven ABORT is.
                pass

    def test_require_channels_config_still_refuses_a_watchlist_with_nothing_usable(self):
        """The lenient change must not weaken the guard's actual job."""
        with pytest.raises(SystemExit) as exc:
            vi.require_channels_config({"channels": "alpha"})
        assert exc.value.code == 1

    def test_require_channels_config_passes_a_list_with_one_bad_row_and_one_good(self):
        """The regression above, at the guard itself."""
        vi.require_channels_config({"channels": [{"url": "u"}, GOOD]})


class TestEveryRejectionIsReportedBeforeExiting:
    """An operator with three bad rows should fix them in one pass, not
    discover them across three runs (review P3)."""

    def test_strict_reports_all_three_then_exits_once(self, caplog):
        cfg = {"channels": ["a", {"url": "u"}, {"name": 42}, GOOD]}
        with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
            vi.configured_channels(cfg, strict=True)
        assert exc.value.code == 1
        reported = [r.getMessage() for r in caplog.records]
        for entry in ("entry 0", "entry 1", "entry 2"):
            assert any(entry in m for m in reported), f"{entry} was never reported: {reported}"

    def test_the_mapping_hint_only_appears_for_a_mapping(self, caplog):
        """It used to be appended for every non-list type, so a bare string got
        'got str. A mapping usually means the dashes were omitted' - a hint
        contradicting the type it had just printed."""
        with caplog.at_level("WARNING"):
            vi.configured_channels({"channels": "alpha"})
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "got str" in joined and "dashes were omitted" not in joined, joined

        caplog.clear()
        with caplog.at_level("WARNING"):
            vi.configured_channels({"channels": {"name": "a"}})
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "dashes were omitted" in joined, joined

    def test_a_none_config_does_not_raise(self):
        """Only `_infer_profile` defended with `config or {}`; the accessor
        owns it now."""
        assert vi.configured_channels(None) == []
