"""Unit tests for the pure on-disk diagnostic logger (`diag_log.DiagLog`).

No Electrum imports: the module is deliberately standalone. `conftest.py` puts
the package dir on sys.path so `diag_log` imports directly.
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Dict

import pytest

from diag_log import (  # type: ignore  (package dir on sys.path via conftest)  # noqa: E402
    DiagLog,
    _sanitize_name,
    _day_from_ts,
)


def _ts(y: int, m: int, d: int, hh: int = 12) -> float:
    """Epoch seconds for a UTC calendar day (stable across the test host's tz)."""
    return datetime.datetime(y, m, d, hh, tzinfo=datetime.timezone.utc).timestamp()


def _entry(ts: float, **over) -> Dict:
    base = {
        "ts": ts, "category": "action", "kind": "swap", "amount_sat": 25_000,
        "source": "117x1x0", "dest": "on-chain", "reason": "channel over trigger",
        "detail": None, "state": {},
    }
    base.update(over)
    return base


# --- routing & format -----------------------------------------------------
def test_write_creates_dated_file_in_wallet_subfolder(tmp_path) -> None:
    log = DiagLog(str(tmp_path))
    ts = _ts(2026, 7, 2)
    assert log.write("mywallet", _entry(ts)) is True
    expected = tmp_path / "mywallet" / "inbound_liquidity-2026-07-02.log"
    assert expected.exists()
    lines = expected.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["reason"] == "channel over trigger"
    assert rec["time"] == "2026-07-02T12:00:00Z"   # readable ts injected
    assert rec["ts"] == ts                          # original fields preserved


def test_entries_route_to_per_day_files(tmp_path) -> None:
    log = DiagLog(str(tmp_path), retention_days=999)
    log.write("w", _entry(_ts(2026, 7, 1), reason="day1"))
    log.write("w", _entry(_ts(2026, 7, 2), reason="day2a"))
    log.write("w", _entry(_ts(2026, 7, 2), reason="day2b"))
    assert log.list_day_files("w") == [
        "inbound_liquidity-2026-07-01.log",
        "inbound_liquidity-2026-07-02.log",
    ]
    day2 = log.read_day("w", datetime.date(2026, 7, 2))
    assert [e["reason"] for e in day2] == ["day2a", "day2b"]  # append order


def test_write_does_not_mutate_callers_entry(tmp_path) -> None:
    log = DiagLog(str(tmp_path))
    e = _entry(_ts(2026, 7, 2))
    log.write("w", e)
    assert "time" not in e   # the readable ts is added to a copy, not the caller's dict


# --- name safety ----------------------------------------------------------
def test_wallet_name_is_sanitised_and_cannot_escape_base(tmp_path) -> None:
    log = DiagLog(str(tmp_path))
    log.write("../../etc/evil", _entry(_ts(2026, 7, 2)))
    # Nothing written outside base_dir; the traversal collapsed to a safe name.
    produced = list(tmp_path.rglob("inbound_liquidity-*.log"))
    assert len(produced) == 1
    assert tmp_path in produced[0].parents
    assert ".." not in str(produced[0].relative_to(tmp_path))


def test_sanitize_name_edges() -> None:
    assert _sanitize_name("a/b\\c") == "a_b_c"
    assert _sanitize_name("") == "wallet"
    assert _sanitize_name("...") == "wallet"
    assert _sanitize_name("good.name-1") == "good.name-1"


# --- retention pruning ----------------------------------------------------
def test_prune_drops_files_older_than_retention_on_write(tmp_path) -> None:
    log = DiagLog(str(tmp_path), retention_days=30)
    # Seed an old day and a recent day directly.
    log.write("w", _entry(_ts(2026, 6, 1), reason="old"))
    assert (tmp_path / "w" / "inbound_liquidity-2026-06-01.log").exists()
    # A write 30+ days later prunes the old file (window is [day-29 .. day]).
    log.write("w", _entry(_ts(2026, 7, 2), reason="new"))
    assert not (tmp_path / "w" / "inbound_liquidity-2026-06-01.log").exists()
    assert (tmp_path / "w" / "inbound_liquidity-2026-07-02.log").exists()


def test_retention_window_boundary_is_inclusive(tmp_path) -> None:
    # retention_days=30 keeps today and the 29 preceding days.
    log = DiagLog(str(tmp_path), retention_days=30)
    log.write("w", _entry(_ts(2026, 6, 3), reason="edge_kept"))     # 29 days before 7-2
    log.write("w", _entry(_ts(2026, 6, 2), reason="edge_dropped"))  # 30 days before -> out
    log.write("w", _entry(_ts(2026, 7, 2), reason="today"))
    files = log.list_day_files("w")
    assert "inbound_liquidity-2026-06-03.log" in files
    assert "inbound_liquidity-2026-06-02.log" not in files


def test_prune_ignores_foreign_files(tmp_path) -> None:
    log = DiagLog(str(tmp_path), retention_days=1)
    folder = tmp_path / "w"
    folder.mkdir()
    (folder / "notes.txt").write_text("keep me", encoding="utf-8")
    (folder / "inbound_liquidity-2020-01-01.log").write_text("old\n", encoding="utf-8")
    log.write("w", _entry(_ts(2026, 7, 2)))
    assert (folder / "notes.txt").exists()                              # untouched
    assert not (folder / "inbound_liquidity-2020-01-01.log").exists()   # pruned


# --- robustness -----------------------------------------------------------
def test_write_never_raises_on_bad_base_dir(tmp_path) -> None:
    # base_dir sits *under* a regular file -> makedirs must fail; write returns False.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    log = DiagLog(str(blocker / "logs"))
    assert log.write("w", _entry(_ts(2026, 7, 2))) is False


def test_read_day_missing_file_is_empty(tmp_path) -> None:
    log = DiagLog(str(tmp_path))
    assert log.read_day("nope", datetime.date(2026, 7, 2)) == []


def test_day_from_ts_is_utc() -> None:
    assert _day_from_ts(_ts(2026, 7, 2, hh=23)) == datetime.date(2026, 7, 2)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# --- robustness of the paths that touch the filesystem --------------------
#
# Everything below is an error branch, and the module's whole contract is that
# none of them can reach the plugin: `write` is called from inside an
# evaluation, so an exception escaping here would abort a tick that is moving
# money. They were the last uncovered lines in the file, which is the wrong
# state for a set of branches whose entire job is to not propagate.
def test_prune_survives_an_unreadable_folder(tmp_path) -> None:
    """`os.listdir` fails -- the folder was removed under us, or is not a
    folder at all. Pruning simply does nothing; the alternative is an exception
    on the write path."""
    log = DiagLog(str(tmp_path))
    (tmp_path / "w").write_text("i am a file, not a folder", encoding="utf-8")
    log._prune("w", datetime.date(2026, 7, 2))      # must not raise


def test_prune_skips_a_file_whose_date_is_not_a_date(tmp_path) -> None:
    """The filename pattern matches digit groups, not real dates, so
    `inbound_liquidity-2026-13-45.log` reaches the date parse and raises
    ValueError. It must be left alone rather than taking pruning down -- and
    leaving it alone is also the safe choice: we cannot say how old it is, so
    we cannot say it is expired."""
    log = DiagLog(str(tmp_path), retention_days=1)
    folder = tmp_path / "w"
    folder.mkdir()
    impossible = folder / "inbound_liquidity-2026-13-45.log"
    impossible.write_text("not a real date\n", encoding="utf-8")
    (folder / "inbound_liquidity-2020-01-01.log").write_text("old\n", encoding="utf-8")

    log.write("w", _entry(_ts(2026, 7, 2)))

    assert impossible.exists(), "a file with an unparseable date was deleted"
    assert not (folder / "inbound_liquidity-2020-01-01.log").exists(), \
        "the unparseable name stopped pruning reaching a genuinely old file"


def test_prune_survives_a_file_it_cannot_delete(tmp_path) -> None:
    """Deletion is refused (read-only directory, or the file is held open on
    another platform). Pruning must carry on and the write must still succeed:
    failing to free disk is not a reason to stop recording diagnostics."""
    log = DiagLog(str(tmp_path), retention_days=1)
    folder = tmp_path / "w"
    folder.mkdir()
    old = folder / "inbound_liquidity-2020-01-01.log"
    old.write_text("old\n", encoding="utf-8")
    # Pre-create today's file, because a read-only folder also forbids creating
    # one -- and it is the REMOVE that is under test here, not the create.
    today = folder / "inbound_liquidity-2026-07-02.log"
    today.write_text("", encoding="utf-8")

    os.chmod(folder, 0o500)                       # r-x: listable, not writable
    try:
        assert log.write("w", _entry(_ts(2026, 7, 2))) is True, \
            "an undeletable old file must not stop today's entry being written"
        assert old.exists(), "the test did not actually prevent deletion"
    finally:
        os.chmod(folder, 0o700)                   # so pytest can clean up


def test_the_capped_notice_never_raises_when_it_cannot_be_written(tmp_path) -> None:
    """The notice is itself a write, so it has the same duty not to propagate.
    Pointed at a path under a file, which cannot be opened."""
    log = DiagLog(str(tmp_path))
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    log._write_capped_notice(str(blocker / "nope" / "x.log"), _ts(2026, 7, 2))


def test_read_day_tolerates_blank_and_truncated_lines(tmp_path) -> None:
    """A crash or a full disk mid-write leaves a partial last line, and the
    reader must return the entries that ARE intact rather than nothing.

    This is the case that matters for the file's purpose: the whole reason to
    keep diagnostics on disk is to read them after something went wrong, which
    is exactly the circumstance that truncates the last line.
    """
    log = DiagLog(str(tmp_path))
    day = datetime.date(2026, 7, 2)
    log.write("w", _entry(_ts(2026, 7, 2), reason="first"))
    log.write("w", _entry(_ts(2026, 7, 2), reason="second"))
    path = tmp_path / "w" / "inbound_liquidity-2026-07-02.log"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n")                             # a blank line
        fh.write('{"ts": 123, "reason": "trunca')  # and a half-written one

    rows = log.read_day("w", day)
    assert [r["reason"] for r in rows] == ["first", "second"], (
        f"a truncated tail lost the intact entries before it: {rows}")


def test_list_day_files_on_an_unreadable_folder_is_empty(tmp_path) -> None:
    """Read by the GUI to offer the user a day to look at; an unreadable folder
    is "no days", not an error dialog."""
    log = DiagLog(str(tmp_path))
    (tmp_path / "w").write_text("i am a file", encoding="utf-8")
    assert log.list_day_files("w") == []
