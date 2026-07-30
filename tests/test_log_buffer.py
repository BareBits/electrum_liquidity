"""Unit tests for the pure in-memory log capture (`log_buffer`).

No Electrum imports: the module is deliberately standalone, so `conftest.py`
puts the package dir on sys.path and it imports directly (same arrangement as
`test_diag_log.py`).

The properties that matter here are the ones a diagnostic sink has to guarantee
under load: it stays bounded, it stays thread-safe, it never raises into the
code it is observing, and it always restores whatever global logging state it
touched.
"""
from __future__ import annotations

import logging
import threading
from typing import List

import pytest

from log_buffer import (  # type: ignore  (package dir on sys.path via conftest)  # noqa: E402
    DEFAULT_MAX_LINES,
    LN_LOGGER_MARKERS,
    LogCapture,
    LogLine,
    LOG_LINE_MAX_LEN,
    LOG_MAX_LINES_PER_RECORD,
    LogRingBuffer,
    MAX_MAX_LINES,
    MIN_MAX_LINES,
    PluginLogHandler,
    clamp_max_lines,
    scrub_log_text,
    short_source,
)

ROOT = "test_log_buffer_tree"


def _line(ts: float = 1.0, level: int = logging.INFO, source: str = "src",
          message: str = "hello") -> LogLine:
    return LogLine(ts=ts, level=level, level_name=logging.getLevelName(level),
                   source=source, message=message)


# --- clamp_max_lines ------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (5, MIN_MAX_LINES),                 # below the floor
    (10 ** 9, MAX_MAX_LINES),           # above the ceiling
    (500, 500),                         # in range
    ("750", 750),                       # numeric string from a config field
    (None, DEFAULT_MAX_LINES),          # unset
    ("banana", DEFAULT_MAX_LINES),      # unparseable -> default, never raises
])
def test_clamp_max_lines(value, expected) -> None:
    assert clamp_max_lines(value) == expected


# --- short_source ---------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("electrum.plugins.inbound_liquidity.Plugin", "inbound_liquidity.Plugin"),
    # External-zip load: a different package path, same trimming intent.
    ("electrum.electrum_external_plugins.inbound_liquidity.qt.Plugin",
     "inbound_liquidity.qt.Plugin"),
    ("electrum.lnworker.LNWallet.[w]", "lnworker.LNWallet.[w]"),
    ("", ""),
])
def test_short_source_trims_the_tree_prefix(name, expected) -> None:
    assert short_source(name) == expected


# --- LogRingBuffer --------------------------------------------------------
def test_buffer_is_bounded_and_keeps_the_newest() -> None:
    buf = LogRingBuffer(MIN_MAX_LINES)
    for i in range(MIN_MAX_LINES + 25):
        buf.append(_line(ts=float(i), message=f"m{i}"))
    got = buf.snapshot()
    assert len(got) == MIN_MAX_LINES
    assert got[0].message == "m25" and got[-1].message == f"m{MIN_MAX_LINES + 24}"
    # Evictions are counted, so the UI can say history was lost rather than
    # silently presenting a truncated picture as complete.
    assert buf.stats() == {"count": MIN_MAX_LINES, "dropped": 25,
                           "max_lines": MIN_MAX_LINES}


def test_revision_advances_on_every_mutation() -> None:
    buf = LogRingBuffer(MIN_MAX_LINES)
    start = buf.revision
    buf.append(_line())
    assert buf.revision == start + 1
    buf.clear()
    assert buf.revision == start + 2
    # A resize to the same size is a genuine no-op (no spurious repaint).
    unchanged = buf.revision
    buf.resize(MIN_MAX_LINES)
    assert buf.revision == unchanged


def test_resize_preserves_the_tail_and_counts_the_loss() -> None:
    buf = LogRingBuffer(1000)
    for i in range(300):
        buf.append(_line(ts=float(i), message=f"m{i}"))
    buf.resize(MIN_MAX_LINES)                       # shrink below the current fill
    got = buf.snapshot()
    assert [line.message for line in got] == [f"m{i}" for i in range(200, 300)]
    assert buf.stats()["dropped"] == 200
    # Growing again keeps what is there and raises the cap.
    buf.resize(1000)
    assert len(buf.snapshot()) == MIN_MAX_LINES and buf.max_lines == 1000


def test_resize_clamps_out_of_range_values() -> None:
    buf = LogRingBuffer(500)
    buf.resize(1)
    assert buf.max_lines == MIN_MAX_LINES
    buf.resize(10 ** 9)
    assert buf.max_lines == MAX_MAX_LINES


def test_snapshot_filters_by_level_and_text() -> None:
    buf = LogRingBuffer()
    buf.append(_line(ts=1, level=logging.DEBUG, message="opening channel"))
    buf.append(_line(ts=2, level=logging.WARNING, message="no partner"))
    buf.append(_line(ts=3, level=logging.ERROR, source="lnrater", message="boom"))

    assert [line.ts for line in buf.snapshot(min_level=logging.WARNING)] == [2, 3]
    # Case-insensitive, and matches the source as well as the message.
    assert [line.ts for line in buf.snapshot(text="PARTNER")] == [2]
    assert [line.ts for line in buf.snapshot(text="lnrater")] == [3]
    assert [line.ts for line in buf.snapshot(min_level=logging.WARNING, text="boom")] == [3]
    # limit keeps the NEWEST survivors, not the oldest.
    assert [line.ts for line in buf.snapshot(limit=1)] == [3]


def test_snapshot_is_a_copy_the_reader_can_hold() -> None:
    buf = LogRingBuffer()
    buf.append(_line(ts=1))
    got = buf.snapshot()
    buf.append(_line(ts=2))          # writer keeps going
    assert len(got) == 1             # the reader's list is unaffected


def test_concurrent_appends_do_not_lose_or_corrupt_lines() -> None:
    # The handler is called from the asyncio thread, Electrum's network threads
    # and the GUI thread; every mutation has to be serialised.
    buf = LogRingBuffer(MAX_MAX_LINES)
    threads = [
        threading.Thread(target=lambda n=n: [buf.append(_line(ts=float(n), message=f"t{n}-{i}"))
                                             for i in range(200)])
        for n in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(buf.snapshot()) == 8 * 200


# --- PluginLogHandler -----------------------------------------------------
def _emit(handler: PluginLogHandler, name: str, level: int = logging.INFO,
          msg: str = "hi", args=(), exc_info=None) -> None:
    handler.emit(logging.LogRecord(name=name, level=level, pathname=__file__,
                                   lineno=1, msg=msg, args=args, exc_info=exc_info))


def test_handler_keeps_only_matching_loggers() -> None:
    buf = LogRingBuffer()
    handler = PluginLogHandler(buf)
    _emit(handler, "electrum.plugins.inbound_liquidity.Plugin", msg="mine")
    _emit(handler, "electrum.lnworker.LNWallet", msg="not mine")
    assert [line.message for line in buf.snapshot()] == ["mine"]

    # Widening the markers (the "also capture Lightning logs" checkbox) admits
    # the subsystem loggers without touching what is already buffered.
    handler.markers = handler.markers + tuple(LN_LOGGER_MARKERS)
    _emit(handler, "electrum.lnworker.LNWallet", msg="now mine")
    assert [line.message for line in buf.snapshot()] == ["mine", "now mine"]


def test_handler_scrubs_control_characters() -> None:
    buf = LogRingBuffer()
    handler = PluginLogHandler(buf)
    # A hostile provider error string must not be able to inject escape
    # sequences into the view or an exported file.
    _emit(handler, "inbound_liquidity", msg="bad\x1b[31mred\x00nul")
    assert buf.snapshot()[0].message == "bad\\x1b[31mred\\x00nul"


def test_scrub_log_text_keeps_line_structure_but_neutralises_the_rest() -> None:
    # Newlines survive (a traceback stays readable); every other control
    # character is still escaped, including a lone carriage return.
    assert scrub_log_text("a\nb") == "a\nb"
    assert scrub_log_text("a\r\nb") == "a\nb"
    assert scrub_log_text("a\tb\x1bc") == "a\\tb\\x1bc"


def test_scrub_log_text_bounds_a_runaway_record() -> None:
    long_line = "x" * (LOG_LINE_MAX_LEN + 500)
    assert len(scrub_log_text(long_line).splitlines()[0]) < LOG_LINE_MAX_LEN + 50
    many = "\n".join(str(i) for i in range(LOG_MAX_LINES_PER_RECORD + 40))
    out = scrub_log_text(many).splitlines()
    assert len(out) == LOG_MAX_LINES_PER_RECORD + 1     # + the "omitted" marker
    assert "40 more line(s) omitted" in out[-1]


def test_handler_records_the_traceback() -> None:
    buf = LogRingBuffer()
    handler = PluginLogHandler(buf)
    try:
        raise ValueError("kaboom")
    except ValueError:
        import sys
        _emit(handler, "inbound_liquidity", level=logging.ERROR, msg="failed",
              exc_info=sys.exc_info())
    message = buf.snapshot()[0].message
    assert message.startswith("failed") and "ValueError: kaboom" in message


def test_handler_never_raises_on_a_bad_record() -> None:
    buf = LogRingBuffer()
    handler = PluginLogHandler(buf)
    # Mismatched %-args make getMessage() raise; a logging fault must never
    # propagate into the plugin it is observing.
    _emit(handler, "inbound_liquidity", msg="%d %d", args=(1,))
    assert buf.snapshot() == []


def test_handler_does_not_recurse_when_the_buffer_logs() -> None:
    # If anything on the emit path ever logged, the handler would re-enter and
    # blow the stack. Simulate that with a buffer that logs on append.
    logger = logging.getLogger("inbound_liquidity.recursion_probe")
    calls = {"n": 0}

    class _LoggingBuffer(LogRingBuffer):
        def append(self, line: LogLine) -> None:  # type: ignore[override]
            calls["n"] += 1
            logger.error("append happened")       # would recurse without the guard
            LogRingBuffer.append(self, line)

    buf = _LoggingBuffer()
    handler = PluginLogHandler(buf)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.info("first")
    finally:
        logger.removeHandler(handler)
    assert calls["n"] == 1


# --- LogCapture -----------------------------------------------------------
@pytest.fixture
def tree():
    """A private logger tree, so these tests never touch Electrum's real one."""
    logger = logging.getLogger(ROOT)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    yield logger
    logger.setLevel(previous_level)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)


def _child(name: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{name}")


def test_configure_attaches_once_and_is_idempotent(tree) -> None:
    buf = LogRingBuffer()
    capture = LogCapture(buf, root_logger_name=ROOT)
    capture.configure()
    capture.configure()
    capture.configure(capture_ln=True)
    # Repeat calls must not stack handlers -- a reload would otherwise duplicate
    # every captured line once per call.
    assert sum(isinstance(h, PluginLogHandler) for h in tree.handlers) == 1
    assert capture.attached


def test_capture_ln_toggles_what_is_recorded(tree) -> None:
    buf = LogRingBuffer()
    capture = LogCapture(buf, root_logger_name=ROOT)
    capture.configure(capture_ln=False)
    _child("inbound_liquidity.Plugin").info("ours")
    _child("lnpeermgr.LNPeerMgr").info("theirs")
    assert [line.message for line in buf.snapshot()] == ["ours"]

    capture.configure(capture_ln=True)
    _child("lnpeermgr.LNPeerMgr").info("theirs now")
    assert [line.message for line in buf.snapshot()] == ["ours", "theirs now"]

    capture.configure(capture_ln=False)
    _child("lnpeermgr.LNPeerMgr").info("theirs again")
    assert [line.message for line in buf.snapshot()] == ["ours", "theirs now"]


def test_force_debug_raises_and_restores_the_level(tree) -> None:
    tree.setLevel(logging.WARNING)          # as if launched with a reduced -v
    buf = LogRingBuffer()
    capture = LogCapture(buf, root_logger_name=ROOT)

    capture.configure(force_debug=False)
    _child("inbound_liquidity").debug("invisible")
    assert buf.snapshot() == []             # the record was never even created

    capture.configure(force_debug=True)
    assert tree.level == logging.DEBUG
    _child("inbound_liquidity").debug("visible")
    assert [line.message for line in buf.snapshot()] == ["visible"]

    capture.configure(force_debug=False)
    assert tree.level == logging.WARNING    # the user's verbosity is handed back


def test_detach_removes_the_handler_and_restores_the_level(tree) -> None:
    tree.setLevel(logging.WARNING)
    buf = LogRingBuffer()
    capture = LogCapture(buf, root_logger_name=ROOT)
    capture.configure(capture_ln=True, force_debug=True)

    capture.detach()
    assert not capture.attached
    assert not any(isinstance(h, PluginLogHandler) for h in tree.handlers)
    assert tree.level == logging.WARNING
    _child("inbound_liquidity").warning("after detach")
    assert buf.snapshot() == []             # nothing is captured any more

    capture.detach()                        # safe to call twice
    assert not capture.attached
