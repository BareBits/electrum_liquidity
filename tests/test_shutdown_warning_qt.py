"""GUI-glue tests for what the user sees when they quit mid-swap.

Two surfaces, and they answer different questions.

The CLOSING WARNING answers "is it safe to quit now?". Electrum already warns
about swaps whose funding tx is on-chain but unconfirmed
(``_check_ongoing_submarine_swaps_callback``, which reads
``get_pending_swaps()``). That check is blind to the window this plugin actually
creates -- a reverse swap whose Lightning payment is committed but whose funding
output does not exist yet -- because a swap is absent from ``get_pending_swaps()``
for the whole of it. So the riskiest moment of the operation was the one moment
the user was told nothing.

The SHUTDOWN WAIT answers "is it hung?". ``stop_wallet`` runs on the GUI thread
(closeEvent -> clean_up -> run_hook('close_wallet')) while the attempt runs on the
asyncio loop, so the base class's plain ``Event.wait`` would freeze the window for
up to a couple of minutes. The reflex that produces -- force-killing Electrum --
is the exact outcome the whole handshake exists to prevent.

Needs PyQt6 and Electrum's Qt GUI importable; skipped outside the electrum venv.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from types import SimpleNamespace
from typing import List

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from electrum.plugins.inbound_liquidity import qt as qt_mod  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity import _InflightAttempt  # type: ignore  # noqa: E402

A = "npub1zfmq8lfqhlpfk9y7m8f5scv7j93f73jqs3m7296t3a6jms3vwazqdvwfdg"


@pytest.fixture(scope="module")
def qapp():
    """A real (offscreen) QApplication. Constructing any QWidget without one
    ABORTS the process rather than raising, so the one test that builds the real
    dialog must have it."""
    app = QApplication.instance() or QApplication([])
    yield app


class _Wallet:
    """Hashable, unlike SimpleNamespace -- the per-wallet stores key on it."""

    @staticmethod
    def basename() -> str:
        return "test-wallet"


def _plugin() -> qt_mod.Plugin:
    p = object.__new__(qt_mod.Plugin)
    p.logger = logging.getLogger("test.inbound_liquidity.shutdown_qt")
    p._stopping = set()
    p._inflight_attempt = {}
    p._attempt_done_events = {}
    p.wallets = {}
    p._heartbeat_tasks = {}
    return p


def _only_int_near(text: str, expected: int, slack: int = 1) -> bool:
    """Whether ``text`` carries a countdown within ``slack`` of ``expected``.
    The rendered figure is truncated to whole seconds, so asserting on the exact
    number is a flake waiting to happen."""
    import re
    return any(abs(int(n) - expected) <= slack for n in re.findall(r"\d+", text))


def _attempt(*, remaining: float = 30.0, amount: int = 95_468) -> _InflightAttempt:
    now = time.monotonic()
    return _InflightAttempt(short_id="967056x2797x2", provider_npub=A,
                            amount_sat=amount, started_at=now,
                            deadline=now + remaining)


# --- the closing warning --------------------------------------------------
def test_no_warning_when_nothing_is_in_flight() -> None:
    """The ordinary close. Returning None shows no dialog at all -- a plugin that
    warned on every quit would train the user to click straight through the one
    that matters."""
    p = _plugin()
    assert p._closing_warning(_Wallet()) is None


def test_the_warning_names_the_amount_channel_and_wait() -> None:
    """It has to be specific enough to decide on. "A swap is in progress" alone
    does not tell you whether to wait, so it says what is committed, where from,
    and how long closing will take."""
    p = _plugin()
    wallet = _Wallet()
    p._note_attempt_started(wallet, _attempt(remaining=42.0))
    warning = p._closing_warning(wallet)
    assert warning is not None
    assert "95,468" in warning
    assert "967056x2797x2" in warning
    # The countdown is truncated to whole seconds, so it reads 41 or 42 depending
    # on where in the second the assertion lands.
    assert _only_int_near(warning, 42)
    # And the honest framing: committed, but not yet at rest.
    assert "in flight" in warning


def test_the_warning_clears_once_the_attempt_finishes() -> None:
    p = _plugin()
    wallet = _Wallet()
    p._note_attempt_started(wallet, _attempt())
    assert p._closing_warning(wallet) is not None
    p._note_attempt_finished(wallet)
    assert p._closing_warning(wallet) is None


def test_registration_tolerates_an_electrum_without_the_hook() -> None:
    """register_closing_warning_callback is a recent addition to Electrum's main
    window. A build without it should cost a debug line, not a wallet that fails
    to load."""
    p = _plugin()
    window = SimpleNamespace()          # no register_closing_warning_callback
    p._register_closing_warning(window, _Wallet())   # must not raise


def test_registration_passes_a_callback_bound_to_this_wallet() -> None:
    p = _plugin()
    wallet = _Wallet()
    registered: List[object] = []
    window = SimpleNamespace(
        register_closing_warning_callback=registered.append)
    p._register_closing_warning(window, wallet)
    assert len(registered) == 1
    callback = registered[0]
    assert callback() is None
    p._note_attempt_started(wallet, _attempt())
    assert callback() is not None


def test_a_raising_registration_never_breaks_wallet_load() -> None:
    p = _plugin()

    def _boom(_cb):
        raise RuntimeError("no")

    window = SimpleNamespace(register_closing_warning_callback=_boom)
    p._register_closing_warning(window, _Wallet())   # must not raise


# --- the shutdown wait ----------------------------------------------------
def test_the_wait_returns_immediately_with_nothing_in_flight() -> None:
    p = _plugin()
    started = time.monotonic()
    p._await_shutdown(_Wallet())
    assert time.monotonic() - started < 0.5


def test_the_wait_ends_as_soon_as_the_attempt_finishes() -> None:
    """It polls rather than blocking so Qt keeps painting, but it must still end
    promptly -- the user is staring at a window that will not close."""
    p = _plugin()
    wallet = _Wallet()
    p._note_attempt_started(wallet, _attempt(remaining=30.0))

    def _finish_shortly() -> None:
        time.sleep(0.3)
        p._note_attempt_finished(wallet)

    threading.Thread(target=_finish_shortly, daemon=True).start()
    started = time.monotonic()
    p._await_shutdown(wallet)
    elapsed = time.monotonic() - started
    assert 0.3 <= elapsed < 5.0, f"the wait took {elapsed:.2f}s"


def test_the_wait_gives_up_at_the_attempts_deadline() -> None:
    """Bounded by what the attempt itself has left to run. Expiry is not an
    error: we save and exit, and the attempt's outcome is reconciled next load."""
    p = _plugin()
    wallet = _Wallet()
    p._note_attempt_started(wallet, _attempt(remaining=0.3))
    started = time.monotonic()
    p._await_shutdown(wallet)               # nothing ever sets the event
    elapsed = time.monotonic() - started
    assert 0.3 <= elapsed < 3.0, f"the wait took {elapsed:.2f}s"


def test_a_short_wait_shows_no_dialog(monkeypatch) -> None:
    """Deferred on purpose: most closes have nothing in flight, and the next most
    common have an attempt that is nearly done. Flashing a dialog for either
    would be worse than showing nothing."""
    p = _plugin()
    wallet = _Wallet()
    built: List[object] = []
    monkeypatch.setattr(qt_mod.Plugin, "_build_shutdown_dialog",
                        lambda self, attempt, deadline: (built.append(attempt), (None, None))[1])
    p._note_attempt_started(wallet, _attempt(remaining=30.0))

    def _finish_shortly() -> None:
        time.sleep(0.2)
        p._note_attempt_finished(wallet)

    threading.Thread(target=_finish_shortly, daemon=True).start()
    p._await_shutdown(wallet)
    assert built == [], "a dialog appeared for a wait shorter than the delay"


def test_a_long_wait_shows_the_dialog_once(monkeypatch) -> None:
    p = _plugin()
    wallet = _Wallet()
    built: List[object] = []

    class _Label:
        def __init__(self) -> None:
            self.texts: List[str] = []

        def setText(self, text: str) -> None:
            self.texts.append(text)

    label = _Label()

    def _build(self, attempt, deadline):
        built.append(attempt)
        return SimpleNamespace(close=lambda: None), label

    monkeypatch.setattr(qt_mod, "SHUTDOWN_DIALOG_DELAY_SEC", 0.1)
    monkeypatch.setattr(qt_mod.Plugin, "_build_shutdown_dialog", _build)
    p._note_attempt_started(wallet, _attempt(remaining=0.6))
    p._await_shutdown(wallet)
    assert len(built) == 1, "the dialog should be built once, not per poll"
    assert label.texts, "the countdown never repainted"
    assert "967056x2797x2" in label.texts[-1]


def test_the_dialog_text_counts_down_and_says_what_it_is_waiting_for() -> None:
    p = _plugin()
    attempt = _attempt(remaining=30.0)
    text = p._shutdown_dialog_text(attempt, time.monotonic() + 12.0)
    assert "95,468" in text
    assert "967056x2797x2" in text
    assert _only_int_near(text, 12)
    assert "No further providers" in text


def test_the_wait_survives_a_qt_without_an_event_loop(monkeypatch) -> None:
    """processEvents can fail if Qt is already torn down. The wait is what
    matters; the repaint is a courtesy, so a failure there must not skip it."""
    p = _plugin()
    wallet = _Wallet()

    def _boom():
        raise RuntimeError("no event loop")

    monkeypatch.setattr(qt_mod.QApplication, "processEvents", staticmethod(_boom))
    p._note_attempt_started(wallet, _attempt(remaining=0.3))
    started = time.monotonic()
    p._await_shutdown(wallet)
    assert 0.3 <= time.monotonic() - started < 3.0


def test_a_dialog_that_cannot_be_shown_does_not_abort_the_wait(monkeypatch) -> None:
    """``_build_shutdown_dialog`` answers (None, None) when Qt cannot show a
    window -- no display, Qt already torn down. The wait must still run to
    completion: not showing a progress window is a cosmetic loss, closing on top
    of a live Lightning payment is not."""
    p = _plugin()
    wallet = _Wallet()
    monkeypatch.setattr(qt_mod, "SHUTDOWN_DIALOG_DELAY_SEC", 0.05)
    monkeypatch.setattr(qt_mod.Plugin, "_build_shutdown_dialog",
                        lambda self, attempt, deadline: (None, None))
    p._note_attempt_started(wallet, _attempt(remaining=0.3))
    started = time.monotonic()
    p._await_shutdown(wallet)
    assert 0.3 <= time.monotonic() - started < 3.0


def test_the_real_dialog_builder_never_raises(qapp) -> None:
    """Whatever it returns, it must not throw: it is called from inside the close
    path, where an exception would leave the window half-torn-down."""
    p = _plugin()
    dialog, label = p._build_shutdown_dialog(_attempt(), time.monotonic() + 30.0)
    assert (dialog is None) == (label is None)
    if dialog is not None:
        assert "967056x2797x2" in label.text()
        dialog.close()
