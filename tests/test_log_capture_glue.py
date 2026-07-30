"""Glue tests: the plugin's live log capture (the Log tab's backing store).

Covers the wiring the pure `log_buffer` tests cannot see: that the three config
settings reach the capture, that re-applying them is idempotent (a settings save
must not stack handlers or duplicate every captured line), that plugin teardown
detaches cleanly, and that a broken capture can never stop the plugin loading.

Imports the real plugin package (needs the Electrum venv); skipped otherwise.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    DEFAULT_LOG_BUFFER_LINES,
    LiquidityPlugin,
    MAX_LOG_BUFFER_LINES,
    MIN_LOG_BUFFER_LINES,
)
from electrum.plugins.inbound_liquidity.log_buffer import (  # type: ignore  # noqa: E402
    LogCapture,
    LogRingBuffer,
    PluginLogHandler,
)

ROOT = "test_capture_glue_tree"


@pytest.fixture
def tree():
    """A private logger tree, so these tests never touch Electrum's real one."""
    logger = logging.getLogger(ROOT)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    yield logger
    logger.setLevel(previous)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)


def _plugin(tree, **config) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.capture")
    cfg = dict(
        INBOUND_LIQUIDITY_LOG_BUFFER_LINES=DEFAULT_LOG_BUFFER_LINES,
        INBOUND_LIQUIDITY_LOG_CAPTURE_LN=False,
        INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG=False,
    )
    cfg.update(config)
    p.config = SimpleNamespace(**cfg)
    p.log_buffer = LogRingBuffer(cfg["INBOUND_LIQUIDITY_LOG_BUFFER_LINES"])
    p.log_capture = LogCapture(p.log_buffer, root_logger_name=ROOT)
    return p


def _child(name: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{name}")


# --- settings -> capture --------------------------------------------------
def test_apply_settings_installs_capture_for_our_own_logging(tree) -> None:
    p = _plugin(tree)
    p.apply_log_capture_settings()
    _child("inbound_liquidity.Plugin").info("ours")
    _child("lnrater.LNRater").info("theirs")
    assert [line.message for line in p.get_log_lines()] == ["ours"]


def test_capture_ln_setting_widens_the_capture(tree) -> None:
    p = _plugin(tree, INBOUND_LIQUIDITY_LOG_CAPTURE_LN=True)
    p.apply_log_capture_settings()
    _child("lnrater.LNRater").info("no peer to suggest")
    assert [line.message for line in p.get_log_lines()] == ["no peer to suggest"]


def test_debug_setting_raises_and_restores_the_level(tree) -> None:
    tree.setLevel(logging.WARNING)               # as if launched with a reduced -v
    p = _plugin(tree, INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG=True)
    p.apply_log_capture_settings()
    _child("inbound_liquidity").debug("deep detail")
    assert [line.message for line in p.get_log_lines()] == ["deep detail"]

    p.config.INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG = False
    p.apply_log_capture_settings()
    assert tree.level == logging.WARNING         # the user's verbosity is handed back


def test_buffer_size_setting_is_applied_and_clamped(tree) -> None:
    p = _plugin(tree)
    p.config.INBOUND_LIQUIDITY_LOG_BUFFER_LINES = 250
    p.apply_log_capture_settings()
    assert p.log_buffer.max_lines == 250

    p.config.INBOUND_LIQUIDITY_LOG_BUFFER_LINES = 1          # below the floor
    p.apply_log_capture_settings()
    assert p.log_buffer.max_lines == MIN_LOG_BUFFER_LINES

    p.config.INBOUND_LIQUIDITY_LOG_BUFFER_LINES = 10 ** 9    # above the ceiling
    p.apply_log_capture_settings()
    assert p.log_buffer.max_lines == MAX_LOG_BUFFER_LINES


def test_reapplying_settings_does_not_stack_handlers(tree) -> None:
    # The Advanced tab calls this on every save; stacking would duplicate every
    # captured line once per save.
    p = _plugin(tree)
    for _ in range(5):
        p.apply_log_capture_settings()
    assert sum(isinstance(h, PluginLogHandler) for h in tree.handlers) == 1
    _child("inbound_liquidity").info("once")
    assert [line.message for line in p.get_log_lines()] == ["once"]


def test_bad_buffer_setting_falls_back_instead_of_breaking_load(tree) -> None:
    p = _plugin(tree, INBOUND_LIQUIDITY_LOG_BUFFER_LINES="not a number")
    p.log_buffer = LogRingBuffer()
    p.log_capture = LogCapture(p.log_buffer, root_logger_name=ROOT)
    p.apply_log_capture_settings()               # must not raise
    assert p.log_buffer.max_lines == DEFAULT_LOG_BUFFER_LINES
    assert p.log_capture.attached


def test_capture_failure_never_breaks_plugin_load(tree) -> None:
    p = _plugin(tree)

    class _Broken:
        attached = False

        def configure(self, **kwargs):
            raise RuntimeError("logging subsystem unavailable")

    p.log_capture = _Broken()
    p.apply_log_capture_settings()               # swallowed, plugin stays usable


# --- filtering ------------------------------------------------------------
def test_get_log_lines_filters_by_level_and_text(tree) -> None:
    p = _plugin(tree)
    p.apply_log_capture_settings()
    log = _child("inbound_liquidity.Plugin")
    log.info("resolving channel partners")
    log.warning("no reachable channel partner")

    assert len(p.get_log_lines()) == 2
    assert [line.message for line in p.get_log_lines(min_level=logging.WARNING)] == [
        "no reachable channel partner"]
    assert [line.message for line in p.get_log_lines(text="resolving")] == [
        "resolving channel partners"]


# --- teardown -------------------------------------------------------------
def test_on_close_detaches_the_capture(tree) -> None:
    p = _plugin(tree)
    p.apply_log_capture_settings()
    p._heartbeat_tasks = {}
    p._on_wallet_event = lambda *a: None

    p.on_close()

    assert not p.log_capture.attached
    assert not any(isinstance(h, PluginLogHandler) for h in tree.handlers)
    _child("inbound_liquidity").warning("after unload")
    assert p.get_log_lines() == []               # a disabled plugin stops capturing
