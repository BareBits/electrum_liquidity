"""GUI tests for the opt-in usage statistics: the one-time consent screen and
the Usage statistics pane nested inside Advanced.

Two surfaces, and what is pinned here is mostly what must NOT happen:

  * the consent dialog opens on **Off**, so an impatient Return or a reflexive
    click cannot opt anyone in;
  * "Ask me later" (and Escape, which resolves to it) records NOTHING -- not a
    yes, and not a no either;
  * pressing Save records the answer in both directions, so a deliberate "no"
    is final and the screen does not come back;
  * the preview shown is the report that would be sent, for the level
    currently selected, before anything is saved;
  * nesting a second pane inside Advanced did not disturb the General pane --
    its line edits, its checkboxes and its Apply are all still where the rest
    of the suite addresses them.

Needs PyQt6 and Electrum's Qt GUI; skipped when either is unavailable."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import List, Optional

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtWidgets import (  # noqa: E402
    QCheckBox, QPlainTextEdit, QPushButton, QRadioButton, QTabWidget, QWidget,
)

from electrum.gui.qt.util import WindowModalDialog  # noqa: E402
from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    USAGE_STATS_ALLOW_REGTEST_ENV, USAGE_STATS_ENDPOINT_ENV,
)

from test_qt_tab import (  # noqa: E402,F401
    _FakeWallet, _FakeWindow, _advanced_pane, _make_plugin, _plain_line_edits,
    _silence_log_tab_timer, qapp,
)

us = pkg.usage_stats


@pytest.fixture(autouse=True)
def _neutral_environment(monkeypatch):
    """A live endpoint on mainnet, so the consent screen is offered by default
    and a stray variable in the developer's shell cannot change the outcome."""
    monkeypatch.delenv(USAGE_STATS_ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(USAGE_STATS_ALLOW_REGTEST_ENV, raising=False)
    from electrum import constants
    monkeypatch.setattr(constants, "net",
                        SimpleNamespace(NET_NAME="mainnet"), raising=False)


class _QtWindow(QWidget):
    """A real QWidget standing in for ElectrumWindow.

    The consent dialog is a WindowModalDialog parented to the window, and Qt
    requires a real QWidget parent -- so the plain object the rest of the suite
    uses as a window will not do here.
    """

    def __init__(self) -> None:
        super().__init__()
        self.tabs = QTabWidget()
        self.questions: List[str] = []

    def question(self, msg, **kwargs) -> bool:
        self.questions.append(msg)
        return False

    def statusBar(self):
        return SimpleNamespace(showMessage=lambda *a, **k: None,
                               addPermanentWidget=lambda *a, **k: None,
                               removeWidget=lambda *a, **k: None)

    def get_decimal_point(self) -> int:
        return 8


def _built(**overrides):
    """A plugin with its Liquidity tab built for one wallet."""
    p = _make_plugin(**overrides) if overrides else _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    return p, wallet


def _usage_pane(plugin, wallet) -> QWidget:
    return _advanced_pane(plugin, wallet, "Usage statistics")


def _radios(pane) -> List[QRadioButton]:
    return pane.findChildren(QRadioButton)


def _radio_for(pane, level: int) -> QRadioButton:
    """The radio for a level. Off is the one that says so; the rest are
    prefixed with their number."""
    for button in _radios(pane):
        if level == us.LEVEL_OFF and button.text().startswith("Off"):
            return button
        if level != us.LEVEL_OFF and button.text().startswith(f"{level} "):
            return button
    raise AssertionError(
        f"no radio for level {level}; have {[b.text() for b in _radios(pane)]}")


def _apply(pane) -> QPushButton:
    buttons = [b for b in pane.findChildren(QPushButton) if b.text() == "Apply"]
    assert len(buttons) == 1, [b.text() for b in pane.findChildren(QPushButton)]
    return buttons[0]


def _preview_text(pane) -> str:
    boxes = pane.findChildren(QPlainTextEdit)
    assert boxes, "the pane has no preview box"
    return boxes[0].toPlainText()


# --- the nested tab, and what it must not have disturbed -----------------

def test_advanced_hosts_general_and_usage_statistics(qapp):
    p, wallet = _built()
    advanced = p._tabs[wallet].container.findChild(QTabWidget).widget(3)
    inner = advanced.findChild(QTabWidget)
    assert inner is not None, "Advanced has no inner tab bar"
    titles = [inner.tabText(i) for i in range(inner.count())]
    assert titles == ["General", "Usage statistics"]


def test_general_is_the_first_inner_tab(qapp):
    """Load-bearing, not cosmetic: the rest of the suite addresses General's
    line edits positionally, and that only holds while General is built and
    parented first."""
    p, wallet = _built()
    advanced = p._tabs[wallet].container.findChild(QTabWidget).widget(3)
    inner = advanced.findChild(QTabWidget)
    assert inner.tabText(0) == "General"
    assert inner.currentIndex() == 0, "Advanced should open on General"


def test_the_usage_pane_adds_no_checkbox_to_advanced(qapp):
    """Why the levels are radio buttons. One existing test flips every
    checkbox on the Advanced tab off; if this pane contributed one, that test
    would silently be switching reporting off as a side effect."""
    p, wallet = _built()
    general = _advanced_pane(p, wallet, "General")
    assert _usage_pane(p, wallet).findChildren(QCheckBox) == []
    # ...and the General pane still has exactly its own feature toggles.
    texts = [cb.text() for cb in general.findChildren(QCheckBox)]
    assert any("reliability" in t.lower() for t in texts), texts
    assert not any("usage" in t.lower() or "statistic" in t.lower()
                   for t in texts), texts


def test_the_general_panes_line_edits_are_still_where_they_were(qapp):
    """The daily ceilings are index 0 and 1 of the General pane."""
    p, wallet = _built()
    edits = _plain_line_edits(_advanced_pane(p, wallet, "General"))
    assert len(edits) >= 2
    assert edits[0].text().isdigit() and edits[1].text().isdigit()


def test_the_usage_pane_contains_no_line_edit(qapp):
    """Which is what keeps the General pane's positional indices valid even for
    a search over the whole Advanced tab."""
    assert _plain_line_edits(_usage_pane(*_built())) == []


# --- the pane's controls -------------------------------------------------

def test_the_pane_offers_off_and_the_three_levels(qapp):
    p, wallet = _built()
    pane = _usage_pane(p, wallet)
    assert len(_radios(pane)) == 1 + len(us.LEVELS)
    for level in (us.LEVEL_OFF, *us.LEVELS):
        _radio_for(pane, level)        # raises if missing


def test_the_pane_opens_on_the_stored_level(qapp):
    p, wallet = _built()
    pane = _usage_pane(p, wallet)
    assert _radio_for(pane, us.LEVEL_OFF).isChecked(), \
        "a wallet that has not opted in must show Off"


def test_the_pane_names_what_is_never_sent(qapp):
    """The promise has to be on the screen where the choice is made, not only
    in the dialog the user saw once."""
    from PyQt6.QtWidgets import QLabel
    p, wallet = _built()
    text = " ".join(l.text() for l in _usage_pane(p, wallet).findChildren(QLabel))
    lowered = text.lower()
    for subject in ("address", "transaction", "balance"):
        assert subject in lowered, f"the pane does not mention {subject}"


def test_the_pane_links_the_privacy_policy(qapp):
    from PyQt6.QtWidgets import QLabel
    p, wallet = _built()
    html = " ".join(l.text() for l in _usage_pane(p, wallet).findChildren(QLabel))
    assert "PRIVACY.md" in html
    assert "https://github.com/BareBits/electrum_liquidity" in html


# --- the live preview ----------------------------------------------------

def test_selecting_a_level_previews_it_without_saving(qapp):
    """The heart of informed consent: you see what level 3 would send while
    still opted out, and nothing is stored until Apply."""
    p, wallet = _built()
    pane = _usage_pane(p, wallet)
    _radio_for(pane, us.LEVEL_LIQUIDITY).setChecked(True)

    shown = json.loads(_preview_text(pane))
    assert shown["level"] == us.LEVEL_LIQUIDITY
    assert "channels_opened_total" in shown["properties"]
    # Nothing committed.
    assert p.usage_stats_level(wallet) == us.LEVEL_OFF
    assert p.usage_stats_install_id(wallet) is None


def test_the_preview_narrows_as_the_level_drops(qapp):
    p, wallet = _built()
    pane = _usage_pane(p, wallet)

    _radio_for(pane, us.LEVEL_LIQUIDITY).setChecked(True)
    level3 = set(json.loads(_preview_text(pane))["properties"])
    _radio_for(pane, us.LEVEL_SETTINGS).setChecked(True)
    level2 = set(json.loads(_preview_text(pane))["properties"])
    _radio_for(pane, us.LEVEL_COUNT).setChecked(True)
    level1 = set(json.loads(_preview_text(pane))["properties"])

    assert level1 < level2 < level3
    assert "channels_opened_total" not in level2
    assert "liquidity_goal_sat" not in level1


def test_off_previews_nothing(qapp):
    p, wallet = _built()
    pane = _usage_pane(p, wallet)
    _radio_for(pane, us.LEVEL_COUNT).setChecked(True)
    _radio_for(pane, us.LEVEL_OFF).setChecked(True)
    text = _preview_text(pane)
    assert "{" not in text, f"Off should not preview a payload: {text!r}"
    assert "nothing is sent" in text.lower()


def test_the_preview_is_what_would_be_sent(qapp):
    p, wallet = _built()
    pane = _usage_pane(p, wallet)
    _radio_for(pane, us.LEVEL_SETTINGS).setChecked(True)
    shown = json.loads(_preview_text(pane))
    # Same builder, same snapshot -- compare everything but the timestamp,
    # which moves between the two calls.
    would_send = us.build_payload(
        us.LEVEL_SETTINGS, p.build_usage_snapshot(wallet), now=0.0,
        install_id=p.usage_stats_install_id(wallet))
    shown.pop("sent_at"), would_send.pop("sent_at")
    assert shown == would_send


def test_a_preview_failure_does_not_take_the_tab_down(qapp, monkeypatch):
    p, wallet = _built()
    pane = _usage_pane(p, wallet)

    def _explode(*a, **k):
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(p, "usage_stats_preview", _explode)
    _radio_for(pane, us.LEVEL_COUNT).setChecked(True)       # must not raise
    assert "could not build" in _preview_text(pane).lower()


# --- applying ------------------------------------------------------------

def test_applying_a_level_persists_it_and_sends_one_report(qapp, monkeypatch):
    p, wallet = _built()
    requested: List[tuple] = []
    monkeypatch.setattr(p, "_request_usage_report",
                        lambda w, **kw: requested.append((w, kw)))
    pane = _usage_pane(p, wallet)
    _radio_for(pane, us.LEVEL_SETTINGS).setChecked(True)
    _apply(pane).click()

    assert p.usage_stats_level(wallet) == us.LEVEL_SETTINGS
    assert p.usage_stats_prompted(wallet)
    assert us.is_install_id(p.usage_stats_install_id(wallet))
    assert len(requested) == 1, "opting in should not wait a week to report"
    assert requested[0][1].get("force") is True


def test_applying_off_persists_and_sends_nothing(qapp, monkeypatch):
    p, wallet = _built()
    requested = []
    monkeypatch.setattr(p, "_request_usage_report",
                        lambda w, **kw: requested.append(w))
    pane = _usage_pane(p, wallet)
    _radio_for(pane, us.LEVEL_LIQUIDITY).setChecked(True)
    _apply(pane).click()
    requested.clear()

    _radio_for(pane, us.LEVEL_OFF).setChecked(True)
    _apply(pane).click()
    assert p.usage_stats_level(wallet) == us.LEVEL_OFF
    assert requested == [], "switching off must not send a farewell report"


def test_the_identifier_survives_switching_off_and_on(qapp, monkeypatch):
    p, wallet = _built()
    monkeypatch.setattr(p, "_request_usage_report", lambda w, **kw: None)
    pane = _usage_pane(p, wallet)

    _radio_for(pane, us.LEVEL_COUNT).setChecked(True)
    _apply(pane).click()
    first = p.usage_stats_install_id(wallet)

    _radio_for(pane, us.LEVEL_OFF).setChecked(True)
    _apply(pane).click()
    _radio_for(pane, us.LEVEL_LIQUIDITY).setChecked(True)
    _apply(pane).click()

    assert p.usage_stats_install_id(wallet) == first, \
        "a re-opt-in must not look like a second install"


def test_moving_the_radio_alone_does_not_start_reporting(qapp, monkeypatch):
    """Apply-gated like every other settings pane."""
    p, wallet = _built()
    requested = []
    monkeypatch.setattr(p, "_request_usage_report",
                        lambda w, **kw: requested.append(w))
    pane = _usage_pane(p, wallet)
    _radio_for(pane, us.LEVEL_LIQUIDITY).setChecked(True)
    assert p.usage_stats_level(wallet) == us.LEVEL_OFF
    assert requested == []


def test_a_pending_choice_marks_the_pane_dirty(qapp):
    """So the unsaved-changes guard will ask about it rather than dropping it.
    The nested pane is the case that silently failed before _ApplyPane.host."""
    p, wallet = _built()
    panes = {pane.name: pane for pane in p._tabs[wallet].panes}
    assert "Usage statistics" in panes, \
        "the usage pane is not registered with the unsaved-changes guard"
    usage = panes["Usage statistics"]
    assert not usage.tracker.is_dirty()
    _radio_for(_usage_pane(p, wallet), us.LEVEL_COUNT).setChecked(True)
    assert usage.tracker.is_dirty()


def test_both_advanced_panes_are_hosted_by_the_advanced_tab(qapp):
    """What makes the outer guard able to find a nested pane's edits at all."""
    p, wallet = _built()
    advanced = p._tabs[wallet].container.findChild(QTabWidget).widget(3)
    hosted = [pane for pane in p._tabs[wallet].panes if pane.host is advanced]
    assert {pane.name for pane in hosted} == {"Advanced", "Usage statistics"}


def test_the_guard_can_apply_the_nested_pane(qapp, monkeypatch):
    """The guard presses Apply on the user's behalf; for a nested pane that has
    to actually commit, not quietly no-op."""
    p, wallet = _built()
    monkeypatch.setattr(p, "_request_usage_report", lambda w, **kw: None)
    usage = next(pane for pane in p._tabs[wallet].panes
                 if pane.name == "Usage statistics")
    _radio_for(_usage_pane(p, wallet), us.LEVEL_SETTINGS).setChecked(True)
    assert usage.apply() is True
    assert p.usage_stats_level(wallet) == us.LEVEL_SETTINGS


def test_applying_elsewhere_does_not_disturb_the_usage_level(qapp, monkeypatch):
    """The General pane's Apply writes two dozen settings in one batch; the
    usage level must not be one of them."""
    p, wallet = _built()
    monkeypatch.setattr(p, "_request_usage_report", lambda w, **kw: None)
    usage_pane = _usage_pane(p, wallet)
    _radio_for(usage_pane, us.LEVEL_LIQUIDITY).setChecked(True)
    _apply(usage_pane).click()

    general = _advanced_pane(p, wallet, "General")
    for cb in general.findChildren(QCheckBox):
        cb.setChecked(False)
    _apply(general).click()
    assert p.usage_stats_level(wallet) == us.LEVEL_LIQUIDITY


# --- the pane when nothing can be sent -----------------------------------

def test_the_pane_says_so_when_the_endpoint_is_switched_off(qapp, monkeypatch):
    """Otherwise the pane would claim to be reporting when it provably is
    not."""
    monkeypatch.setenv(USAGE_STATS_ENDPOINT_ENV, "disabled")
    from PyQt6.QtWidgets import QLabel
    p, wallet = _built()
    labels = [l.text() for l in _usage_pane(p, wallet).findChildren(QLabel)
              if l.isVisibleTo(_usage_pane(p, wallet))]
    assert any(USAGE_STATS_ENDPOINT_ENV in t for t in labels), labels


def test_the_pane_says_so_on_regtest(qapp, monkeypatch):
    from electrum import constants
    from PyQt6.QtWidgets import QLabel
    monkeypatch.setattr(constants, "net", SimpleNamespace(NET_NAME="regtest"))
    p, wallet = _built()
    labels = [l.text() for l in _usage_pane(p, wallet).findChildren(QLabel)]
    assert any("regtest" in t.lower() for t in labels), labels


# --- the consent screen --------------------------------------------------

def _drive_consent(monkeypatch, *, pick: Optional[int], accept: bool):
    """Stand in for the user: open the dialog, select ``pick``, then press Save
    (accept) or Ask me later (reject).

    Returns a dict that will hold a SNAPSHOT of the dialog's contents --
    label texts, radio states, preview text -- not the dialog itself. The
    dialog is parented to the window, so both are destroyed as soon as the
    prompt returns, and touching the widget afterwards raises "wrapped C/C++
    object has been deleted". Everything a test wants to assert about the
    screen therefore has to be read while it is still on screen, which is what
    this does.
    """
    from PyQt6.QtWidgets import QLabel

    captured: dict = {}

    def _fake_exec(dialog):
        if pick is not None:
            for button in dialog.findChildren(QRadioButton):
                text = button.text()
                if pick == us.LEVEL_OFF and text.startswith("No thanks"):
                    button.setChecked(True)
                elif pick != us.LEVEL_OFF and text.startswith(f"{pick} "):
                    button.setChecked(True)
        captured["labels"] = [l.text() for l in dialog.findChildren(QLabel)]
        captured["radios"] = [b.text() for b in dialog.findChildren(QRadioButton)]
        captured["checked"] = [b.text() for b in dialog.findChildren(QRadioButton)
                               if b.isChecked()]
        captured["buttons"] = [b.text() for b in dialog.findChildren(QPushButton)]
        previews = dialog.findChildren(QPlainTextEdit)
        captured["preview"] = previews[0].toPlainText() if previews else ""
        captured["title"] = dialog.windowTitle()
        return 1 if accept else 0

    monkeypatch.setattr(WindowModalDialog, "exec", _fake_exec, raising=False)
    return captured


def _prompted(p, monkeypatch, *, pick, accept):
    captured = _drive_consent(monkeypatch, pick=pick, accept=accept)
    monkeypatch.setattr(p, "_request_usage_report", lambda w, **kw: None)
    window, wallet = _QtWindow(), _FakeWallet()
    p._maybe_prompt_usage_consent(window, wallet)
    return wallet, captured


def test_the_consent_screen_opens_on_off(qapp, monkeypatch):
    """So an impatient Return, or a reflexive click on the default button,
    cannot opt anyone in."""
    p = _make_plugin()
    _, captured = _prompted(p, monkeypatch, pick=None, accept=False)
    assert captured["checked"] == ["No thanks — send nothing"], captured["checked"]


def test_the_consent_screen_previews_every_level(qapp, monkeypatch):
    p = _make_plugin()
    _, captured = _prompted(p, monkeypatch, pick=us.LEVEL_LIQUIDITY, accept=False)
    shown = json.loads(captured["preview"])
    assert shown["level"] == us.LEVEL_LIQUIDITY
    assert shown["install_id"] == us.INSTALL_ID_PLACEHOLDER, \
        "the consent screen must preview without minting an identifier"


def test_saving_a_level_opts_in(qapp, monkeypatch):
    p = _make_plugin()
    wallet, _ = _prompted(p, monkeypatch, pick=us.LEVEL_SETTINGS, accept=True)
    assert p.usage_stats_level(wallet) == us.LEVEL_SETTINGS
    assert p.usage_stats_prompted(wallet)
    assert us.is_install_id(p.usage_stats_install_id(wallet))


def test_saving_no_thanks_is_recorded_as_a_final_answer(qapp, monkeypatch):
    """A declined choice has to be remembered, or the screen comes back at
    every start."""
    p = _make_plugin()
    wallet, _ = _prompted(p, monkeypatch, pick=us.LEVEL_OFF, accept=True)
    assert p.usage_stats_level(wallet) == us.LEVEL_OFF
    assert p.usage_stats_prompted(wallet)


def test_ask_me_later_records_nothing_at_all(qapp, monkeypatch):
    """Not a yes, and not a no either. Escape and the title-bar close resolve
    here too, which is why neither may be read as an answer."""
    p = _make_plugin()
    wallet, _ = _prompted(p, monkeypatch, pick=us.LEVEL_LIQUIDITY, accept=False)
    assert p.usage_stats_level(wallet) == us.LEVEL_OFF
    assert not p.usage_stats_prompted(wallet), \
        "deferring must leave the question open, not answer it"
    assert p.usage_stats_install_id(wallet) is None


def test_the_screen_is_offered_only_once_per_session(qapp, monkeypatch):
    """A second wallet opening must not stack a second copy of a long dialog."""
    p = _make_plugin()
    opened = []

    monkeypatch.setattr(p, "_ask_usage_consent",
                        lambda window, wallet: opened.append(wallet) or None)
    window = _QtWindow()
    p._maybe_prompt_usage_consent(window, _FakeWallet())
    p._maybe_prompt_usage_consent(window, _FakeWallet())
    assert len(opened) == 1


def test_an_already_answered_wallet_is_not_asked_again(qapp, monkeypatch):
    p = _make_plugin()
    opened = []
    monkeypatch.setattr(p, "_ask_usage_consent",
                        lambda window, wallet: opened.append(wallet) or None)
    wallet = _FakeWallet()
    p.set_usage_stats_choice(wallet, us.LEVEL_OFF)
    p._maybe_prompt_usage_consent(_QtWindow(), wallet)
    assert opened == []


def test_a_wallet_opted_in_from_advanced_is_not_asked(qapp, monkeypatch):
    """They already answered, in the other place the question is asked."""
    p = _make_plugin()
    opened = []
    monkeypatch.setattr(p, "_ask_usage_consent",
                        lambda window, wallet: opened.append(wallet) or None)
    wallet = _FakeWallet()
    p.set_usage_stats_choice(wallet, us.LEVEL_COUNT, mark_prompted=False)
    p._maybe_prompt_usage_consent(_QtWindow(), wallet)
    assert opened == []


def test_the_screen_is_skipped_when_nothing_could_be_sent(qapp, monkeypatch):
    """Asking for permission we could not act on is noise. Both gates."""
    p = _make_plugin()
    opened = []
    monkeypatch.setattr(p, "_ask_usage_consent",
                        lambda window, wallet: opened.append(wallet) or None)

    monkeypatch.setenv(USAGE_STATS_ENDPOINT_ENV, "disabled")
    p._maybe_prompt_usage_consent(_QtWindow(), _FakeWallet())
    assert opened == []

    monkeypatch.delenv(USAGE_STATS_ENDPOINT_ENV)
    from electrum import constants
    monkeypatch.setattr(constants, "net", SimpleNamespace(NET_NAME="regtest"))
    p._usage_consent_asked = False
    p._maybe_prompt_usage_consent(_QtWindow(), _FakeWallet())
    assert opened == []


def test_a_dialog_failure_never_breaks_loading_a_wallet(qapp, monkeypatch):
    """And leaves the question open rather than recording a non-answer."""
    p = _make_plugin()

    def _explode(window, wallet):
        raise RuntimeError("no display")

    monkeypatch.setattr(p, "_ask_usage_consent", _explode)
    wallet = _FakeWallet()
    p._maybe_prompt_usage_consent(_QtWindow(), wallet)      # must not raise
    assert not p.usage_stats_prompted(wallet)
    assert not p._usage_consent_asked, \
        "a failed prompt should be retried, not marked as shown"


def test_opting_in_from_the_screen_sends_one_report(qapp, monkeypatch):
    p = _make_plugin()
    requested = []
    _drive_consent(monkeypatch, pick=us.LEVEL_COUNT, accept=True)
    monkeypatch.setattr(p, "_request_usage_report",
                        lambda w, **kw: requested.append(kw))
    p._maybe_prompt_usage_consent(_QtWindow(), _FakeWallet())
    assert len(requested) == 1
    assert requested[0].get("force") is True


def test_declining_from_the_screen_sends_nothing(qapp, monkeypatch):
    p = _make_plugin()
    requested = []
    _drive_consent(monkeypatch, pick=us.LEVEL_OFF, accept=True)
    monkeypatch.setattr(p, "_request_usage_report",
                        lambda w, **kw: requested.append(kw))
    p._maybe_prompt_usage_consent(_QtWindow(), _FakeWallet())
    assert requested == []


def test_the_screen_names_what_is_never_sent(qapp, monkeypatch):
    p = _make_plugin()
    _, captured = _prompted(p, monkeypatch, pick=None, accept=False)
    text = " ".join(captured["labels"])
    lowered = text.lower()
    for subject in ("address", "transaction", "balance", "seed"):
        assert subject in lowered, f"the consent screen does not mention {subject}"
    assert "PRIVACY.md" in text, "the consent screen does not link the policy"


def test_the_screen_says_where_to_change_it_later(qapp, monkeypatch):
    p = _make_plugin()
    _, captured = _prompted(p, monkeypatch, pick=None, accept=False)
    text = " ".join(captured["labels"])
    assert "Advanced" in text and "Usage statistics" in text


def test_the_screen_offers_off_plus_the_three_levels(qapp, monkeypatch):
    p = _make_plugin()
    _, captured = _prompted(p, monkeypatch, pick=None, accept=False)
    radios = captured["radios"]
    assert len(radios) == 1 + len(us.LEVELS)
    for level in us.LEVELS:
        assert any(t.startswith(f"{level} ") for t in radios), level
    # Both buttons are present, and Save is the affirmative one.
    assert "Save my choice" in captured["buttons"]
    assert "Ask me later" in captured["buttons"]
