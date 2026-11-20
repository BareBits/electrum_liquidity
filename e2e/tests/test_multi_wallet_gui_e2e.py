"""Multiple wallets open in ONE real Electrum Qt process: every window gets a
Liquidity tab, and every wallet keeps its own settings.

This is the half the mocked GUI tests cannot cover. ``tests/test_multi_wallet_tab.py``
drives the plugin against a fake window whose ``.tabs`` is a bare QTabWidget;
here the windows are real ``ElectrumWindow``s built by a real ``ElectrumGui``
over a real ``Daemon``, with the plugin loaded by Electrum's own plugin loader
and its hooks dispatched by Electrum's own ``run_hook``. So it proves the thing
that was actually in doubt: that opening a second wallet in the same Electrum
really does produce a second Liquidity tab.

It also covers the path the reported symptom most likely came from -- the
Plugins dialog's ``reload_windows()``, which closes and rebuilds every open
window -- and the one that produced it in a unit test: a window replaced without
``close_wallet`` firing.

It also covers the analytics consent screen, which is per wallet for the same
reason the settings are: every wallet must be asked in its own right, and its
answer must land in its own file. That lives here rather than in a module of
its own because ``ElectrumGui.__init__`` constructs a ``QApplication``
unconditionally, and a second one in the same process is undefined behaviour in
Qt -- so there is exactly one real-Qt-GUI module, and this is it.

The last section covers the settings tabs' own surface in the same real
windows: the Channel partners tab's node-name column (resolved through
Electrum's REAL ``LNPeerManager.get_node_alias``, against a real ``LNWallet``),
the click-to-sort column headers, and the big green Apply actually writing to a
real wallet file.

No rig needed: offline and offscreen, with freshly created regtest wallets, so
there is no bitcoind, no server and no network -- seconds to run, and most of
that is importing Electrum and building three real windows.

Run:  RUN_GUI_E2E=1 .venv-electrum/bin/python -m pytest \\
          tests/test_multi_wallet_gui_e2e.py -q -s
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from typing import Callable, Dict, List, Optional

import pytest

if os.environ.get("RUN_GUI_E2E") != "1" and os.environ.get("RUN_RIG_E2E") != "1":
    pytest.skip("set RUN_GUI_E2E=1 to run the real-Electrum-Qt multi-wallet test",
                allow_module_level=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _import_real_electrum():
    """Bind the INSTALLED ``electrum`` package, not the rig's source symlink.

    ``e2e/electrum`` is a symlink to the Electrum *repo root*, which is not a
    package, so with ``e2e/`` on sys.path ``import electrum`` resolves to that
    bare directory as a NAMESPACE package and Electrum's own internal imports
    fail. Verbatim the same problem, and the same fix, as the helper of this
    name in test_goal_buffer_e2e.py -- see the long note there.

    Returns the import error, if any, so this module can skip rather than fail
    on a machine with no PyQt6.
    """
    e2e_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bound = sys.modules.get("electrum")
    if bound is None or getattr(bound, "__file__", None) is None:
        for name in [n for n in list(sys.modules)
                     if n == "electrum" or n.startswith("electrum.")]:
            del sys.modules[name]
    saved = list(sys.path)
    sys.path[:] = [p for p in sys.path if os.path.abspath(p) != e2e_dir]
    try:
        import electrum  # noqa: F401
        assert electrum.__file__ is not None, \
            "still bound to the namespace package after cleaning sys.path"
        import electrum.gui.qt  # noqa: F401
        import electrum.plugins.inbound_liquidity.qt  # noqa: F401
        return None
    except Exception as exc:       # no PyQt6, or Electrum not installed
        return exc
    finally:
        sys.path[:] = saved


_ELECTRUM_IMPORT_ERROR = _import_real_electrum()
if _ELECTRUM_IMPORT_ERROR is not None:
    pytest.skip(f"needs Electrum's Qt GUI importable: {_ELECTRUM_IMPORT_ERROR!r}",
                allow_module_level=True)

WALLET_NAMES = ("liq_gui_a", "liq_gui_b", "liq_gui_c")

# What the user picks on each wallet's analytics consent screen -- one wallet
# per answer, so every branch of the choice is exercised against a real wallet
# file: a mid level, an explicit "no thanks", and the full level.
CONSENT_ANSWERS: Dict[str, int] = {
    "liq_gui_a": 2,     # LEVEL_SETTINGS
    "liq_gui_b": 0,     # LEVEL_OFF -- a deliberate, recorded "no"
    "liq_gui_c": 3,     # LEVEL_LIQUIDITY
}
# Reachable-looking and certainly dead: port 1 is privileged and nothing is
# listening, so an opt-in here can attempt a POST and get nowhere. NOT one of
# the disabling words, or the consent screen would be skipped entirely.
DEAD_USAGE_ENDPOINT = "http://127.0.0.1:1/report.php"


class _ConsentDriver:
    """Stands in for the user in front of the analytics consent screen.

    Installed over ``WindowModalDialog.exec`` for the whole module, because the
    screen comes up as each wallet's window opens and a modal with nobody to
    click it would hang this module for its whole timeout. It recognises the
    consent screen by its title and its radio buttons, picks the level that
    wallet is meant to pick, and presses Save. Any OTHER modal that tries to
    open is rejected rather than driven: silently answering a dialog we did not
    recognise would be worse than failing.
    """

    def __init__(self) -> None:
        self.titles: List[str] = []
        self.bodies: List[str] = []
        self.unexpected: List[str] = []
        self.hook: Callable[[str], None] = lambda name: None

    def install(self, monkeypatch) -> None:
        from electrum.gui.qt.util import WindowModalDialog
        # A plain function, not the driver itself: a callable instance set as a
        # class attribute is not a descriptor, so `dialog.exec()` would be
        # called with no arguments at all.
        monkeypatch.setattr(WindowModalDialog, "exec",
                            lambda dialog: self(dialog), raising=False)

    def __call__(self, dialog) -> int:
        from PyQt6.QtWidgets import QLabel, QRadioButton

        title = dialog.windowTitle() or ""
        radios = dialog.findChildren(QRadioButton)
        if "Help improve this plugin?" not in title or not radios:
            self.unexpected.append(title)
            return 0
        self.titles.append(title)
        self.bodies.append(" ".join(l.text() for l in dialog.findChildren(QLabel)))
        name = next((n for n in WALLET_NAMES if n in title), None)
        assert name is not None, \
            f"the consent screen does not name a wallet: {title!r}"
        level = CONSENT_ANSWERS[name]
        for button in radios:
            text = button.text()
            if level == 0 and text.startswith("No thanks"):
                button.setChecked(True)
            elif level != 0 and text.startswith(f"{level} "):
                button.setChecked(True)
        self.hook(name)
        return 1                    # "Save my choice"

    def count_for(self, name: str) -> int:
        return sum(1 for title in self.titles if name in title)


@pytest.fixture(scope="module")
def gui(request):
    """A real Qt GUI over a real (offline) daemon, with the plugin enabled.

    Module-scoped: one Electrum process' worth of state, which is exactly the
    subject under test -- several wallets sharing one plugin instance.
    """
    from _pytest.monkeypatch import MonkeyPatch

    monkeypatch = MonkeyPatch()
    request.addfinalizer(monkeypatch.undo)

    from electrum import constants
    from electrum.simple_config import SimpleConfig
    from electrum.plugins.inbound_liquidity import (  # type: ignore
        USAGE_STATS_ALLOW_REGTEST_ENV, USAGE_STATS_ENDPOINT_ENV,
    )

    # Open both gates that would otherwise skip the consent screen: these are
    # regtest wallets (suppressed), and the shipped endpoint is the live one,
    # which must never be contacted from a test.
    monkeypatch.setenv(USAGE_STATS_ALLOW_REGTEST_ENV, "1")
    monkeypatch.setenv(USAGE_STATS_ENDPOINT_ENV, DEAD_USAGE_ENDPOINT)

    datadir = tempfile.mkdtemp(prefix="liq-gui-e2e-")
    config = SimpleConfig({"electrum_path": datadir, "regtest": True, "offline": True})
    constants.net = constants.BitcoinRegtest

    # The asyncio loop has to exist before a wallet is built: creating addresses
    # triggers callbacks, and Electrum's trigger_callback needs the loop.
    from electrum.util import create_and_start_event_loop
    loop, stopping_fut, loop_thread = create_and_start_event_loop()

    from electrum.wallet import create_new_wallet
    from electrum.plugins.inbound_liquidity import (  # type: ignore
        SETTINGS_DB_KEY, SETTINGS_STORE_VERSION,
    )
    paths: List[str] = []
    for name in WALLET_NAMES:
        path = os.path.join(datadir, "regtest", "wallets", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        wallet = create_new_wallet(path=path, config=config, password=None,
                                   encrypt_file=False)["wallet"]
        if not wallet.has_lightning():
            wallet.init_lightning(password=None)
        # Pre-answer the plugin's OTHER one-time prompts, ON THE WALLET. They
        # are per-wallet settings, so setting them on the config would not
        # reach any of these wallets and each window would come up behind a
        # modal with nobody to click it -- which hung this module for its whole
        # timeout. The analytics consent screen is deliberately NOT pre-
        # answered: it is what the last section of this module tests, and
        # _ConsentDriver is what clicks it.
        wallet.db.put(SETTINGS_DB_KEY, {"_v": SETTINGS_STORE_VERSION,
                                        "UPDATE_CHECK_PROMPTED": True,
                                        "TRAMPOLINE_WARNED": True})
        wallet.save_db()
        paths.append(path)

    # Two modal dialogs sit between us and a window, and neither is about this
    # feature: Electrum's testnet warning and its update-check opt-in. A modal
    # with nobody to click it would hang the test for ever.
    config.DONT_SHOW_TESTNET_WARNING = True
    config.AUTOMATIC_CENTRALIZED_UPDATE_CHECKS = False

    from electrum.plugin import Plugins
    config.set_key("plugins.inbound_liquidity.enabled", True)
    plugins = Plugins(config, gui_name="qt")
    plugin = plugins.get("inbound_liquidity")
    assert plugin is not None, "the plugin is not installed in this Electrum"

    from electrum.daemon import Daemon
    daemon = Daemon(config, listen_jsonrpc=False, start_network=False)

    from electrum.gui.qt import ElectrumGui
    gui_object = ElectrumGui(config=config, daemon=daemon, plugins=plugins)

    driver = _ConsentDriver()
    driver.install(monkeypatch)

    yield _Harness(gui_object=gui_object, plugin=plugin, paths=paths,
                   config=config, plugins=plugins, driver=driver)

    # Teardown, in the reverse order of the bring-up. This has to happen: the
    # asyncio loop runs on a NON-daemon thread (see
    # electrum/util.py:create_and_start_event_loop), so leaving it running keeps
    # the pytest process alive long after the tests have passed -- which is
    # exactly what happened before this, the module reporting "10 passed in 4s"
    # and then sitting there until its runner's timeout killed it.
    #
    # Each step is guarded: a failure to unwind must be reported as what it is,
    # not turned into a failure of the tests that already passed.
    for window in list(gui_object.windows):
        try:
            window.should_stop_wallet_on_close = False   # the daemon stops them
            window.close()
        except Exception as e:
            print(f"teardown: could not close a window: {e!r}")
    try:
        gui_object._cleanup_before_exit()
    except Exception as e:
        print(f"teardown: could not clean up the Qt app: {e!r}")
    try:
        asyncio.run_coroutine_threadsafe(daemon.stop(), loop).result(timeout=30)
    except Exception as e:
        print(f"teardown: could not stop the daemon: {e!r}")
    try:
        # Before the loop, not after: the plugin registry is a DaemonThread
        # whose on_stop needs a live loop (util.py:DaemonThread.on_stop), so
        # letting it exit at interpreter shutdown instead prints an
        # "event loop not created yet" traceback over an otherwise clean run.
        plugins.stop()
        plugins.stopped_event.wait(10)
    except Exception as e:
        print(f"teardown: could not stop the plugin registry: {e!r}")
    try:
        loop.call_soon_threadsafe(stopping_fut.set_result, 1)
        loop_thread.join(timeout=10)
    except Exception as e:
        print(f"teardown: could not stop the asyncio loop: {e!r}")


class _Harness:
    def __init__(self, *, gui_object, plugin, paths, config, plugins,
                 driver) -> None:
        self.gui = gui_object
        self.plugin = plugin
        self.paths = paths
        self.config = config
        self.plugins = plugins
        self.driver = driver
        self._opened: Dict[str, object] = {}

    def wallet_named(self, name: str):
        for window in self.gui.windows:
            if window.wallet.basename() == name:
                return window.wallet
        raise AssertionError(f"{name} is not open; have "
                             f"{[w.wallet.basename() for w in self.gui.windows]}")

    def pump(self) -> None:
        """Let the posted consent-queue drain timer fire.

        ``_schedule_usage_consent_drain`` goes through ``QTimer.singleShot(0)``
        rather than opening the next dialog directly, so nothing happens until
        the event loop runs -- which, in a test, means saying so.
        """
        from PyQt6.QtWidgets import QApplication
        for _ in range(5):
            QApplication.processEvents()

    def open(self, index: int):
        """Open wallet ``index`` in a new window, as the user would."""
        path = self.paths[index]
        window = self.gui.start_new_window(path, None)
        assert window is not None, f"Electrum would not open {path}"
        self._opened[path] = window
        return window

    def window_for(self, index: int):
        return self.gui.get_window_for_wallet_path(self.paths[index]) \
            if hasattr(self.gui, "get_window_for_wallet_path") else \
            next((w for w in self.gui.windows
                  if w.wallet.storage.path == self.paths[index]), None)


def _tab_titles(window) -> List[str]:
    return [window.tabs.tabText(i) for i in range(window.tabs.count())]


def _liquidity_tabs(window) -> int:
    return _tab_titles(window).count("Liquidity")


def _settings_tab(plugin, wallet):
    from PyQt6.QtWidgets import QTabWidget
    state = plugin._tabs[wallet]
    return state.container.findChild(QTabWidget).widget(0)


def _apply_max_channels(plugin, wallet, value: str) -> None:
    from PyQt6.QtWidgets import QLineEdit, QPushButton
    tab = _settings_tab(plugin, wallet)
    edits = [e for e in tab.findChildren(QLineEdit) if type(e) is QLineEdit]
    edits[1].setText(value)             # "Maximum number of channels"
    next(b for b in tab.findChildren(QPushButton) if b.text() == "Apply").click()


# --- the tab ---------------------------------------------------------------
def test_the_first_wallet_gets_a_liquidity_tab(gui) -> None:
    window = gui.open(0)
    assert _liquidity_tabs(window) == 1, _tab_titles(window)


def test_a_second_and_third_wallet_each_get_one_too(gui) -> None:
    """The reported symptom: a tab in only one of the open wallets."""
    second = gui.open(1)
    third = gui.open(2)
    assert _liquidity_tabs(second) == 1, _tab_titles(second)
    assert _liquidity_tabs(third) == 1, _tab_titles(third)
    # And the first one still has exactly one -- not zero (stolen) and not two.
    first = gui.window_for(0)
    assert _liquidity_tabs(first) == 1, _tab_titles(first)


def test_the_plugin_holds_one_tab_handle_per_open_wallet(gui) -> None:
    plugin = gui.plugin
    assert len(plugin._tabs) == len(WALLET_NAMES)
    windows = {w.wallet: w for w in gui.gui.windows}
    for wallet, state in plugin._tabs.items():
        assert state.window is windows[wallet], \
            "a tab handle names a window other than its wallet's"


def test_every_wallet_is_managed_concurrently(gui) -> None:
    """Not just a tab each: the plugin takes all three wallets on. (Offline, so
    ``start_wallet`` declines them -- assert on the tab-level bookkeeping that
    does apply, and on each wallet having its own settings view.)"""
    plugin = gui.plugin
    views = {id(plugin.settings(w)) for w in plugin._tabs}
    assert len(views) == len(WALLET_NAMES), "wallets are sharing a settings view"


# --- the settings ----------------------------------------------------------
def test_each_wallets_settings_are_its_own(gui) -> None:
    plugin = gui.plugin
    wallets = [w.wallet for w in gui.gui.windows]
    a, b = wallets[0], wallets[1]

    _apply_max_channels(plugin, a, "7")
    assert plugin.settings(a).INBOUND_LIQUIDITY_MAX_CHANNELS == 7
    assert plugin.settings(b).INBOUND_LIQUIDITY_MAX_CHANNELS != 7, \
        "applying on one wallet moved the other"

    _apply_max_channels(plugin, b, "1")
    assert plugin.settings(b).INBOUND_LIQUIDITY_MAX_CHANNELS == 1
    assert plugin.settings(a).INBOUND_LIQUIDITY_MAX_CHANNELS == 7


def test_a_setting_is_persisted_in_its_own_wallet_file(gui) -> None:
    """Through a REAL WalletDB, not a dict stand-in: the store has to survive
    Electrum's own put/save semantics."""
    from electrum.plugins.inbound_liquidity import SETTINGS_DB_KEY  # type: ignore
    plugin = gui.plugin
    a, b = [w.wallet for w in gui.gui.windows][:2]
    assert dict(a.db.get(SETTINGS_DB_KEY))["MAX_CHANNELS"] == 7
    assert dict(b.db.get(SETTINGS_DB_KEY))["MAX_CHANNELS"] == 1


def test_the_arm_switch_arms_only_its_own_wallet(gui) -> None:
    """The switch that decides whether a wallet moves funds at all."""
    from electrum.plugins.inbound_liquidity.qt_widgets import ToggleSwitch  # type: ignore
    plugin = gui.plugin
    a, b = [w.wallet for w in gui.gui.windows][:2]

    _settings_tab(plugin, a).findChild(ToggleSwitch).setChecked(True)
    assert plugin.settings(a).INBOUND_LIQUIDITY_AUTOMATION_ENABLED is True
    assert plugin.settings(b).INBOUND_LIQUIDITY_AUTOMATION_ENABLED is False
    assert _settings_tab(plugin, b).findChild(ToggleSwitch).isChecked() is False
    _settings_tab(plugin, a).findChild(ToggleSwitch).setChecked(False)


# --- the window-replacement paths -----------------------------------------
def test_reload_windows_leaves_every_wallet_with_a_tab(gui) -> None:
    """What the Plugins dialog does on enable/disable: close and rebuild every
    open window. Every one of them must come back with its tab."""
    gui.gui.reload_windows()
    assert len(gui.gui.windows) == len(WALLET_NAMES)
    for window in gui.gui.windows:
        assert _liquidity_tabs(window) == 1, \
            f"{window.wallet.basename()}: {_tab_titles(window)}"
    assert len(gui.plugin._tabs) == len(WALLET_NAMES)


def test_a_reloaded_wallet_keeps_its_own_settings(gui) -> None:
    """The settings are in the wallet file, so a rebuilt window reads them back
    -- and reads back ITS wallet's, not another's."""
    plugin = gui.plugin
    by_name = {w.wallet.basename(): w.wallet for w in gui.gui.windows}
    assert plugin.settings(by_name[WALLET_NAMES[0]]).INBOUND_LIQUIDITY_MAX_CHANNELS == 7
    assert plugin.settings(by_name[WALLET_NAMES[1]]).INBOUND_LIQUIDITY_MAX_CHANNELS == 1


def test_a_window_replaced_without_a_close_hook_still_gets_a_tab(gui) -> None:
    """``reload_window`` builds the new window whether or not the close was
    accepted, and ``closeEvent`` can refuse (a closing warning -- including one
    this plugin registers). The replacement window must still get a tab rather
    than being skipped because a handle for the dead window is still held.
    """
    window = gui.window_for(0)
    wallet = window.wallet
    plugin = gui.plugin
    before = plugin._tabs[wallet]

    # Rebuild the window the way reload_window does, but WITHOUT letting the
    # close run -- i.e. the case where closeEvent ignored it.
    window.should_stop_wallet_on_close = False
    replacement = gui.gui._create_window_for_wallet(wallet)
    try:
        assert _liquidity_tabs(replacement) == 1, _tab_titles(replacement)
        assert plugin._tabs[wallet] is not before
        assert plugin._tabs[wallet].window is replacement
    finally:
        replacement.close()


# --- the settings tabs' own surface, in a real window ----------------------
# These use the LAST wallet, so the per-wallet settings the tests above pinned
# on wallets 0 and 1 are left alone.
LAST = len(WALLET_NAMES) - 1

PARTNER_A = "02" + "aa" * 32     # given a trampoline name below
PARTNER_B = "03" + "bb" * 32     # no name: the ordinary case


def _sub_tabs(plugin, wallet):
    from PyQt6.QtWidgets import QTabWidget
    return plugin._tabs[wallet].container.findChild(QTabWidget)


def _tree_of(plugin, wallet, index):
    from PyQt6.QtWidgets import QTreeWidget
    return _sub_tabs(plugin, wallet).widget(index).findChild(QTreeWidget)


def _apply_of(plugin, wallet, index):
    from PyQt6.QtWidgets import QPushButton
    return next(b for b in _sub_tabs(plugin, wallet).widget(index).findChildren(QPushButton)
                if b.text() == "Apply")


@pytest.fixture
def partners(gui):
    """The last wallet's Channel partners tab, holding two peers.

    The peers are given a fault history rather than a channel: that is enough
    to list them, and it needs no funded regtest wallet. One of them is also
    registered as a hardcoded trampoline node, which is how a name resolves at
    all while gossip is off -- so the alias below comes back through Electrum's
    own ``LNPeerManager.get_node_alias``, not a stand-in for it.
    """
    from electrum import trampoline
    from electrum.lntransport import LNPeerAddr

    window = gui.window_for(LAST) or gui.open(LAST)
    wallet, plugin = window.wallet, gui.plugin
    assert wallet.lnworker is not None, "this wallet has no Lightning"
    assert wallet.lnworker.lnpeermgr is not None, "this LNWallet has no peer manager"

    saved = dict(trampoline._TRAMPOLINE_NODES_UNITTESTS)
    trampoline._TRAMPOLINE_NODES_UNITTESTS.update(
        {"Rig Trampoline": LNPeerAddr("127.0.0.1", 9735, bytes.fromhex(PARTNER_A))})
    plugin._record_peer_fault(wallet, PARTNER_A, "could not connect", hard=False)
    plugin._record_peer_fault(wallet, PARTNER_B, "force-closed on us", hard=False)
    plugin._tabs[wallet].refresh()
    try:
        yield plugin, wallet, _tree_of(plugin, wallet, 2)
    finally:
        trampoline._TRAMPOLINE_NODES_UNITTESTS.clear()
        trampoline._TRAMPOLINE_NODES_UNITTESTS.update(saved)
        plugin.clear_peer_reliability(wallet)


def _rows(tree, col):
    return [tree.topLevelItem(i).text(col) for i in range(tree.topLevelItemCount())]


def test_the_partners_tab_names_a_peer_electrum_can_name(partners) -> None:
    plugin, _wallet, tree = partners
    names = _rows(tree, plugin._PARTNER_NAME_COL)
    assert sorted(names) == ["Rig Trampoline", "—"], names


def test_the_partners_tab_headers_are_clickable_in_a_real_window(partners) -> None:
    _plugin, _wallet, tree = partners
    assert tree.header().sectionsClickable()
    assert tree.header().isSortIndicatorShown()


def test_clicking_a_real_header_reorders_the_rows(partners) -> None:
    plugin, _wallet, tree = partners
    col = plugin._PARTNER_NAME_COL
    tree.header().sectionClicked.emit(col)
    assert _rows(tree, col) == ["Rig Trampoline", "—"]
    tree.header().sectionClicked.emit(col)
    assert _rows(tree, col) == ["—", "Rig Trampoline"]


def test_the_providers_tab_headers_are_clickable_in_a_real_window(gui) -> None:
    window = gui.window_for(LAST) or gui.open(LAST)
    tree = _tree_of(gui.plugin, window.wallet, 1)
    assert tree.header().sectionsClickable()


def test_every_apply_in_a_real_window_is_big_and_green(gui) -> None:
    from electrum.plugins.inbound_liquidity.qt_widgets import (  # type: ignore
        ACCENT_GREEN, PRIMARY_BUTTON_MIN_HEIGHT, PRIMARY_BUTTON_MIN_WIDTH,
    )
    window = gui.window_for(LAST) or gui.open(LAST)
    wallet = window.wallet
    for index in range(4):          # Settings, Swap providers, Channel partners, Advanced
        btn = _apply_of(gui.plugin, wallet, index)
        title = _sub_tabs(gui.plugin, wallet).tabText(index)
        assert btn.minimumWidth() >= PRIMARY_BUTTON_MIN_WIDTH, title
        assert btn.minimumHeight() >= PRIMARY_BUTTON_MIN_HEIGHT, title
        assert ACCENT_GREEN in btn.styleSheet(), title


def test_the_green_apply_writes_to_the_real_wallet_file(gui) -> None:
    """End to end for the restyled button: click it in a real window, then read
    the value back off the wallet's own DB rather than the plugin's cache."""
    from PyQt6.QtWidgets import QCheckBox
    from electrum.plugins.inbound_liquidity import SETTINGS_DB_KEY  # type: ignore
    window = gui.window_for(LAST) or gui.open(LAST)
    wallet, plugin = window.wallet, gui.plugin
    partners_tab = _sub_tabs(plugin, wallet).widget(2)
    strict_cb = partners_tab.findChildren(QCheckBox)[0]
    assert strict_cb.text().startswith("Only open channels to preferred")
    assert not strict_cb.isChecked()            # premise

    strict_cb.setChecked(True)
    _apply_of(plugin, wallet, 2).click()
    try:
        assert plugin.settings(wallet).INBOUND_LIQUIDITY_PARTNERS_STRICT is True
        assert wallet.db.get(SETTINGS_DB_KEY, {}).get("PARTNERS_STRICT") is True
    finally:
        strict_cb.setChecked(False)
        _apply_of(plugin, wallet, 2).click()


# --- the analytics consent screen, once per wallet -------------------------
#
# The consent screen came up as each of the three windows was opened, back in
# the first tests of this module, and _ConsentDriver answered each one. These
# tests read back what that left behind.
#
# The claims, in descending order of how much they matter:
#
#   * EVERY WALLET IS ASKED. The in-session guard used to be one flag for the
#     whole process, so only the first wallet opened was ever offered the
#     screen. The others sent nothing -- but they had never been given the
#     choice, which is not the same thing as having declined, and is not what
#     the screen says happens.
#   * EACH ANSWER LANDS IN ITS OWN WALLET FILE, read back out of a real
#     WalletDB: the level, the consent flag, and a distinct install id, with
#     the wallet that declined left with no identifier at all.
#   * NO ANSWER LEAKS SIDEWAYS, in either direction.
#   * ONE SCREEN AT A TIME: a wallet opened while another's screen is up waits
#     its turn, and really is offered the screen once the first one closes.

def _stored(wallet) -> Dict[str, object]:
    """This wallet's plugin settings as they sit in its real WalletDB."""
    from electrum.plugins.inbound_liquidity import SETTINGS_DB_KEY  # type: ignore
    return dict(wallet.db.get(SETTINGS_DB_KEY) or {})


def test_every_wallet_was_offered_the_consent_screen(gui) -> None:
    for name in WALLET_NAMES:
        assert gui.driver.count_for(name) == 1, \
            f"{name} was offered the consent screen {gui.driver.count_for(name)} times"


def test_no_unrecognised_modal_was_driven(gui) -> None:
    """If some other dialog opened and was rejected, the answers these tests
    read back are not the ones they think they are."""
    assert gui.driver.unexpected == [], gui.driver.unexpected


def test_the_consent_screen_named_the_wallet_it_was_asking_about(gui) -> None:
    """By now the user has been asked three times, so each screen has to say
    which wallet it is for -- in the title and in the body."""
    for name in WALLET_NAMES:
        assert any(name in title for title in gui.driver.titles), name
        assert any(name in body for body in gui.driver.bodies), \
            f"the body of {name}'s consent screen did not name it"


def test_each_wallet_holds_the_level_it_was_given(gui) -> None:
    plugin = gui.plugin
    levels = {name: plugin.usage_stats_level(gui.wallet_named(name))
              for name in WALLET_NAMES}
    assert levels == CONSENT_ANSWERS, levels
    for name in WALLET_NAMES:
        assert plugin.usage_stats_prompted(gui.wallet_named(name)), name


def test_each_answer_is_in_that_wallets_real_db(gui) -> None:
    """Through a REAL WalletDB, not a dict stand-in: the record has to survive
    Electrum's own put/save semantics, since it is what the next start reads."""
    for name, level in CONSENT_ANSWERS.items():
        stored = _stored(gui.wallet_named(name))
        assert stored.get("USAGE_STATS_LEVEL") == level, (name, stored)
        assert stored.get("USAGE_STATS_PROMPTED") is True, (name, stored)


def test_only_the_opted_in_wallets_have_an_install_id(gui) -> None:
    """A wallet that declined never gets one -- nothing to leak, nothing to
    correlate. The two that opted in do not share one: three wallets are three
    installs, deliberately (see the note on the ConfigVar)."""
    from electrum.plugins.inbound_liquidity import usage_stats as us  # type: ignore
    plugin = gui.plugin
    declined = gui.wallet_named("liq_gui_b")
    assert plugin.usage_stats_install_id(declined) is None
    assert not _stored(declined).get("USAGE_STATS_INSTALL_ID")

    ids = [plugin.usage_stats_install_id(gui.wallet_named(n))
           for n in ("liq_gui_a", "liq_gui_c")]
    assert all(us.is_install_id(i) for i in ids), ids
    assert ids[0] != ids[1], f"two wallets are sharing an install id: {ids}"


def test_loading_every_wallet_again_does_not_ask_again(gui) -> None:
    """What the Plugins dialog's ``reload_windows()`` does to this plugin: fire
    ``load_wallet`` for every open window a second time. All three have
    answered by now, so the persisted per-wallet flag is what keeps the screen
    away -- and it has to do so for each of them, not just the first.

    Dispatched through Electrum's own ``run_hook``, the way the real reload
    does it, rather than by calling the method: that is the path a wrong hook
    registration would break.
    """
    from electrum.plugin import run_hook

    before = len(gui.driver.titles)
    for window in list(gui.gui.windows):
        run_hook('load_wallet', window.wallet, window)
    gui.pump()
    assert len(gui.driver.titles) == before, \
        "a wallet that had already answered was asked again"
    levels = {name: gui.plugin.usage_stats_level(gui.wallet_named(name))
              for name in WALLET_NAMES}
    assert levels == CONSENT_ANSWERS, levels


def test_a_wallet_opened_behind_the_screen_waits_its_turn(gui, monkeypatch) -> None:
    """The dialog runs a blocking ``exec()`` inside the ``load_wallet`` hook, so
    a wallet opened while it is up must be queued rather than stack a second
    copy of a long modal -- and must really be offered the screen afterwards,
    through the posted timer.

    Driven by re-opening the question on two wallets that have already answered
    (clearing the consent flag, which a user cannot do from the UI but is
    exactly the state a fresh pair of wallets is in).
    """
    from electrum.plugins.inbound_liquidity import usage_stats as us  # type: ignore

    plugin = gui.plugin
    first = gui.wallet_named(WALLET_NAMES[0])
    second = gui.wallet_named(WALLET_NAMES[1])
    windows = {w.wallet: w for w in gui.gui.windows}
    for wallet in (first, second):
        plugin.set_setting(wallet, "INBOUND_LIQUIDITY_USAGE_STATS_PROMPTED", False)
        plugin.set_setting(wallet, "INBOUND_LIQUIDITY_USAGE_STATS_LEVEL",
                           us.LEVEL_OFF)
    plugin._usage_consent_asked.clear()

    seen: List[str] = []
    # Recorded rather than asserted inside the hook: the prompt catches every
    # exception out of the dialog, so a bare assert in here would be swallowed
    # into a log line instead of failing this test.
    mid: Dict[str, object] = {}

    def _hook(name: str) -> None:
        seen.append(name)
        if name != WALLET_NAMES[0]:
            return
        # As if the user opened the second wallet while the first's screen was
        # up: its load_wallet path reaches the consent prompt, which must queue
        # rather than open a second dialog.
        plugin._maybe_prompt_usage_consent(windows[second], second)
        mid["seen"] = list(seen)
        mid["queued"] = list(plugin._usage_consent_queue)

    monkeypatch.setattr(gui.driver, "hook", _hook)
    plugin._maybe_prompt_usage_consent(windows[first], first)
    assert mid.get("seen") == [WALLET_NAMES[0]], \
        f"the second wallet's screen was stacked on the first's: {mid.get('seen')}"
    assert mid.get("queued") == [second], mid.get("queued")
    gui.pump()                      # lets the posted drain run
    assert seen == [WALLET_NAMES[0], WALLET_NAMES[1]], \
        f"the queued wallet was never offered the screen: {seen}"
    assert plugin._usage_consent_queue == []
    # And each of them still recorded its OWN answer, through the queue.
    for name in WALLET_NAMES[:2]:
        assert plugin.usage_stats_level(gui.wallet_named(name)) \
            == CONSENT_ANSWERS[name], name
