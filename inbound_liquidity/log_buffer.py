"""In-memory log capture for the Inbound Liquidity plugin.

A *sink* — deliberately free of any Electrum imports so it is fully unit-
testable in isolation (mirroring the pure/glue split of `liquidity_manager.py`
and `diag_log.py`). It backs the GUI's "Log" tab, which merges these captured
Python-logging records with the per-wallet decision log so one view answers both
"what did the plugin decide" and "what did it actually see while deciding".

Three pieces:

``LogRingBuffer``
    A bounded, thread-safe ring of :class:`LogLine`. Records arrive from the
    asyncio thread, Electrum's network threads and the GUI thread, so every
    mutation takes a lock, and readers get a *copy* they can hold while the
    writer keeps going. A monotonic ``revision`` counter lets the GUI poll for
    "is there anything new" without formatting or copying anything.

``PluginLogHandler``
    A ``logging.Handler`` that formats a record, runs it through
    :func:`scrub_text` (so a control-character injection in, say, a provider's
    error string cannot corrupt the view or an exported file) and appends it.
    It swallows *everything*: a logging fault must never propagate into the
    plugin, and must never recurse back into logging.

``LogCapture``
    Attach/detach lifecycle. One handler is installed on Electrum's ``electrum``
    logger tree with a name-based filter, rather than on each individual logger:
    the plugin's own logger names differ between an internal/symlinked load
    (``electrum.plugins.inbound_liquidity.…``) and an external-zip load
    (``electrum.electrum_external_plugins.inbound_liquidity.…``), and Electrum's
    ``Logger`` mixin appends a per-instance diagnostic suffix, so matching on a
    substring is the only robust rule. Attaching is idempotent, and detaching
    restores any log level we overrode.

Nothing here is written to disk: the buffer lives and dies with the session
(the tab's "Save to file…" button is the deliberate, user-driven export path).
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Sequence, Tuple

try:  # package import (normal plugin load)
    from .liquidity_manager import scrub_text
except ImportError:  # standalone import (unit tests put the package dir on sys.path)
    from liquidity_manager import scrub_text  # type: ignore

# Ring size. The default matches MAX_LOG_ENTRIES (the decision log's own cap) so
# the two halves of the merged view hold comparable history. The bounds are wide
# but finite: the point of the ring is that a DEBUG firehose can never grow the
# process without limit, so "unbounded" is not on the menu.
DEFAULT_MAX_LINES = 2000
MIN_MAX_LINES = 100
MAX_MAX_LINES = 100_000

# Level choices offered by the tab's filter, coarsest first.
LEVEL_CHOICES: Tuple[Tuple[str, int], ...] = (
    ("Error", logging.ERROR),
    ("Warning", logging.WARNING),
    ("Info", logging.INFO),
    ("Debug", logging.DEBUG),
)

# Substring of every logger name belonging to this plugin, under either load
# mode (see the module docstring).
PLUGIN_LOGGER_MARKERS: Tuple[str, ...] = ("inbound_liquidity",)

# Electrum's Lightning/swap subsystems -- where "why is there no channel partner"
# is actually decided (lnrater ranks the graph, lnpeermgr does the connecting,
# lnworker.suggest_peer picks). Opt-in, because they are noisy.
LN_LOGGER_MARKERS: Tuple[str, ...] = (
    "lnworker",
    "lnpeer",          # also matches lnpeermgr
    "lnrater",
    "lnchannel",
    "lnrouter",
    "submarine_swaps",
)

# Logger tree Electrum puts everything under (electrum.logging.electrum_logger).
ELECTRUM_LOGGER_NAME = "electrum"

# Formatter used only to render an exc_info traceback into the message body.
_EXC_FORMATTER = logging.Formatter()

# Per-line and per-record limits for a captured message. `scrub_text`'s own
# default (200 chars) is tuned for the compact decision-log columns and would
# truncate the very diagnostics this view exists to show, so the line limit here
# is far more generous -- but still finite, so one pathological record cannot
# dominate the ring. The line cap bounds a runaway traceback.
LOG_LINE_MAX_LEN = 2000
LOG_MAX_LINES_PER_RECORD = 60


def scrub_log_text(text: str) -> str:
    """Scrub a possibly multi-line log message, preserving its line structure.

    :func:`scrub_text` escapes newlines along with every other control
    character, which is right for the one-line-per-entry file logger but turns a
    traceback into an unreadable single line here. So each physical line is
    scrubbed *independently* and the lines are rejoined with real newlines: every
    control character is still neutralised, and the multi-line shape survives.

    This is safe against log injection because of how the view renders a record:
    only the FIRST line of an event starts at column 0 (continuation lines are
    indented under the message column), so text smuggled into a message can never
    be mistaken for a separate log row.
    """
    lines = str(text).splitlines() or [""]
    truncated = lines[:LOG_MAX_LINES_PER_RECORD]
    out = [scrub_text(line, max_len=LOG_LINE_MAX_LEN) for line in truncated]
    dropped = len(lines) - len(truncated)
    if dropped > 0:
        out.append(f"…({dropped} more line(s) omitted)")
    return "\n".join(out)


def clamp_max_lines(value: object) -> int:
    """Coerce an operator-supplied buffer size into the supported range.

    Anything unparseable falls back to the default rather than raising: this is
    read from config on a hot path and a bad value must not break capture.
    """
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_MAX_LINES
    return max(MIN_MAX_LINES, min(n, MAX_MAX_LINES))


def short_source(name: str) -> str:
    """Trim a logger name down to something that fits the tab's Source column.

    Drops the ``electrum.`` tree prefix and the plugin package path, keeping the
    tail (class + diagnostic name) that actually distinguishes one line's origin
    from another's.
    """
    if not name:
        return ""
    for prefix in ("electrum.electrum_external_plugins.", "electrum.plugins.", "electrum."):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name


@dataclass(frozen=True)
class LogLine:
    """One captured logging record, already scrubbed and ready to display."""
    ts: float           # epoch seconds (record.created), for merging with the decision log
    level: int          # numeric level, for the tab's level filter
    level_name: str     # "INFO", "WARNING", …
    source: str         # shortened logger name
    message: str        # scrubbed message, traceback appended if there was one


class LogRingBuffer:
    """Bounded, thread-safe ring of :class:`LogLine`."""

    def __init__(self, max_lines: int = DEFAULT_MAX_LINES) -> None:
        self._lock = threading.Lock()
        self._max_lines = clamp_max_lines(max_lines)
        self._records: Deque[LogLine] = deque(maxlen=self._max_lines)
        self._revision = 0
        # Lines evicted by the ring (or by a shrink), so the tab can honestly say
        # history was lost instead of silently showing a truncated picture.
        self._dropped = 0

    # --- properties -------------------------------------------------------
    @property
    def max_lines(self) -> int:
        with self._lock:
            return self._max_lines

    @property
    def revision(self) -> int:
        """Bumped on every mutation. The GUI polls this to decide whether a
        repaint is warranted, so a quiet plugin costs one integer read."""
        with self._lock:
            return self._revision

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    # --- writing ----------------------------------------------------------
    def append(self, line: LogLine) -> None:
        with self._lock:
            if len(self._records) == self._max_lines:
                self._dropped += 1  # deque is about to evict the oldest
            self._records.append(line)
            self._revision += 1

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._dropped = 0
            self._revision += 1

    def resize(self, max_lines: int) -> None:
        """Change the cap, keeping the most recent lines that still fit."""
        new_max = clamp_max_lines(max_lines)
        with self._lock:
            if new_max == self._max_lines:
                return
            kept = list(self._records)[-new_max:]
            self._dropped += max(0, len(self._records) - len(kept))
            self._max_lines = new_max
            self._records = deque(kept, maxlen=new_max)
            self._revision += 1

    # --- reading ----------------------------------------------------------
    def snapshot(self, *, min_level: int = 0, text: Optional[str] = None,
                 limit: Optional[int] = None) -> List[LogLine]:
        """Oldest-first copy of the buffer, optionally filtered.

        ``min_level`` keeps records at or above a numeric level; ``text`` is a
        case-insensitive substring matched against the message and the source.
        ``limit`` keeps only the newest N of whatever survives the filters.
        """
        with self._lock:
            records = list(self._records)
        if min_level:
            records = [r for r in records if r.level >= min_level]
        if text:
            needle = text.casefold()
            records = [r for r in records
                       if needle in r.message.casefold() or needle in r.source.casefold()]
        if limit is not None and limit >= 0:
            records = records[-limit:]
        return records

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "count": len(self._records),
                "dropped": self._dropped,
                "max_lines": self._max_lines,
            }


class PluginLogHandler(logging.Handler):
    """Feeds a :class:`LogRingBuffer` from the Python logging framework."""

    def __init__(self, buffer: LogRingBuffer, *,
                 markers: Sequence[str] = PLUGIN_LOGGER_MARKERS) -> None:
        logging.Handler.__init__(self, level=logging.DEBUG)
        self.buffer = buffer
        # Mutated live when the user ticks "also capture Electrum Lightning
        # logs"; a plain tuple assignment is atomic enough for a filter that is
        # allowed to miss the exact record the checkbox was flipped on.
        self.markers: Tuple[str, ...] = tuple(markers)
        # Re-entrancy guard: if anything on the emit path ever logged (directly,
        # or via a library we call), we would recurse until the stack blew.
        self._local = threading.local()

    def wants(self, logger_name: str) -> bool:
        name = (logger_name or "").casefold()
        return any(m in name for m in self.markers)

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            if not self.wants(record.name):
                return
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + _EXC_FORMATTER.formatException(record.exc_info)
            self.buffer.append(LogLine(
                ts=float(record.created),
                level=int(record.levelno),
                level_name=str(record.levelname),
                source=short_source(record.name),
                message=scrub_log_text(message),
            ))
        except Exception:
            # Diagnostics are strictly best-effort. Never raise, and never call
            # handleError (which would print to stderr on every bad record).
            pass
        finally:
            self._local.busy = False

    def handleError(self, record: logging.LogRecord) -> None:  # pragma: no cover
        pass


class LogCapture:
    """Attach/detach lifecycle for one :class:`PluginLogHandler`."""

    def __init__(self, buffer: LogRingBuffer, *,
                 root_logger_name: str = ELECTRUM_LOGGER_NAME) -> None:
        self.buffer = buffer
        self.root_logger_name = root_logger_name
        self._handler: Optional[PluginLogHandler] = None
        # Level we found on the root logger before force-DEBUG overrode it, so
        # unticking (or unloading the plugin) puts the user's verbosity back.
        self._saved_level: Optional[int] = None
        self._capture_ln = False
        self._force_debug = False

    # --- state ------------------------------------------------------------
    @property
    def attached(self) -> bool:
        return self._handler is not None

    @property
    def capture_ln(self) -> bool:
        return self._capture_ln

    @property
    def force_debug(self) -> bool:
        return self._force_debug

    def _markers(self, capture_ln: bool) -> Tuple[str, ...]:
        markers = tuple(PLUGIN_LOGGER_MARKERS)
        if capture_ln:
            markers += tuple(LN_LOGGER_MARKERS)
        return markers

    # --- lifecycle --------------------------------------------------------
    def configure(self, *, capture_ln: bool = False, force_debug: bool = False) -> None:
        """Install the handler (once) and bring it in line with the settings.

        Idempotent: calling it repeatedly with the same arguments is a no-op, so
        it is safe to call on every settings change and on every wallet load.
        """
        capture_ln = bool(capture_ln)
        force_debug = bool(force_debug)
        logger = logging.getLogger(self.root_logger_name)
        if self._handler is None:
            self._handler = PluginLogHandler(self.buffer, markers=self._markers(capture_ln))
            logger.addHandler(self._handler)
        else:
            self._handler.markers = self._markers(capture_ln)
        self._capture_ln = capture_ln
        self._apply_force_debug(logger, force_debug)

    def _apply_force_debug(self, logger: logging.Logger, force_debug: bool) -> None:
        """Raise (or restore) the captured tree's level.

        Electrum leaves its ``electrum`` logger at DEBUG by default and filters
        at the *handler*, so DEBUG records normally reach us untouched and this
        is a no-op. It matters only when the user launched with an explicit
        ``-v`` that raised the level, which would stop the interesting records
        from ever being created.
        """
        if force_debug and not self._force_debug:
            self._saved_level = logger.level
            logger.setLevel(logging.DEBUG)
        elif not force_debug and self._force_debug:
            if self._saved_level is not None:
                logger.setLevel(self._saved_level)
            self._saved_level = None
        self._force_debug = force_debug

    def detach(self) -> None:
        """Remove the handler and undo any level override. Safe to call twice."""
        logger = logging.getLogger(self.root_logger_name)
        if self._handler is not None:
            try:
                logger.removeHandler(self._handler)
                self._handler.close()
            except Exception:
                pass
            self._handler = None
        if self._force_debug and self._saved_level is not None:
            try:
                logger.setLevel(self._saved_level)
            except Exception:
                pass
        self._saved_level = None
        self._force_debug = False
        self._capture_ln = False
