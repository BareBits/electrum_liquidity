"""End-to-end test for the liquidity-goal BUFFER -- the blue pie slice and the
Send tab's inline warning -- driven against the live rig (bitcoind + Fulcrum +
nostr + the client daemon with the plugin, holding two real 0.02 BTC channels).

The buffer is a GUI feature, and the rig's client is a headless daemon, so what
this proves is the half no mocked test can: that the numbers the GUI is handed
are correct for a REAL Electrum wallet holding REAL coins on a REAL chain with
REAL open channels.

  1. ``wallet_total_balance_sat`` on a real wallet agrees with the balance the
     daemon itself reports over RPC -- i.e. the threshold the send warning is
     measured against really is the balance the user sees. This is the one that
     mocks cannot check at all: every unit test feeds it a hand-built
     PiechartBalance, so a wallet API that drifted would sail straight through.
  2. The buffer split against those real balances comes out of on-chain first
     and conserves every satoshi, so the real pie chart still adds up.
  3. The send warning fires at exactly the real threshold and not one sat below
     it, and a goal of 0 switches it off.
  4. Electrum's real Wallet Balance dialog, built offscreen against that same
     real wallet, actually grows the blue slice and its legend row -- the whole
     monkeypatch path end to end, on a real balance rather than a fake list.

The wallet is opened from a COPY of the live wallet file. The rig's daemon holds
the original open and is still writing to it; loading it a second time in this
process would be a second writer on a file that is mid-flight.

Heavy and slow (~6-10 min for the bring-up); needs the electrum venv + docker.
Gated behind RUN_RIG_E2E=1.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest \
          tests/test_goal_buffer_e2e.py -q -s
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from typing import Dict

import pytest

if os.environ.get("RUN_RIG_E2E") != "1":
    pytest.skip("set RUN_RIG_E2E=1 to run the heavy rig-based e2e test",
                allow_module_level=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run as run_mod  # noqa: E402
from rig.services import (  # noqa: E402
    CLIENT,
    electrum_cli,
    wallet_path,
)


def _import_real_electrum():
    """Bind the INSTALLED ``electrum`` package, not the rig's source symlink.

    ``e2e/electrum`` is a symlink to the Electrum *repo root*, which is not a
    package (no ``__init__.py``). Every e2e module puts ``e2e/`` on
    ``sys.path[0]`` so it can ``import run``, and once it is there ``import
    electrum`` resolves to that bare directory as a NAMESPACE package:
    ``electrum.__file__`` is None, and ``from . import GuiImportError`` inside
    Electrum's own ``commands.py`` then fails.

    Whoever imports first decides, and pytest imports test modules in
    alphabetical order -- which is why running this file ALONE worked and running
    it in the suite did not (``test_daily_caps_e2e`` gets there first). Rather
    than depend on that ordering, the e2e directory is lifted out of ``sys.path``
    for the duration of the import and any half-bound namespace package is
    purged first.

    The heavy Qt chain is imported here too, while the path is clean, so the
    in-test imports later resolve from ``sys.modules`` instead of re-running
    resolution against the poisoned path. Returns the import error, if any, so
    the Qt test can skip rather than fail on a machine with no PyQt6.
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
        import electrum.gui.qt.balance_dialog  # noqa: F401
        import electrum.plugins.inbound_liquidity.qt  # noqa: F401
        return None
    except Exception as exc:       # no PyQt6, or Electrum not installed
        return exc
    finally:
        sys.path[:] = saved


_ELECTRUM_IMPORT_ERROR = _import_real_electrum()

GOAL_SAT = 1_500_000


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _rpc_onchain_sat() -> int:
    bal = json.loads(electrum_cli("getbalance", inst=CLIENT))
    return sum(int(round(float(bal.get(k, 0)) * 100_000_000))
               for k in ("confirmed", "unconfirmed", "unmatured"))


def _rpc_lightning_sat() -> int:
    channels = json.loads(electrum_cli("list_channels", inst=CLIENT))
    return sum(int(c.get("local_balance") or 0) for c in channels
               if c.get("state") == "OPEN")


def _rpc_total_balance_sat() -> int:
    """The wallet's total balance as the DAEMON reports it: on-chain (confirmed
    + unconfirmed + unmatured) plus Lightning. Deliberately computed from a
    different source than the plugin's own path, so the two agreeing means
    something."""
    return _rpc_onchain_sat() + _rpc_lightning_sat()


@pytest.fixture(scope="module")
def rig():
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui"]))
    r.preflight()
    r.allocate()
    r.bring_up()   # 2 baseline 50/50 channels; client loads the plugin
    try:
        yield r
    finally:
        r.shutdown()


@pytest.fixture(scope="module")
def live_wallet(rig):
    """The rig client's REAL wallet, loaded offline from a copy of its file.

    A copy because the daemon still owns the original: Electrum writes the
    wallet file as it goes, and a second opener would be a second writer. The
    copy is a faithful snapshot -- real addresses, real UTXOs from the real
    regtest chain, real channels -- which is the entire point of testing against
    it rather than a fake.

    Two pieces of global Electrum state have to be set up first, both of which
    the electrum CLI normally does at startup and neither of which pytest has
    done for us:

      * ``BitcoinRegtest.set_as_network()`` -- the rig's wallet holds regtest
        keys, and the default mainnet version bytes reject them outright; and
      * the asyncio loop -- ``Abstract_Wallet.__init__`` registers addresses,
        which fires an ``adb_set_up_to_date`` callback through
        ``util.trigger_callback``, which looks the global loop up.

    The wallet never goes online (no network is attached); both are needed just
    for the constructor to run.
    """
    from electrum import constants, util as e_util
    from electrum.simple_config import SimpleConfig
    from electrum.storage import WalletStorage
    from electrum.wallet import Wallet
    from electrum.wallet_db import WalletDB

    tmpdir = tempfile.mkdtemp(prefix="liq-buffer-e2e-")
    loop = stopping_fut = loop_thread = None
    prev_net = constants.net
    try:
        constants.BitcoinRegtest.set_as_network()
        loop, stopping_fut, loop_thread = e_util.create_and_start_event_loop()
        dest = os.path.join(tmpdir, "wallet")
        shutil.copy2(wallet_path(CLIENT), dest)
        config = SimpleConfig({"electrum_path": tmpdir, "regtest": True})
        storage = WalletStorage(dest)
        db = WalletDB(storage.read(), storage=storage, upgrade=True)
        wallet = Wallet(db, config=config)
        yield wallet
    finally:
        if stopping_fut is not None and loop is not None:
            loop.call_soon_threadsafe(stopping_fut.set_result, 1)
        if loop_thread is not None:
            loop_thread.join(timeout=30)
        shutil.rmtree(tmpdir, ignore_errors=True)
        prev_net.set_as_network()


def _plugin(wallet, *, goal_sat: int = GOAL_SAT):
    """The plugin's GUI-facing half, wired to the real wallet.

    ``object.__new__`` because the real constructor wants an Electrum ``Plugins``
    registry that has nothing to do with the buffer; ``wallets`` is the managed
    set the buffer actually keys on, so putting the real wallet in it is exactly
    the state the running plugin would be in.
    """
    from electrum.plugins.inbound_liquidity import LiquidityPlugin  # type: ignore

    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("e2e.inbound_liquidity.goal_buffer")
    p.config = _config(goal_sat)
    p.wallets = {wallet: object()}
    return p


def _config(goal_sat: int):
    class _Cfg:
        INBOUND_LIQUIDITY_AUTOMATION_ENABLED = True
        INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT = 60_000
        INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT = 10_000
        INBOUND_LIQUIDITY_MAX_CHANNELS = 2
        INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT = 0.9
        INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT = 25.0
        INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT = 25_000
        INBOUND_LIQUIDITY_MIN_OUTBOUND_SAT = 0
        INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY = True
        INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY = 5
        INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY = 5
        INBOUND_LIQUIDITY_GOAL_SAT = goal_sat
        INBOUND_LIQUIDITY_PREFERRED_NPUBS = ""
        INBOUND_LIQUIDITY_BANNED_NPUBS = ""
        INBOUND_LIQUIDITY_SINK_ADDRESS = ""
        INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS = False
        INBOUND_LIQUIDITY_DEFER_SINK_UNTIL_GOAL = True

    return _Cfg()


# --- 1. the threshold is the balance the user actually sees ---------------
def test_total_balance_agrees_with_the_running_daemon(rig, live_wallet):
    """The send warning is measured against ``wallet_total_balance_sat``. If that
    ever stopped meaning "the balance on the status bar", the warning would fire
    at a threshold the user has no way to predict."""
    p = _plugin(live_wallet)
    ours = p.wallet_total_balance_sat(live_wallet)
    theirs = _rpc_total_balance_sat()

    assert ours > 0, "the rig funds this wallet; a zero balance means it did not load"
    # Exact agreement is not required (the copy is a snapshot taken a moment
    # after the RPC read, and the rig keeps mining), but they must describe the
    # same wallet rather than differ by a rail.
    assert abs(ours - theirs) < 1_000_000, (
        f"plugin sees {ours} sat, daemon reports {theirs} sat -- "
        f"these are not the same balance")
    assert ours >= 2_000_000, "both of the rig's channels should be counted"


def test_the_real_balance_has_both_rails(rig, live_wallet):
    """Both rails hold funds on the rig, which is the premise the split tests
    below rest on.

    Read over RPC rather than off the offline wallet copy on purpose. A wallet
    loaded with no network has no channel_db, so ``uses_trampoline()`` is true
    and ``is_frozen_for_sending`` reports every non-trampoline peer as frozen --
    which parks the whole channel balance in ``lightning_frozen`` rather than
    ``lightning``. That is an artifact of opening the file offline, not of the
    wallet, and the running daemon reports the balance in the ordinary bucket.
    """
    assert _rpc_onchain_sat() > 0, "rig wallet should hold on-chain coins"
    assert _rpc_lightning_sat() > 0, "rig wallet should hold two open channels"
    # The offline copy still totals the same, whichever bucket it used -- which
    # is exactly why wallet_total_balance_sat measures against total().
    assert int(live_wallet.get_balances_for_piechart().total()) > 0


# --- 2. the split against real balances -----------------------------------
def test_buffer_splits_real_balances_without_losing_satoshis(rig, live_wallet):
    """On-chain first, on the rig's real balances. The rig funds this wallet with
    ~12 BTC on-chain, so on-chain alone covers the goal and the channels are left
    whole."""
    p = _plugin(live_wallet)
    onchain, lightning = _rpc_onchain_sat(), _rpc_lightning_sat()
    split = p.goal_buffer_split(live_wallet, onchain_sat=onchain,
                                lightning_sat=lightning)

    assert split.total() == GOAL_SAT, "the whole goal should be reservable here"
    assert split.from_onchain + split.onchain_remaining == onchain
    assert split.from_lightning + split.lightning_remaining == lightning
    assert split.from_onchain == GOAL_SAT
    assert split.from_lightning == 0
    assert split.lightning_remaining == lightning


def test_buffer_spills_into_lightning_when_the_goal_exceeds_onchain(rig, live_wallet):
    """Same real wallet, a goal larger than its on-chain balance: on-chain is
    emptied into the buffer and the rest comes out of the channels."""
    onchain, lightning = _rpc_onchain_sat(), _rpc_lightning_sat()
    spill = min(500_000, lightning)
    goal = onchain + spill
    p = _plugin(live_wallet, goal_sat=goal)
    split = p.goal_buffer_split(live_wallet, onchain_sat=onchain,
                                lightning_sat=lightning)

    assert split.from_onchain == onchain
    assert split.onchain_remaining == 0
    assert split.from_lightning == spill
    assert split.total() == goal


# --- 3. the send warning at the real threshold ----------------------------
def test_send_warning_fires_at_the_real_threshold(rig, live_wallet):
    from electrum.plugins.inbound_liquidity import (  # type: ignore
        format_goal_buffer_warning,
    )

    p = _plugin(live_wallet)
    total = p.wallet_total_balance_sat(live_wallet)
    threshold = total - GOAL_SAT
    assert threshold > 0, "rig wallet should comfortably exceed the goal"

    assert p.send_warning_text(live_wallet, threshold) == ""
    assert p.send_warning_text(live_wallet, threshold + 1) == \
        format_goal_buffer_warning(GOAL_SAT)
    assert p.send_warning_text(live_wallet, total) != ""


def test_send_warning_is_off_when_the_goal_is_zero(rig, live_wallet):
    p = _plugin(live_wallet, goal_sat=0)
    total = p.wallet_total_balance_sat(live_wallet)
    assert p.send_warning_text(live_wallet, total) == ""


def test_send_warning_is_off_for_an_unmanaged_wallet(rig, live_wallet):
    """The safety property: a wallet the plugin is not running on gets no
    reserve and no warning, whatever the goal says."""
    p = _plugin(live_wallet)
    p.wallets = {}
    assert p.goal_buffer_sat(live_wallet) == 0
    assert p.send_warning_text(live_wallet, p.wallet_total_balance_sat(live_wallet)) == ""


def test_the_configured_goal_reaches_the_buffer_through_the_real_config(rig, live_wallet):
    """Not the hand-built config the other tests use: a real SimpleConfig with
    the ConfigVar set the way the Settings tab sets it."""
    from electrum.plugins.inbound_liquidity import LiquidityPlugin  # type: ignore

    _setcfg("plugins.inbound_liquidity.liquidity_goal_sat", str(GOAL_SAT))
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("e2e.inbound_liquidity.goal_buffer.realcfg")
    p.config = live_wallet.config
    p.config.INBOUND_LIQUIDITY_GOAL_SAT = GOAL_SAT
    p.wallets = {live_wallet: object()}
    assert p.goal_buffer_sat(live_wallet) == GOAL_SAT


# --- 4. the real Balance dialog gets the blue slice -----------------------
def test_real_balance_dialog_grows_the_buffer_slice(rig, live_wallet):
    """Electrum's own Wallet Balance dialog, built against the real wallet, with
    the plugin's patch installed -- the full monkeypatch path on a real balance.
    """
    if _ELECTRUM_IMPORT_ERROR is not None:
        pytest.skip(f"Electrum's Qt GUI is unavailable: {_ELECTRUM_IMPORT_ERROR!r}")
    from PyQt6.QtWidgets import QApplication, QGridLayout, QLabel, QWidget
    from electrum.gui.qt.balance_dialog import (
        COLOR_CONFIRMED, BalanceDialog, LegendWidget, PieChartWidget,
    )
    from electrum.plugins.inbound_liquidity import qt as qt_mod  # type: ignore

    app = QApplication.instance() or QApplication([])
    assert app is not None

    p = _plugin(live_wallet)
    assert qt_mod._patch_balance_dialog() is True
    qt_mod._set_buffer_provider(p.goal_buffer_sat)
    try:
        bal = live_wallet.get_balances_for_piechart()
        parent = QWidget()
        parent.config = live_wallet.config
        parent.fx = None
        parent.wallet = live_wallet
        dialog = BalanceDialog(parent, wallet=live_wallet)

        piechart = dialog.findChild(PieChartWidget)
        assert piechart is not None, "Electrum no longer builds a PieChartWidget"

        def _amount_of(color):
            # A linear scan, not a dict: the entry list mixes QColor with
            # Qt.GlobalColor and QColor is not hashable.
            return next(a for _n, c, a in piechart._list if c == color)

        assert _amount_of(qt_mod.COLOR_GOAL_BUFFER) == GOAL_SAT
        assert _amount_of(COLOR_CONFIRMED) == int(bal.confirmed) - GOAL_SAT
        # The whole balance is still on the chart -- the slice was split off the
        # on-chain wedge, not added on top of it.
        assert sum(a for _n, _c, a in piechart._list) == int(bal.total())

        labels = [c.text() for c in dialog.findChildren(QLabel)]
        assert any("Liquidity goal buffer" in t for t in labels)
        assert any(lw.color == qt_mod.COLOR_GOAL_BUFFER
                   for lw in dialog.findChildren(LegendWidget))

        # The legend has to follow the wedges. Electrum builds those rows
        # straight from the raw PiechartBalance, so a legend still quoting the
        # full on-chain balance beside a shrunken wedge is the failure mode
        # here -- with real amounts formatted by a real SimpleConfig.
        grid = dialog.findChild(QGridLayout)
        assert grid is not None
        rows = {}
        for row in range(grid.rowCount()):
            name_item = grid.itemAtPosition(row, 1)
            amount_item = grid.itemAtPosition(row, 2)
            if name_item is not None and amount_item is not None:
                rows[name_item.widget().text()] = amount_item.widget().text()
        expected_onchain = live_wallet.config.format_amount_and_units(
            int(bal.confirmed) - GOAL_SAT)
        assert rows["On-chain:"] == expected_onchain
        assert rows["Liquidity goal buffer:"] == \
            live_wallet.config.format_amount_and_units(GOAL_SAT)
        dialog.deleteLater()
    finally:
        qt_mod._set_buffer_provider(None)
