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

from conftest import seed_settings  # noqa: E402

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


class _CaptureWallet:
    """The wallet these tests capture through. Hashable, with nothing stored:
    its settings come from the seed (see conftest.seed_settings)."""
    db = None


_CAPTURE_WALLET = _CaptureWallet()


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
    # Settings are per wallet now, so the same kwargs are given to every wallet
    # this plugin sees -- see conftest.seed_settings.
    seed_settings(p, **config)
    # Log capture is one handler set per process, derived from the OPEN wallets
    # (see Plugin.apply_log_capture_settings), so this plugin needs one for the
    # configured capture settings to count at all.
    p.settings(_CAPTURE_WALLET)
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

    p.set_setting(_CAPTURE_WALLET, "INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG", False)
    p.apply_log_capture_settings()
    assert tree.level == logging.WARNING         # the user's verbosity is handed back


def test_buffer_size_setting_is_applied_and_clamped(tree) -> None:
    p = _plugin(tree)
    p.set_setting(_CAPTURE_WALLET, "INBOUND_LIQUIDITY_LOG_BUFFER_LINES", 250)
    p.apply_log_capture_settings()
    assert p.log_buffer.max_lines == 250

    p.set_setting(_CAPTURE_WALLET, "INBOUND_LIQUIDITY_LOG_BUFFER_LINES", 1)          # below the floor
    p.apply_log_capture_settings()
    assert p.log_buffer.max_lines == MIN_LOG_BUFFER_LINES

    p.set_setting(_CAPTURE_WALLET, "INBOUND_LIQUIDITY_LOG_BUFFER_LINES", 10 ** 9)    # above the ceiling
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


# --- per-wallet settings, process-wide capture ----------------------------
class _FakeDB:
    def __init__(self) -> None:
        self._d = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _FakeWallet:
    def __init__(self) -> None:
        self.db = _FakeDB()

    def save_db(self) -> None:
        pass


def test_capture_follows_the_broadest_setting_among_open_wallets(tree) -> None:
    """The three capture settings are per wallet, but there is one handler set
    and one ring buffer per process. Taking the last writer's answer would mean
    opening a second wallet silently switched off a capture the user turned on to
    diagnose the first, so the broadest answer wins."""
    p = _plugin(tree)
    quiet, loud = _FakeWallet(), _FakeWallet()
    p.settings(quiet), p.settings(loud)
    p.set_setting(loud, "INBOUND_LIQUIDITY_LOG_CAPTURE_LN", True)

    p.apply_log_capture_settings()
    _child("lnrater.LNRater").info("no peer to suggest")
    assert [line.message for line in p.get_log_lines()] == ["no peer to suggest"]
    # And the wallet that did not ask for it still reads its own answer.
    assert p.settings(quiet).INBOUND_LIQUIDITY_LOG_CAPTURE_LN is False


def test_the_transport_is_told_whether_any_wallet_wants_debug_logs(tree) -> None:
    """``get_relay_manager`` lifts aionostr's INFO pin, and the switch behind it
    is per wallet while the logger is not -- so the plugin decides and passes the
    answer in rather than the transport reading a wallet's setting it has no way
    to choose."""
    mod = pytest.importorskip("electrum.plugins.inbound_liquidity.swap_transport")
    p = _plugin(tree)
    wallet = _FakeWallet()
    p.settings(wallet)
    assert p._any_wallet_wants("INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG") is False
    p.set_setting(wallet, "INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG", True)
    assert p._any_wallet_wants("INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG") is True


def _transport(monkeypatch, *, debug_logs: bool):
    """A TargetedNostrTransport with its base class's get_relay_manager stubbed.

    Built without __init__ and with the stock method replaced, because a real
    one needs a config, a swap manager and a keypair -- none of which this
    branch touches. What is under test is what the override does AFTER the
    stock method has pinned the level."""
    mod = pytest.importorskip("electrum.plugins.inbound_liquidity.swap_transport")
    sentinel = object()
    monkeypatch.setattr(mod.NostrTransport, "get_relay_manager",
                        lambda self: sentinel, raising=True)
    t = object.__new__(mod.TargetedNostrTransport)
    t.logger = logging.getLogger(f"{ROOT}.transport")
    t.debug_logs = debug_logs
    return t, sentinel


def test_the_transport_lifts_the_aionostr_pin_only_when_told_to(monkeypatch) -> None:
    aionostr = logging.getLogger(f"{ROOT}.transport.aionostr")

    t, sentinel = _transport(monkeypatch, debug_logs=False)
    aionostr.setLevel(logging.INFO)             # as the stock method pins it
    assert t.get_relay_manager() is sentinel, "the stock manager must be returned"
    assert aionostr.level == logging.INFO

    t, sentinel = _transport(monkeypatch, debug_logs=True)
    aionostr.setLevel(logging.INFO)
    assert t.get_relay_manager() is sentinel
    assert aionostr.level == logging.DEBUG, "the pin was not lifted"


def test_a_failure_lifting_the_pin_never_breaks_a_swap(monkeypatch) -> None:
    """Diagnostics must not be able to abort provider discovery."""
    t, sentinel = _transport(monkeypatch, debug_logs=True)

    class _Boom:
        def getChild(self, name):
            raise RuntimeError("no such logger")

    t.logger = _Boom()
    assert t.get_relay_manager() is sentinel
