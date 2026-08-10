"""The Log tab's two capture switches must actually widen what is recorded.

Two defects, both reported as "checking the box does nothing":

  * "Capture debug-level logs" only raised the level of the *tree root*. Electrum
    already pins its ``electrum`` logger to DEBUG and filters at the handlers, so
    that was a no-op in every normal launch -- while the records that really were
    suppressed sit behind levels pinned on *child* loggers (a reduced ``-v``
    verbosity, and ``submarine_swaps`` pinning its ``aionostr`` child to INFO,
    which hides whether a swap provider ever answered our DM).
  * "Also capture Electrum Lightning logs" omitted ``lnwatcher``, ``txbatcher``
    and ``lnsweep`` -- exactly the subsystems that decide whether a reverse swap's
    on-chain funding is seen and claimed, i.e. the ones you need when the plugin
    reports "no on-chain funding".
"""
from __future__ import annotations

import logging

import pytest

from log_buffer import (  # type: ignore
    LN_LOGGER_MARKERS,
    LogCapture,
    LogRingBuffer,
    PluginLogHandler,
)

ROOT = "test_capture_debug_tree"


@pytest.fixture
def tree():
    """A private logger tree, so these tests never touch Electrum's real one."""
    logger = logging.getLogger(ROOT)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    yield logger
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(previous)


def _capture(tree) -> LogCapture:
    return LogCapture(LogRingBuffer(200), root_logger_name=tree.name)


# --- marker coverage ------------------------------------------------------
@pytest.mark.parametrize("subsystem", ["lnwatcher", "txbatcher", "lnsweep"])
def test_swap_funding_subsystems_are_captured(tree, subsystem) -> None:
    """These decide whether a swap's funding is spotted and claimed."""
    cap = _capture(tree)
    cap.configure(capture_ln=True)
    name = f"{tree.name}.{subsystem}.SomeClass.[wallet]"
    logging.getLogger(name).debug("hello")
    assert [line.message for line in cap.buffer.snapshot()] == ["hello"]


@pytest.mark.parametrize("subsystem", ["lnwatcher", "txbatcher", "lnsweep"])
def test_those_subsystems_stay_out_when_the_switch_is_off(tree, subsystem) -> None:
    cap = _capture(tree)
    cap.configure(capture_ln=False)
    logging.getLogger(f"{tree.name}.{subsystem}.SomeClass").debug("hello")
    assert cap.buffer.snapshot() == []


def test_marker_list_still_covers_the_original_subsystems() -> None:
    """Adding markers must not have dropped any."""
    for marker in ("lnworker", "lnpeer", "lnrater", "lnchannel", "lnrouter",
                   "submarine_swaps"):
        assert marker in LN_LOGGER_MARKERS


# --- force-debug: child levels -------------------------------------------
def test_force_debug_lifts_a_pinned_child_logger(tree) -> None:
    """The aionostr case: a child pinned to INFO by Electrum itself. A level on a
    child wins over its ancestors, so raising the root alone left it silent."""
    pinned = logging.getLogger(f"{tree.name}.submarine_swaps.NostrTransport.aionostr")
    pinned.setLevel(logging.INFO)
    cap = _capture(tree)

    cap.configure(capture_ln=True, force_debug=False)
    pinned.debug("before")
    assert cap.buffer.snapshot() == []          # suppressed at the child

    cap.configure(capture_ln=True, force_debug=True)
    pinned.debug("after")
    assert [line.message for line in cap.buffer.snapshot()] == ["after"]


def test_unticking_restores_the_childs_own_level(tree) -> None:
    pinned = logging.getLogger(f"{tree.name}.lnworker.LNWallet")
    pinned.setLevel(logging.WARNING)
    cap = _capture(tree)
    cap.configure(capture_ln=True, force_debug=True)
    assert pinned.level == logging.DEBUG

    cap.configure(capture_ln=True, force_debug=False)
    assert pinned.level == logging.WARNING


def test_detach_restores_child_levels_too(tree) -> None:
    """Unloading the plugin must not leave the user's verbosity rewritten."""
    pinned = logging.getLogger(f"{tree.name}.lnpeer.Peer")
    pinned.setLevel(logging.ERROR)
    cap = _capture(tree)
    cap.configure(capture_ln=True, force_debug=True)
    assert pinned.level == logging.DEBUG

    cap.detach()
    assert pinned.level == logging.ERROR


def test_force_debug_leaves_unpinned_children_alone(tree) -> None:
    """Only loggers with an explicit level of their own are touched; everything
    else must keep inheriting (NOTSET), or unticking could not restore it."""
    inheriting = logging.getLogger(f"{tree.name}.lnchannel.Channel")
    assert inheriting.level == logging.NOTSET
    cap = _capture(tree)
    cap.configure(capture_ln=True, force_debug=True)
    assert inheriting.level == logging.NOTSET


def test_force_debug_ignores_children_already_at_debug_or_lower(tree) -> None:
    verbose = logging.getLogger(f"{tree.name}.lnrouter.Router")
    verbose.setLevel(logging.DEBUG)
    cap = _capture(tree)
    cap.configure(capture_ln=True, force_debug=True)
    cap.configure(capture_ln=True, force_debug=False)
    assert verbose.level == logging.DEBUG       # untouched, so nothing to restore


def test_force_debug_toggling_is_idempotent(tree) -> None:
    """Re-applying the same settings must not lose the saved levels."""
    pinned = logging.getLogger(f"{tree.name}.lnworker.Idempotent")
    pinned.setLevel(logging.WARNING)
    cap = _capture(tree)
    cap.configure(capture_ln=True, force_debug=True)
    cap.configure(capture_ln=True, force_debug=True)
    cap.configure(capture_ln=True, force_debug=True)
    assert pinned.level == logging.DEBUG
    cap.configure(capture_ln=True, force_debug=False)
    assert pinned.level == logging.WARNING


# --- the honesty hint the GUI shows --------------------------------------
def test_already_emits_debug_reports_the_trees_effective_level(tree) -> None:
    """What the Log tab uses to say "Electrum already logs at debug level"
    instead of leaving a no-op checkbox looking broken."""
    assert LogCapture.already_emits_debug(tree.name) is True
    tree.setLevel(logging.WARNING)
    assert LogCapture.already_emits_debug(tree.name) is False


def test_already_emits_debug_is_safe_on_an_unknown_tree() -> None:
    assert isinstance(LogCapture.already_emits_debug("no.such.tree.exists"), bool)
