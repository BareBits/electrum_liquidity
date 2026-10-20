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
from typing import Dict, List, Optional

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


@pytest.fixture(scope="module")
def gui():
    """A real Qt GUI over a real (offline) daemon, with the plugin enabled.

    Module-scoped: one Electrum process' worth of state, which is exactly the
    subject under test -- several wallets sharing one plugin instance.
    """
    from electrum import constants
    from electrum.simple_config import SimpleConfig

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
        # Pre-answer the plugin's update-check opt-in, ON THE WALLET. It is a
        # per-wallet setting, so setting it on the config would not reach any of
        # these wallets and each window would come up behind a modal with nobody
        # to click it -- which hung this module for its whole timeout.
        wallet.db.put(SETTINGS_DB_KEY, {"_v": SETTINGS_STORE_VERSION,
                                        "UPDATE_CHECK_PROMPTED": True})
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

    yield _Harness(gui_object=gui_object, plugin=plugin, paths=paths,
                   config=config, plugins=plugins)

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
    def __init__(self, *, gui_object, plugin, paths, config, plugins) -> None:
        self.gui = gui_object
        self.plugin = plugin
        self.paths = paths
        self.config = config
        self.plugins = plugins
        self._opened: Dict[str, object] = {}

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
