"""A channel-less config must not overwrite the record of a channel-ful one (#156).

OBSERVED on the live corpus, 2026-08-23, during an overnight run that created
three worktrees:

    config.latest.yaml         40140 bytes   59 channels
    config.2026-08-23-2.yaml   40140 bytes   59 channels
    config.2026-08-23.yaml       778 bytes    0 channels   <-- from a worktree
    config.2026-08-22-5.yaml   40140 bytes   59 channels

`config.yaml` is gitignored, so a fresh worktree has no plugin-local config and
`load_config` falls through to `~/.video-intel/config.yaml` - the read-only
search install, whose `output_dir` points at the SAME corpus. The backup helper
then snapshotted that channel-less config over the corpus's own history.

That run got lucky on ORDERING: a later full-config command restored
`config.latest.yaml` to 59 channels. Had the worktree command run last, `latest`
would claim zero channels and every subsequent scan would content-compare
against a record that is actively wrong.

The guard is deliberately narrow - the zero-versus-nonzero case only, never a
"suspiciously large drop" heuristic. A deliberate prune from 59 channels to 3 is
a legitimate edit that must still snapshot, and "how big a drop is suspicious"
is an invented threshold of exactly the kind this repo has learned not to ship.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import video_intel as vi  # noqa: E402

CHANNELFUL = b"""output_dir: G:/My Drive/video-intel
model: gemini-3.7-flash
channels:
  - name: alpha
    url: https://www.youtube.com/@alpha
  - name: beta
    url: https://www.youtube.com/@beta
"""

# Byte-for-byte the shape of the real `~/.video-intel/config.yaml`: an
# output_dir pointing at the same corpus, and no `channels:` key at all.
CHANNELLESS = b"""output_dir: G:/My Drive/video-intel
vector_db_dir: C:/Users/danie/video-intel-cache/lancedb
"""


def _code_without_docstring(fn) -> str:
    """Source of `fn` with its docstring removed.

    The first cut of the drift guard below read `inspect.getsource(...)` whole
    and failed on the function's OWN docstring, which legitimately quotes
    `raw.get("channels")` while explaining why the code must not do that. A
    guard that reads prose is not reading the code it guards.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    if ast.get_docstring(node) is not None:
        node.body = node.body[1:]
    return ast.unparse(node)


def test_the_docstring_stripper_actually_strips():
    """Companion: if this stopped stripping, the guard above would read prose
    again and could pass or fail for reasons unrelated to the code."""
    code = _code_without_docstring(vi._config_bytes_declare_channels)
    assert "Does this config text name any usable channel" not in code
    assert "configured_channels" in code, "stripping removed the body, not just the docstring"


def _backup_dir(tmp_path: Path) -> Path:
    return tmp_path / vi.CONFIG_BACKUP_DIR_NAME


def _seed_latest(tmp_path: Path, content: bytes) -> Path:
    d = _backup_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    latest = d / "config.latest.yaml"
    latest.write_bytes(content)
    return latest


def _run(tmp_path: Path, incoming: bytes, monkeypatch, source_name: str = "config.yaml"):
    """Drive the REAL helper with a real config file on disk."""
    src = tmp_path / source_name
    src.write_bytes(incoming)
    monkeypatch.setattr(vi, "_LAST_RESOLVED_SOURCE", f"test:{source_name}", raising=False)
    return vi.backup_config_if_changed(tmp_path, config_path=src)


class TestTheObservedDefect:
    def test_a_channelless_config_does_not_overwrite_a_channelful_record(self, tmp_path, monkeypatch, caplog):
        latest = _seed_latest(tmp_path, CHANNELFUL)
        with caplog.at_level("WARNING", logger="video_intel"):
            result = _run(tmp_path, CHANNELLESS, monkeypatch)

        assert result is None, "the helper reported a snapshot it must not have written"
        assert latest.read_bytes() == CHANNELFUL, "the record of the channel list was overwritten"
        dated = sorted(_backup_dir(tmp_path).glob("config.2*.yaml"))
        assert dated == [], f"a spurious dated snapshot was written: {[p.name for p in dated]}"

    def test_the_warning_names_the_source_and_the_file_it_protected(self, tmp_path, monkeypatch, caplog):
        """A silent decline reproduces the month-long gap this feature exists to
        prevent (invariant 4: a failure is never silent)."""
        _seed_latest(tmp_path, CHANNELFUL)
        with caplog.at_level("WARNING", logger="video_intel"):
            _run(tmp_path, CHANNELLESS, monkeypatch)
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert warnings, "declining to snapshot must not be silent"
        msg = warnings[0]
        assert "DECLINED" in msg
        assert "config.latest.yaml" in msg
        assert "worktree" in msg, "the message must name the situation the operator is in"

    def test_the_decline_is_not_an_error_for_the_caller(self, tmp_path, monkeypatch):
        """Invariant 3: a backup outcome never aborts the command that triggered
        it. Declining returns None exactly like 'nothing changed' does."""
        _seed_latest(tmp_path, CHANNELFUL)
        assert _run(tmp_path, CHANNELLESS, monkeypatch) is None


class TestTheGuardStaysNarrow:
    def test_the_first_ever_snapshot_still_writes_even_channelless(self, tmp_path, monkeypatch):
        """A user whose ONLY config is the channel-less user-level one must still
        get snapshots. Nothing is being protected when no record exists yet."""
        result = _run(tmp_path, CHANNELLESS, monkeypatch)
        assert result is not None, "the first-ever snapshot was refused"
        assert (_backup_dir(tmp_path) / "config.latest.yaml").read_bytes() == CHANNELLESS

    def test_a_channelful_config_still_overwrites_a_channelless_record(self, tmp_path, monkeypatch):
        """The recovery direction must stay open: after a bad snapshot has
        landed, a real config has to be able to repair the record."""
        _seed_latest(tmp_path, CHANNELLESS)
        result = _run(tmp_path, CHANNELFUL, monkeypatch)
        assert result is not None
        assert (_backup_dir(tmp_path) / "config.latest.yaml").read_bytes() == CHANNELFUL

    def test_channelless_over_channelless_still_writes(self, tmp_path, monkeypatch):
        """Nothing is protected, and the content genuinely differs, so a real
        edit to a channel-less config must still be recorded."""
        _seed_latest(tmp_path, CHANNELLESS)
        edited = CHANNELLESS + b"log_level: debug\n"
        assert _run(tmp_path, edited, monkeypatch) is not None

    def test_a_deliberate_prune_is_not_blocked(self, tmp_path, monkeypatch):
        """59 channels down to 1 is a legitimate edit. The guard is
        zero-versus-nonzero, never a magnitude heuristic - this test fails if a
        future edit adds a 'suspicious drop' threshold."""
        _seed_latest(tmp_path, CHANNELFUL)
        pruned = b"output_dir: G:/x\nchannels:\n  - name: alpha\n    url: https://y/@a\n"
        assert _run(tmp_path, pruned, monkeypatch) is not None


class TestUnanswerableIsNotChannelless:
    """`None` (cannot tell) must never be treated as `False` (no channels).
    Refusing on a parse failure would invent a new way for the backup to stop
    backing up - the exact failure mode the feature exists to prevent."""

    def test_an_unparseable_incoming_config_is_still_snapshotted(self, tmp_path, monkeypatch):
        _seed_latest(tmp_path, CHANNELFUL)
        assert _run(tmp_path, b"channels: [unclosed\n  - {", monkeypatch) is not None

    def test_a_non_mapping_incoming_config_is_still_snapshotted(self, tmp_path, monkeypatch):
        _seed_latest(tmp_path, CHANNELFUL)
        assert _run(tmp_path, b"- just\n- a\n- list\n", monkeypatch) is not None

    def test_an_unparseable_latest_does_not_block_the_write(self, tmp_path, monkeypatch):
        """If the existing record cannot be read as YAML we cannot claim it has
        channels, so there is nothing to protect."""
        _seed_latest(tmp_path, b"channels: [unclosed\n")
        assert _run(tmp_path, CHANNELLESS, monkeypatch) is not None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (CHANNELFUL, True),
            (CHANNELLESS, False),
            (b"channels:\n", False),  # key present, value None
            (b"channels: []\n", False),  # explicitly zero
            (b"channels: alpha\n", False),  # bare string: names no usable channel
            (b"channels:\n  - alpha\n", False),  # scalar entry, no name
            (b"{{{not yaml", None),
            (b"- a\n- b\n", None),  # not a mapping
        ],
    )
    def test_the_helper_answers_each_shape(self, raw, expected):
        assert vi._config_bytes_declare_channels(raw) is expected


class TestTheHelperReusesTheOneReader:
    def test_channel_counting_goes_through_configured_channels(self):
        """Issue #213 made `configured_channels` the ONE reader of that key, and
        it already knows the four YAML shapes that name no usable channel. A
        re-derived `parsed.get("channels")` here would drift from it - the same
        checker-versus-writer class this repo keeps getting bitten by."""
        code = _code_without_docstring(vi._config_bytes_declare_channels)
        assert "configured_channels(" in code
        assert '"channels"' not in code, "the key is re-read directly instead of via the one reader"

    def test_it_asks_leniently_so_a_bad_entry_cannot_exit_the_process(self):
        """`configured_channels(strict=True)` calls sys.exit. A backup helper
        must never abort the command that triggered it (invariant 3)."""
        code = _code_without_docstring(vi._config_bytes_declare_channels)
        assert "strict=False" in code

    def test_a_malformed_entry_does_not_raise_out_of_the_backup(self, tmp_path, monkeypatch):
        """End-to-end: the four #213 shapes reach the real helper without a
        traceback escaping into the caller."""
        _seed_latest(tmp_path, CHANNELFUL)
        for raw in (b"channels:\n", b"channels: alpha\n", b"channels:\n  - alpha\n"):
            src = tmp_path / "config.yaml"
            src.write_bytes(raw)
            assert vi.backup_config_if_changed(tmp_path, config_path=src) is None


class TestReadingTheRecordCanNeverCrashTheCaller:
    """Found by review, reproduced live: the first cut caught only
    `(yaml.YAMLError, UnicodeDecodeError, ValueError)`.

    This PR introduced the FIRST code path that semantically parses
    `config.latest.yaml` - before it, `latest` was only ever byte-compared - so
    it is a new risk surface on a file nobody validates. A deeply nested
    document raises `RecursionError`, which none of those three cover, and it
    escapes into `cmd_scan`'s own UNWRAPPED call to `backup_config_if_changed`
    (the deliberate "point of record, before any fetch" duplicate). `main()`'s
    dispatch is a bare try/finally with no `except`, so a corrupted backup
    mirror would end an entire scan on an otherwise healthy corpus - a direct
    contradiction of invariant 3, "it never aborts the caller".
    """

    def test_a_recursion_bomb_in_latest_does_not_escape(self, tmp_path, monkeypatch):
        _seed_latest(tmp_path, b"a: " + b"[" * 3000 + b"]" * 3000)
        # Must return, not raise. Writing is the CORRECT outcome: an unparseable
        # record cannot be shown to have channels, so nothing is protected.
        assert _run(tmp_path, CHANNELLESS, monkeypatch) is not None

    def test_a_recursion_bomb_in_the_incoming_config_does_not_escape(self, tmp_path, monkeypatch):
        _seed_latest(tmp_path, CHANNELFUL)
        assert _run(tmp_path, b"a: " + b"[" * 3000 + b"]" * 3000, monkeypatch) is not None

    def test_the_helper_answers_none_rather_than_raising(self):
        """The contract is "None means cannot tell", and every parse failure
        means exactly that - so there is no shape for which a narrower catch
        gives a better answer, only shapes where it gives a traceback instead
        of an answer."""
        assert vi._config_bytes_declare_channels(b"a: " + b"[" * 3000 + b"]" * 3000) is None

    def test_the_catch_is_broad_on_purpose(self):
        """A future tidy-up narrowing this to specific exception types would
        reopen the crash. Same reasoning as `_read_meta_best_effort`."""
        code = _code_without_docstring(vi._config_bytes_declare_channels)
        assert "except Exception:" in code, "the catch was narrowed; a parse failure must never reach the caller"
