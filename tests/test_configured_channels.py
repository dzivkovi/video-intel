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

    def _direct_reads(self) -> list[str]:
        """Every `config["channels"]` / `.get("channels")` read OUTSIDE the
        accessor. An AST walk rather than a grep, because a comment mentioning
        the key must not count as a read - and this file's guardrails
        deliberately quote the old expression in prose.
        """
        tree = ast.parse(SOURCE)
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
        """Companion, per the standing rule: an AST walk that stops matching
        turns the check above into `assert [] == []` forever. Prove it finds a
        read when one exists, using the accessor's OWN read - which the check
        excludes by line range, so this is a real positive control."""
        tree = ast.parse(SOURCE)
        accessor_reads = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get"
            and n.args
            and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == "channels"
        ]
        assert accessor_reads, "the walk finds no read at all - it has stopped matching"

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

    def test_both_modes_are_actually_used_in_production(self):
        """A `strict` parameter nothing passes is a parameter that will drift.
        Curate paths must pass True; the default covers the read-only ones."""
        assert SOURCE.count("configured_channels(config, strict=True)") >= 4, (
            "the curate paths stopped opting into strict"
        )
        assert SOURCE.count("configured_channels(config)") >= 5, "the read-only paths stopped using the accessor"


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
