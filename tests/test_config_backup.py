"""Mandatory config snapshots before any corpus-mutating command.

Why this is code and not a documented habit: the manual routine FAILED. The
corpus went from 2026-07-22 to 2026-08-17 with no snapshot while the channel
list was actively edited (YC Shorts curation, skip_video_ids blocklists, a
headline_digest flag). The routine existed and was written down; it simply did
not run, because it depended on a human or an agent remembering. Per the
durability ladder in CLAUDE.md, a failure a future run can hit with nobody
noticing must become code.

The tests below lock the five behaviours that make this a backup rather than a
habit, plus a dispatch-parity check so a NEW corpus-mutating subcommand cannot
silently opt out of snapshotting.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re
import textwrap

import pytest

import video_intel
from video_intel import (
    CONFIG_BACKUP_COMMANDS,
    CONFIG_BACKUP_DIR_NAME,
    backup_config_if_changed,
)

CFG_A = b"model: gemini-3.5-flash\nchannels:\n  - name: alpha\n"
CFG_B = b"model: gemini-3.5-flash\nchannels:\n  - name: alpha\n  - name: beta\n"


def _is_args_command(node: ast.AST) -> bool:
    """True for the attribute access `args.command`, either side of a Compare."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "command"
        and isinstance(node.value, ast.Name)
        and node.value.id == "args"
    )


def _str_constants(node: ast.AST) -> list[str]:
    """Collect string literals out of a single comparator.

    A plain `ast.Constant` covers `== "scan"`; a `Tuple`/`List`/`Set` covers the
    `in ("a", "b")` membership form. Non-str constants (True, None, 1, ...) are
    dropped on purpose - they cannot be dispatch command names.
    """
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else []
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [elt.value for elt in node.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
    return []


def _case_str_constants(pattern: ast.AST) -> list[str]:
    """Collect string literals out of a `case` pattern.

    `case "scan":` is a MatchValue wrapping a Constant; `case "a" | "b":` is a
    MatchOr of those. `case "x" as selected:` is a MatchAs wrapping a
    sub-pattern in `.pattern` - not recursing into it was a real bug (proven
    by executing python against this exact shape): it returned nothing while
    `_registered_commands` still found "x", so the parity test would REJECT a
    perfectly valid `as`-bound case as a mismatch. A guard clause
    (`case "x" if enabled:`) needs no such handling - the guard lives on
    `ast.match_case.guard`, not on the pattern, so it never reaches this
    function at all. A bare capture (`case selected:`) or wildcard
    (`case _:`) is also a MatchAs, but with `pattern is None`, and names no
    literal command.
    """
    if isinstance(pattern, ast.MatchValue):
        return _str_constants(pattern.value)
    if isinstance(pattern, ast.MatchOr):
        out: list[str] = []
        for sub in pattern.patterns:
            out.extend(_case_str_constants(sub))
        return out
    if isinstance(pattern, ast.MatchAs):
        if pattern.pattern is not None:
            return _case_str_constants(pattern.pattern)
        return []
    return []


def _dispatch_commands(src: str) -> set[str]:
    """Extract dispatch command names from source text via the AST, not regex.

    Why AST and not `re.findall(r'args\\.command == "([^"]+)"', src)`: the regex
    is quote-sensitive. A future branch written `args.command == 'x'` (single
    quotes) would be silently dropped from the inventory while the test's
    `assert dispatched` guard still passes - the test would keep passing while
    quietly losing its ability to catch a subcommand that dodges
    classification. The regex is also blind to `in (...)` membership dispatch
    and can match text inside a string literal or a comment. Walking `Compare`
    nodes sidesteps all three: it sees exactly the comparisons Python itself
    will evaluate, regardless of quoting style, comparison shape, or where the
    text `args.command == "..."` happens to appear as a substring. A
    `match args.command:` statement is inventoried through its `case` patterns
    for the same reason.
    """
    tree = ast.parse(textwrap.dedent(src))
    commands: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Match) and _is_args_command(node.subject):
            # `match args.command:` / `case "scan":`. A 16-branch if/elif chain is
            # the single most likely thing a future refactor rewrites as a match
            # statement, and a PARTIAL conversion is the exact silent-drop this
            # helper exists to prevent: the un-converted branches keep the
            # inventory non-empty, so `assert dispatched` still passes while the
            # converted subcommands vanish from classification.
            for case in node.cases:
                commands.update(_case_str_constants(case.pattern))
            continue
        if not isinstance(node, ast.Compare):
            continue
        # Comparison chaining (`a == b == c`) puts several ops/comparators on
        # one Compare node; zip them instead of only looking at index 0 so a
        # chained dispatch condition is not silently truncated.
        left = node.left
        # ast.Compare structurally guarantees len(ops) == len(comparators);
        # strict=True documents that expectation rather than defending
        # against a real case (the branch it would guard is unreachable).
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, ast.Eq):
                if _is_args_command(left):
                    commands.update(_str_constants(comparator))
                elif _is_args_command(comparator):
                    # Reversed form, e.g. `"scan" == args.command`.
                    commands.update(_str_constants(left))
            elif isinstance(op, ast.In) and _is_args_command(left):
                commands.update(_str_constants(comparator))
            left = comparator
    return commands


def _registered_commands(module_src: str) -> set[str]:
    """Extract every top-level subcommand name registered via `subparsers.add_parser(...)`.

    Anchored to the argparse registry rather than to any dispatch shape,
    because the registry is undodgeable: a subcommand cannot exist without
    being registered here, whereas `_dispatch_commands` can only see the
    comparison shapes it knows about (a future `args.command == CMD_CONST`,
    a dict dispatch, or a chain moved into a helper function would silently
    vanish from that inventory while `assert dispatched` stays green).

    The receiver filter (bare `ast.Name` with `id == "subparsers"`) is
    load-bearing and was verified empirically: `scripts/video_intel.py` also
    contains `profile_actions.add_parser("init")` / `add_parser("show")`,
    which are sub-actions of the `profile` subcommand, not top-level
    subcommands. Selecting by receiver name excludes them correctly. Do NOT
    select by "whichever receiver has the most add_parser calls" - that is
    a hack that would silently pick the wrong receiver if the code changed.
    """
    tree = ast.parse(textwrap.dedent(module_src))
    commands: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_parser"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "subparsers"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            commands.add(first.value)
    return commands


def _unparseable_registrations(module_src: str) -> list[str]:
    """Report every `subparsers.add_parser(...)` call whose name is not a string literal.

    `_registered_commands` silently ignores such a call (a `CMD = "x"` /
    `subparsers.add_parser(CMD)` registration, or a call with no positional
    args at all). Proven by executing python against exactly that shape: BOTH
    `_registered_commands` and `_dispatch_commands` return `set()` for "x", so
    the registry equals the dispatch inventory (both simply lack it), the
    parity test passes, and the union used by
    test_all_dispatch_commands_are_classified never contains "x" - a new
    subcommand can dodge classification entirely with every other guard
    green. Ignoring is what makes that silent; this helper exists to fail
    closed instead. It intentionally does not try to resolve a Name back to
    its assignment (e.g. following CMD to "x") - that is a rabbit hole and
    only a partial fix, whereas surfacing the call site as unparseable is
    complete and cheap: the fix is either to use a string literal or to
    extend the extractor, and the assertion message below says exactly that.
    """
    tree = ast.parse(textwrap.dedent(module_src))
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_parser"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "subparsers"):
            continue
        if not node.args:
            problems.append(f"line {node.lineno}: subparsers.add_parser() called with no positional args")
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            problems.append(
                f"line {node.lineno}: subparsers.add_parser(...) first arg is not a string literal ({ast.dump(first)})"
            )
    return problems


@pytest.fixture
def corpus(tmp_path):
    out = tmp_path / "corpus"
    out.mkdir()
    return out


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_bytes(CFG_A)
    return p


def _backups(corpus):
    d = corpus / CONFIG_BACKUP_DIR_NAME
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


class TestSnapshotIsWritten:
    def test_first_run_writes_dated_and_latest(self, corpus, cfg):
        written = backup_config_if_changed(corpus, config_path=cfg)
        assert written is not None
        names = _backups(corpus)
        assert "config.latest.yaml" in names
        assert any(re.fullmatch(r"config\.\d{4}-\d{2}-\d{2}\.yaml", n) for n in names), names
        assert (corpus / CONFIG_BACKUP_DIR_NAME / "config.latest.yaml").read_bytes() == CFG_A

    def test_backup_dir_is_created_when_absent(self, corpus, cfg):
        assert not (corpus / CONFIG_BACKUP_DIR_NAME).exists()
        backup_config_if_changed(corpus, config_path=cfg)
        assert (corpus / CONFIG_BACKUP_DIR_NAME).is_dir()


class TestContentComparedNotTimeBased:
    """Ten scans a day must not litter ten snapshots."""

    def test_unchanged_config_writes_nothing_second_time(self, corpus, cfg):
        backup_config_if_changed(corpus, config_path=cfg)
        before = _backups(corpus)
        assert backup_config_if_changed(corpus, config_path=cfg) is None
        assert _backups(corpus) == before

    def test_changed_config_writes_again(self, corpus, cfg):
        backup_config_if_changed(corpus, config_path=cfg)
        cfg.write_bytes(CFG_B)
        assert backup_config_if_changed(corpus, config_path=cfg) is not None
        assert (corpus / CONFIG_BACKUP_DIR_NAME / "config.latest.yaml").read_bytes() == CFG_B


class TestDatedSnapshotsAreImmutable:
    """Only config.latest.yaml is ever overwritten."""

    def test_second_different_edit_same_day_is_suffixed_not_clobbered(self, corpus, cfg):
        first = backup_config_if_changed(corpus, config_path=cfg)
        cfg.write_bytes(CFG_B)
        second = backup_config_if_changed(corpus, config_path=cfg)

        assert second is not None and second != first
        assert second.name.endswith("-2.yaml"), second.name
        assert first.read_bytes() == CFG_A, (
            "the morning's snapshot was clobbered by the afternoon edit; dated "
            "snapshots must be immutable or the history they exist to preserve is lost"
        )
        assert second.read_bytes() == CFG_B

    def test_third_edit_same_day_gets_next_suffix(self, corpus, cfg):
        backup_config_if_changed(corpus, config_path=cfg)
        cfg.write_bytes(CFG_B)
        backup_config_if_changed(corpus, config_path=cfg)
        cfg.write_bytes(b"model: x\nchannels: []\n")
        third = backup_config_if_changed(corpus, config_path=cfg)
        assert third.name.endswith("-3.yaml"), third.name


class TestNeverAbortsTheCaller:
    """A backup failure must not block a scan; it must also never be silent."""

    def test_unreadable_config_returns_none_and_warns(self, corpus, tmp_path, caplog):
        missing = tmp_path / "does-not-exist.yaml"
        with caplog.at_level("WARNING"):
            assert backup_config_if_changed(corpus, config_path=missing) is None
        assert any("Config backup skipped" in r.message for r in caplog.records)

    def test_unwritable_output_dir_returns_none_and_warns(self, cfg, tmp_path, monkeypatch, caplog):
        target = tmp_path / "corpus2"
        target.mkdir()

        def boom(*a, **kw):
            raise OSError("Incorrect function (os error 1)")

        monkeypatch.setattr(pathlib.Path, "mkdir", boom)
        with caplog.at_level("WARNING"):
            assert backup_config_if_changed(target, config_path=cfg) is None
        assert any("Config backup FAILED" in r.message for r in caplog.records)

    def test_no_resolved_path_warns_instead_of_silently_skipping(self, corpus, monkeypatch, caplog):
        """Env-var resolution names a directory, not a config file."""
        monkeypatch.setattr(video_intel, "_LAST_RESOLVED_PATH", None)
        with caplog.at_level("WARNING"):
            assert backup_config_if_changed(corpus) is None
        assert any("not a file" in r.message for r in caplog.records), (
            "a backup that stops backing up must say so; silence here is exactly "
            "how the 2026-07-22 -> 2026-08-17 gap went unnoticed"
        )

    def test_unreadable_latest_writes_fresh_snapshot_rather_than_assuming_unchanged(
        self, corpus, cfg, monkeypatch, caplog
    ):
        backup_config_if_changed(corpus, config_path=cfg)
        latest = corpus / CONFIG_BACKUP_DIR_NAME / "config.latest.yaml"
        real_read = pathlib.Path.read_bytes

        def flaky(self, *a, **kw):
            if self == latest:
                raise OSError("transient cloud-mount read error")
            return real_read(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "read_bytes", flaky)
        with caplog.at_level("WARNING"):
            # Config is byte-identical, but `latest` cannot be read to prove it.
            result = backup_config_if_changed(corpus, config_path=cfg)
        assert result is not None, (
            "an unreadable latest is not proof the config is unchanged; assuming "
            "safety there is how a real edit would go unsnapshotted"
        )


class TestResolvedPathIsTracked:
    def test_load_config_records_the_plugin_local_path(self, tmp_path, monkeypatch):
        skill = tmp_path / "skill"
        skill.mkdir()
        (skill / "config.yaml").write_text("output_dir: /tmp/x\nchannels: []\n", encoding="utf-8")
        monkeypatch.setattr(video_intel, "SKILL_DIR", skill)
        video_intel.load_config()
        assert skill / "config.yaml" == video_intel._LAST_RESOLVED_PATH


class TestEveryMutatingCommandSnapshots:
    """Dispatch parity: a new corpus-mutating subcommand cannot silently opt out.

    The command list is derived from the live dispatch table in main() rather
    than restated here, so the test and the code cannot agree by construction.
    Any command that is neither in CONFIG_BACKUP_COMMANDS nor in the explicit
    read-only allowlist fails this test until someone classifies it.
    """

    READ_ONLY = frozenset({"search", "status", "briefings", "profile"})

    # `nugget` writes a file (a brief under output_dir/_briefings/nuggets/) so
    # it is not READ_ONLY, but it is deliberately exempt from
    # CONFIG_BACKUP_COMMANDS (issue #147 guardrail 3): the write is
    # additive-only and never touches channel config or scan state, and
    # nugget is reachable from the globally installed search skill where the
    # resolved config is the channel-less user-level ~/.video-intel/config.yaml
    # - snapshotting THAT would overwrite config.latest.yaml (the record of
    # the channel list that produced the corpus) with a config that has no
    # channels at all, and would churn a dated snapshot on every query.
    WRITES_BUT_EXEMPT = frozenset({"nugget"})

    def test_all_dispatch_commands_are_classified(self):
        """Inventory is the UNION of the argparse registry and the dispatch table.

        Union, not either alone: a registered-but-undispatched command and a
        dispatched-but-unregistered command must both still be forced through
        classification.
        """
        main_src = inspect.getsource(video_intel.main)
        module_src = inspect.getsource(video_intel)
        inventory = _registered_commands(module_src) | _dispatch_commands(main_src)
        assert inventory, "could not parse the dispatch table or the argparse registry"
        unclassified = inventory - CONFIG_BACKUP_COMMANDS - self.READ_ONLY - self.WRITES_BUT_EXEMPT
        assert not unclassified, (
            f"subcommand(s) {sorted(unclassified)} are neither in "
            "CONFIG_BACKUP_COMMANDS, the read-only allowlist, nor WRITES_BUT_EXEMPT. "
            "Classify them: if the command can mutate the corpus it MUST snapshot "
            "the config first, unless it has a documented exemption."
        )

    def test_backup_set_contains_no_phantom_commands(self):
        """Inventory is the argparse registry ALONE, not the union and not the dispatch table.

        Now that the extractor supports `in (...)`, any unrelated
        `args.command in (...)` guard inside main() would ADD a name to the
        dispatch inventory and could mask a genuinely stale entry in
        CONFIG_BACKUP_COMMANDS, making this phantom check vacuous (a real
        example: `if args.command in CONFIG_BACKUP_COMMANDS:` near the top of
        main() is exactly such a guard). The argparse registry cannot be
        polluted by an in-function guard, so it is the only safe source for
        this particular check.
        """
        module_src = inspect.getsource(video_intel)
        registered = _registered_commands(module_src)
        assert registered, "could not parse the argparse registry"
        phantom = CONFIG_BACKUP_COMMANDS - registered
        assert not phantom, f"CONFIG_BACKUP_COMMANDS names non-existent command(s): {sorted(phantom)}"

    def test_dispatch_inventory_matches_the_argparse_registry(self):
        """The loud failure that catches an unrecognized dispatch shape.

        The AST extractor recognizes `==`, `in (...)`, and `match`/`case`
        forms; any OTHER dispatch shape (a constant comparand, a dict
        dispatch, a chain moved into a helper function) makes THIS test fail
        loudly instead of silently shrinking the inventory that
        test_all_dispatch_commands_are_classified relies on.
        """
        main_src = inspect.getsource(video_intel.main)
        module_src = inspect.getsource(video_intel)
        registered = _registered_commands(module_src)
        dispatched = _dispatch_commands(main_src)
        assert registered == dispatched, (
            "registered but not dispatched (dead subcommand, or a dispatch "
            f"shape the AST extractor does not recognize): {sorted(registered - dispatched)}; "
            "dispatched but not registered (dispatch table names a command "
            f"argparse never registers): {sorted(dispatched - registered)}"
        )

    def test_scan_is_in_the_set(self):
        assert "scan" in CONFIG_BACKUP_COMMANDS

    def test_every_subcommand_is_registered_with_a_string_literal(self):
        """Fail closed on a common-mode blind spot in BOTH extractors at once.

        Proven by executing python: `CMD = "x"` followed by
        `subparsers.add_parser(CMD)` and `args.command == CMD` makes
        `_registered_commands` AND `_dispatch_commands` both return `set()`
        for "x" - not just one of them. The registry and the dispatch
        inventory then agree (both lack "x"), so
        test_dispatch_inventory_matches_the_argparse_registry stays green,
        the union in test_all_dispatch_commands_are_classified never contains
        "x", and "x" dodges classification with every other guard passing.
        This test closes that gap by refusing to accept a non-literal
        registration at all: a command must be reachable via a string
        literal, or the extractor must be extended to understand the shape
        that names it. `_dispatch_commands` needs no equivalent fail-closed
        check for this same defect - once every registration is guaranteed
        literal, a dispatch branch compared against a constant is caught by
        the existing registered == dispatched equality test, because the
        registry will have the name and the dispatch inventory will not.
        """
        module_src = inspect.getsource(video_intel)
        unparseable = _unparseable_registrations(module_src)
        assert not unparseable, (
            "subcommand(s) registered without a string literal name - invisible to "
            f"both _registered_commands and _dispatch_commands at once: {unparseable}. "
            "Either register the command with a string literal, or extend "
            "_unparseable_registrations (and _registered_commands) to understand "
            "this shape."
        )

    def test_nugget_is_exempt_from_config_backup(self):
        """Issue #147 guardrail 3: nugget writes only its own brief under
        output_dir/_briefings/nuggets/, never channel config or scan state, so
        it does not need a config snapshot. Snapshotting it would actively
        harm the corpus: nugget is reachable from the globally installed
        search skill, where the resolved config is the channel-less
        user-level ~/.video-intel/config.yaml - snapshotting THAT would
        overwrite config.latest.yaml (the record of the channel list that
        produced the corpus) and churn a dated snapshot on every query.
        It is not READ_ONLY either, because it genuinely writes a file."""
        assert "nugget" in self.WRITES_BUT_EXEMPT
        assert "nugget" not in CONFIG_BACKUP_COMMANDS
        assert "nugget" not in self.READ_ONLY


class TestScanSnapshotsBeforeMutating:
    """The snapshot must precede the first fetch, not follow it.

    A backup taken after a scan has already mutated the corpus documents the
    wrong state - it records the config that produced the NEXT run's changes,
    not this one's.
    """

    def test_backup_call_precedes_first_fetch_in_cmd_scan(self):
        src = inspect.getsource(video_intel.cmd_scan)
        backup_at = src.find("backup_config_if_changed(")
        assert backup_at != -1, "cmd_scan must snapshot the config itself"
        fetch_positions = [
            src.find(name)
            for name in ("fetch_selective_videos", "fetch_channel_videos", "enrich_with_durations")
            if src.find(name) != -1
        ]
        assert fetch_positions, "could not locate any fetch call in cmd_scan"
        assert backup_at < min(fetch_positions), (
            "backup_config_if_changed must run BEFORE cmd_scan's first fetch/mutation"
        )


class TestDispatchExtractor:
    r"""Coverage for `_dispatch_commands` itself, per issue #151.

    The regex it replaces (`re.findall(r'args\.command == "([^"]+)"', src)`)
    worked only by accident of every existing branch happening to use double
    quotes. A future branch written with single quotes would have been
    silently dropped from the inventory while `assert dispatched` kept
    passing - the drift risk this class exists to close off.
    """

    def test_mixed_quoting_is_collected(self):
        src = "if args.command == \"alpha\":\n    pass\nelif args.command == 'beta':\n    pass\n"
        assert _dispatch_commands(src) == {"alpha", "beta"}

    def test_membership_form_is_collected(self):
        src = 'if args.command in ("gamma", "delta"):\n    pass\n'
        assert _dispatch_commands(src) == {"gamma", "delta"}

    def test_occurrences_inside_strings_and_comments_are_ignored(self):
        src = (
            '"""a docstring mentioning args.command == "phantom" as prose"""\n'
            '# args.command == "ghost"\n'
            'if args.command == "real":\n'
            "    pass\n"
        )
        assert _dispatch_commands(src) == {"real"}

    def test_reversed_form_is_collected(self):
        src = 'if "epsilon" == args.command:\n    pass\n'
        assert _dispatch_commands(src) == {"epsilon"}

    def test_non_str_comparand_is_ignored(self):
        src = (
            "if args.command == True:\n    pass\nif args.command == 1:\n    pass\nif args.command is None:\n    pass\n"
        )
        assert _dispatch_commands(src) == set()

    def test_never_drops_what_the_old_regex_found(self):
        """No regression against the regex it replaces - not parity.

        An `==` assertion here would go RED the moment anyone writes the
        single-quoted or `in (...)` branch this refactor exists to support,
        which is exactly the outcome the refactor is FOR. The AST extractor
        must never DROP a name the regex found; it is expected and intended
        to find MORE (membership forms, single quotes, match/case).
        """
        src = inspect.getsource(video_intel.main)
        old_regex_result = set(re.findall(r'args\.command == "([^"]+)"', src))
        assert old_regex_result, "the old regex found nothing; this guard is vacuous without a baseline"
        assert old_regex_result <= _dispatch_commands(src)

    def test_chained_comparison_is_collected(self):
        """Direct test for the `left = comparator` / `zip(ops, comparators)` multi-op path.

        Reverting to `node.ops[0]` / `node.comparators[0]` keeps every other
        test in this class green, which means that code path was untested
        before this test existed.
        """
        src = 'if "zeta" == args.command == "eta":\n    pass\n'
        assert _dispatch_commands(src) == {"zeta", "eta"}

    def test_list_literal_membership_is_collected(self):
        src = 'if args.command in ["theta"]:\n    pass\n'
        assert _dispatch_commands(src) == {"theta"}

    def test_set_literal_membership_is_collected(self):
        src = 'if args.command in {"iota"}:\n    pass\n'
        assert _dispatch_commands(src) == {"iota"}

    def test_match_statement_is_collected(self):
        src = (
            "match args.command:\n"
            '    case "alpha":\n'
            "        pass\n"
            '    case "beta" | "gamma":\n'
            "        pass\n"
            "    case _:\n"
            "        pass\n"
        )
        assert _dispatch_commands(src) == {"alpha", "beta", "gamma"}

    def test_match_on_different_subject_is_ignored(self):
        src = 'match args.other:\n    case "nope":\n        pass\n'
        assert _dispatch_commands(src) == set()

    def test_non_str_element_inside_membership_collection_is_dropped(self):
        src = 'if args.command in ("ok", 1, None):\n    pass\n'
        assert _dispatch_commands(src) == {"ok"}

    def test_name_comparand_is_a_known_blind_spot(self):
        """KNOWN and accepted limitation, not a bug to fix here.

        `args.command == CMD_CONST` compares against a Name, not a string
        literal, so `_str_constants` cannot resolve it and this extractor
        correctly (if silently) sees nothing. In practice this class of gap
        is caught by test_dispatch_inventory_matches_the_argparse_registry,
        which cross-checks against the argparse registry and fails loudly
        when a registered command never shows up here. The next reader
        should not assume this AST walk is exhaustive on its own.
        """
        src = 'CMD_SCAN = "scan"\nif args.command == CMD_SCAN:\n    pass\n'
        assert _dispatch_commands(src) == set()

    def test_registered_commands_excludes_a_nested_sub_action_parser(self):
        src = 'top_parser = subparsers.add_parser("top")\nnested_parser = profile_actions.add_parser("nested")\n'
        assert _registered_commands(src) == {"top"}

    def test_case_as_binding_is_collected(self):
        """Proven by execution: pre-fix, `case "x" as selected:` returned set() while
        `_registered_commands` found {"x"}, so the parity test would REJECT this
        perfectly valid refactor - a cry-wolf failure."""
        src = 'match args.command:\n    case "x" as selected:\n        pass\n'
        assert _dispatch_commands(src) == {"x"}

    def test_case_or_with_as_binding_is_collected(self):
        src = 'match args.command:\n    case ("x" | "y") as selected:\n        pass\n'
        assert _dispatch_commands(src) == {"x", "y"}

    def test_case_bare_capture_and_wildcard_yield_nothing(self):
        src = "match args.command:\n    case selected:\n        pass\n    case _:\n        pass\n"
        assert _dispatch_commands(src) == set()

    def test_case_guard_clause_is_collected(self):
        """Regression pin: a guard clause already worked pre-fix (verified by execution)
        because a guard lives on ast.match_case.guard, not on the pattern - it never
        touches _case_str_constants. Pinned here so a future change cannot break it
        unnoticed."""
        src = 'match args.command:\n    case "x" if enabled:\n        pass\n'
        assert _dispatch_commands(src) == {"x"}


class TestUnparseableRegistrations:
    """Coverage for `_unparseable_registrations`, the fail-closed half of the P2 fix."""

    def test_name_registration_is_reported(self):
        src = 'CMD = "x"\nsubparsers.add_parser(CMD)\n'
        problems = _unparseable_registrations(src)
        assert len(problems) == 1
        assert "line 2" in problems[0]

    def test_string_literal_registration_is_not_reported(self):
        src = 'subparsers.add_parser("ok")\n'
        assert _unparseable_registrations(src) == []

    def test_no_positional_args_is_reported(self):
        src = "subparsers.add_parser()\n"
        problems = _unparseable_registrations(src)
        assert len(problems) == 1
        assert "no positional args" in problems[0]

    def test_live_module_has_no_unparseable_registrations(self):
        """All 16 subcommands in scripts/video_intel.py register with a string
        literal today; this pins that fact so a future non-literal registration
        is caught immediately rather than silently dodging both extractors."""
        module_src = inspect.getsource(video_intel)
        assert _unparseable_registrations(module_src) == []
