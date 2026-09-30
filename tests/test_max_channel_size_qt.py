"""GUI tests for the max-channel-size field on the Settings tab.

The field itself is a plain sat count (unlike the liquidity goal directly below
it, which is entered as money), so most of what matters here is the CROSS-FIELD
validation: the settings tab is what makes the two states the engine cannot act on
-- a ceiling below the liquidity goal, and a ceiling below the channel-funding
floor -- unreachable in the first place. The engine declines rather than churning
if a hand-edited config creates them anyway; this is the layer that stops a user
creating them by accident.

Needs PyQt6 and Electrum's Qt GUI; skipped when either is unavailable."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QLabel, QLineEdit, QPushButton, QTabWidget,
)

from electrum.gui.qt.amountedit import BTCAmountEdit  # noqa: E402
from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    DEFAULT_MAX_CHANNEL_SIZE_SAT,
)

# The config/wallet/plugin fakes are reused wholesale from the main Qt tab tests
# rather than duplicated (and left to drift) here.
from test_qt_tab import (  # type: ignore  # noqa: E402
    _FakeWallet, _FakeWindow, _make_plugin, _plain_line_edits,
)

LABEL = "Max channel size (sat, 0 = no limit)"
# Index in the Settings tab's plain line-edits: see the field order pinned in
# test_qt_tab.test_apply_persists_and_clamps.
MAX_SIZE_INDEX = 2


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _build():
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    settings_tab = p._tabs[wallet].container.findChild(QTabWidget).widget(0)
    return p, settings_tab


def _size_edit(tab) -> QLineEdit:
    return _plain_line_edits(tab)[MAX_SIZE_INDEX]


def _min_onchain_edit(tab) -> QLineEdit:
    return _plain_line_edits(tab)[0]


def _goal_edit(tab) -> BTCAmountEdit:
    edits = tab.findChildren(BTCAmountEdit)
    assert len(edits) == 1, "expected exactly one BTC amount field on Settings"
    return edits[0]


def _apply(tab) -> None:
    next(b for b in tab.findChildren(QPushButton)
         if b.text() == "Apply").click()


def _status_text(tab) -> str:
    return " ".join(lbl.text() for lbl in tab.findChildren(QLabel))


# --- the field is present, labelled and explained -------------------------
def test_the_field_is_on_the_settings_tab_and_labelled(qapp) -> None:
    # On Settings, not Advanced: without it the plugin funds a channel with the
    # whole on-chain balance, so it is a first-order bound, not a tuning knob.
    _, tab = _build()
    assert any(lbl.text() == LABEL for lbl in tab.findChildren(QLabel))


def test_the_field_sits_with_the_other_channel_shape_knobs(qapp) -> None:
    # Directly under "Maximum number of channels" (index 1), so how many / how
    # big / what size to aim for read together. Also pins that indices 0 and 1 did
    # not move, which the other Qt tests address positionally.
    _, tab = _build()
    edits = _plain_line_edits(tab)
    assert edits.index(_size_edit(tab)) == MAX_SIZE_INDEX == 2


def test_the_field_explains_itself(qapp) -> None:
    _, tab = _build()
    label = next(lbl for lbl in tab.findChildren(QLabel) if lbl.text() == LABEL)
    for tip in (label.toolTip(), _size_edit(tab).toolTip()):
        assert "one very large channel" in tip
        assert "0 = no ceiling" in tip
        # The two constraints the validation below enforces are stated up front.
        assert "liquidity goal" in tip
        assert "new opens only" in tip


def test_the_field_shows_the_stored_value(qapp) -> None:
    p, tab = _build()
    assert _size_edit(tab).text() == str(
        p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT)


# --- the round trip -------------------------------------------------------
def test_apply_persists_the_ceiling(qapp) -> None:
    p, tab = _build()
    _size_edit(tab).setText("2500000")
    _apply(tab)
    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT == 2_500_000
    assert "saved" in _status_text(tab)


def test_apply_reloads_the_field_from_config(qapp) -> None:
    # The positional reload list in _reload_settings_fields has to stay in step
    # with the `fields` list; a mismatch silently shows one field's value in
    # another's box (which is exactly how this was first caught).
    p, tab = _build()
    _size_edit(tab).setText("2500000")
    _apply(tab)
    assert _size_edit(tab).text() == "2500000"
    assert _min_onchain_edit(tab).text() == str(
        p.config.INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT)


def test_zero_is_accepted_as_no_ceiling(qapp) -> None:
    # 0 is how the feature is turned off, so it must pass validation even though
    # it is below both the goal and the funding floor.
    p, tab = _build()
    _size_edit(tab).setText("0")
    _apply(tab)
    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT == 0
    assert "saved" in _status_text(tab)


def test_a_non_numeric_ceiling_is_refused(qapp) -> None:
    p, tab = _build()
    before = p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT
    _size_edit(tab).setText("lots")
    _apply(tab)
    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT == before
    assert "Invalid value for" in _status_text(tab)


def test_a_negative_ceiling_is_refused(qapp) -> None:
    p, tab = _build()
    before = p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT
    _size_edit(tab).setText("-1")
    _apply(tab)
    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT == before
    assert "cannot be negative" in _status_text(tab)


# --- the cross-field validation ------------------------------------------
def test_a_ceiling_below_the_goal_is_refused(qapp) -> None:
    """The state the engine cannot act on: every channel opened would count as
    undersized the moment it existed, so the plugin would sit configured and
    quietly stuck. Refused here, where the user can see both numbers at once."""
    p, tab = _build()
    before = p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT
    _goal_edit(tab).setAmount(1_000_000)
    _size_edit(tab).setText("900000")
    _apply(tab)
    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT == before
    status = _status_text(tab)
    assert "below the liquidity goal" in status
    # Both numbers named, and a way out given.
    assert "900000" in status and "1000000" in status
    assert "Raise the max channel size or lower the goal" in status


def test_the_refusal_persists_nothing_at_all(qapp) -> None:
    # Everything is parsed and validated before anything is written, so a bad
    # ceiling must not leave the other fields half-saved.
    p, tab = _build()
    goal_before = p.config.INBOUND_LIQUIDITY_GOAL_SAT
    channels_before = p.config.INBOUND_LIQUIDITY_MAX_CHANNELS
    _plain_line_edits(tab)[1].setText("5")        # max-channels
    _goal_edit(tab).setAmount(1_000_000)
    _size_edit(tab).setText("900000")
    _apply(tab)
    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNELS == channels_before
    assert p.config.INBOUND_LIQUIDITY_GOAL_SAT == goal_before


def test_a_ceiling_equal_to_the_goal_is_accepted(qapp) -> None:
    # The boundary: a channel exactly at the goal is not undersized, so there is
    # nothing to churn.
    p, tab = _build()
    _goal_edit(tab).setAmount(1_000_000)
    _size_edit(tab).setText("1000000")
    _apply(tab)
    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT == 1_000_000
    assert p.config.INBOUND_LIQUIDITY_GOAL_SAT == 1_000_000


def test_raising_the_goal_and_the_ceiling_together_is_accepted(qapp) -> None:
    """Validated against the values being applied in THIS click, not the stored
    ones -- otherwise a user raising both at once would be judged against the old
    goal and refused for a conflict they were in the middle of fixing."""
    p, tab = _build()
    _goal_edit(tab).setAmount(3_000_000)
    _size_edit(tab).setText("4000000")
    _apply(tab)
    assert p.config.INBOUND_LIQUIDITY_GOAL_SAT == 3_000_000
    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT == 4_000_000


def test_a_ceiling_below_the_goal_is_fine_when_the_goal_is_off(qapp) -> None:
    # Goal 0 disables the replacement rule, so there is no conflict to have.
    p, tab = _build()
    _goal_edit(tab).setAmount(0)
    _size_edit(tab).setText("300000")
    _apply(tab)
    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT == 300_000


def test_a_ceiling_below_the_funding_floor_is_refused(qapp) -> None:
    """The other unactionable state: below the floor Electrum will fund at, no
    channel can be opened at all, so the plugin would be permanently inert."""
    p, tab = _build()
    before = p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT
    floor = p.effective_min_funding_sat()
    _goal_edit(tab).setAmount(0)             # take the goal check out of play
    _size_edit(tab).setText(str(floor - 1))
    _apply(tab)
    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT == before
    status = _status_text(tab)
    assert "channel-funding floor" in status
    assert str(floor) in status


def test_the_floor_check_uses_the_min_onchain_being_applied(qapp) -> None:
    """The floor tracks min-on-chain-to-open, which is on the same form. Lowering
    it in the same click lowers the floor, so a ceiling that would be refused
    against the OLD floor must be accepted against the new one."""
    p, tab = _build()
    _goal_edit(tab).setAmount(0)
    _min_onchain_edit(tab).setText("30000")
    _size_edit(tab).setText("40000")
    _apply(tab)
    assert p.config.INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT == 30_000
    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT == 40_000


def test_the_shipped_default_passes_its_own_validation(qapp) -> None:
    # A default the settings tab would refuse to save would be indefensible.
    p, tab = _build()
    _size_edit(tab).setText(str(DEFAULT_MAX_CHANNEL_SIZE_SAT))
    _apply(tab)
    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT == DEFAULT_MAX_CHANNEL_SIZE_SAT
    assert "saved" in _status_text(tab)
