"""The real ``LiquidityPlugin.__init__`` / ``start_wallet`` / ``stop_wallet``.

Why this file exists
--------------------
These three were, until it was added, the single largest untested region of the
plugin: 215 statements at **zero** coverage, and not for lack of tests around
them. Every other glue module here builds its plugin with
``object.__new__(LiquidityPlugin)`` and sets the handful of attributes its own
test needs, which is a good trade for a focused test but means the constructor
never runs. The one suite that drives a real Electrum --
``e2e/tests/test_multi_wallet_gui_e2e`` -- builds its daemon with
``start_network=False``, so ``wallet.network is None`` and ``start_wallet``
returns at its second line. The result was that the plugin's entire wiring step
-- registering the event callbacks, lowering Electrum's funding floor,
installing the close hooks, starting the heartbeat, arming the post-grace
evaluation -- was proven only by the opt-in rig suite, i.e. not in CI at all.

The failure mode that motivates the drift guard below is nastier than the
coverage number suggests. Because the other modules hand-set their attributes,
a constructor that *stopped* creating one would break no test here while
breaking every real install: the mocked suite supplies the missing attribute
itself. So this file asserts the constructor's output against the class's own
declared contract rather than against a list written out by hand.

Fakes are minimal but the plugin code is real, and the asyncio loop is real --
``start_wallet`` schedules four separate things onto it.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

import electrum.plugins.inbound_liquidity as IL  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    STATUS_SETTLING,
)


# --- fakes ----------------------------------------------------------------
class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, Any] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value) -> None:
        self._d[key] = value


class _FakeLnworker:
    """Carries the four close entry points ``_install_close_hooks`` wraps.

    Two sync and two async, because the hook installer branches on
    ``iscoroutinefunction`` and both branches should be exercised.
    """

    def __init__(self) -> None:
        self.closed: List[str] = []

    def close_channel(self, chan_id, *a, **kw):
        self.closed.append("close_channel")
        return "closed"

    def force_close_channel(self, chan_id, *a, **kw):
        self.closed.append("force_close_channel")
        return "force-closed"

    async def request_force_close(self, chan_id, *a, **kw):
        self.closed.append("request_force_close")
        return "requested"

    async def schedule_force_closing(self, chan_id, *a, **kw):
        self.closed.append("schedule_force_closing")
        return "scheduled"


class _FakeWallet:
    def __init__(self, loop, *, name: str = "lifecycle-wallet",
                 lightning: bool = True, online: bool = True) -> None:
        self.db = _FakeDB()
        self.lnworker = _FakeLnworker() if lightning else None
        self.network = (SimpleNamespace(asyncio_loop=loop, is_connected=lambda: True)
                        if online else None)
        self.saves = 0
        self._name = name
        self._lightning = lightning

    def has_lightning(self) -> bool:
        return self._lightning

    def basename(self) -> str:
        return self._name

    def save_db(self) -> None:
        self.saves += 1


@pytest.fixture
def loop():
    """A real asyncio loop on its own thread.

    ``start_wallet`` does not await anything -- it fires work at a loop with
    ``run_coroutine_threadsafe`` (the evaluation, the update check, the usage
    report) and ``run_coroutine_threadsafe``/``call_later`` for the heartbeat.
    A loop that is not RUNNING would accept those and silently never run them,
    so the test would prove only that the calls were made.
    """
    out: Dict[str, Any] = {}
    ready = threading.Event()

    def _run() -> None:
        lp = asyncio.new_event_loop()
        asyncio.set_event_loop(lp)
        out["loop"] = lp
        lp.call_soon(ready.set)
        lp.run_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert ready.wait(10), "the test event loop never started"
    lp = out["loop"]
    try:
        yield lp
    finally:
        # Cancel what is still pending before stopping, or asyncio prints
        # "Task was destroyed but it is pending!" over the test output.
        # `_schedule_post_grace_evaluation` is the one that does it: it fires an
        # untracked task that sleeps out the whole startup grace, and `on_close`
        # does not cancel it (it tracks heartbeats and trailing evaluations
        # only). Harmless in production -- the task re-checks `wallet in
        # self.wallets` when it wakes, so after a stop it is a no-op -- but in a
        # test it outlives the loop and makes the log look broken.
        def _cancel_all() -> None:
            for task in asyncio.all_tasks(lp):
                task.cancel()
            lp.call_later(0.05, lp.stop)

        lp.call_soon_threadsafe(_cancel_all)
        thread.join(timeout=10)


@pytest.fixture
def plugin(tmp_path):
    """A plugin built by its REAL constructor, torn down the way Electrum does.

    Teardown matters and is easy to get wrong: ``__init__`` registers two
    process-wide ``util`` callbacks and ``BasePlugin.__init__`` appends this
    instance to the global ``hooks`` table. Leaving either behind would leak
    into every later test in this worker -- a stale callback holding a dead
    plugin, invoked on the next wallet event. ``BasePlugin.close()`` is
    Electrum's own unwind (hooks, then ``parent.close_plugin``, then our
    ``on_close``, which cancels heartbeats and unregisters the callbacks), so
    use that rather than a hand-rolled approximation.
    """
    from electrum.simple_config import SimpleConfig  # type: ignore

    config = SimpleConfig({"electrum_path": str(tmp_path)})
    closed: List[Any] = []
    parent = SimpleNamespace(close_plugin=closed.append)
    p = LiquidityPlugin(parent, config, "inbound_liquidity")
    try:
        yield p
    finally:
        p.close()


# --- the constructor ------------------------------------------------------
def test_every_declared_per_wallet_store_is_usable(plugin) -> None:
    """Each name in ``_PER_WALLET_STATE_ATTRS`` resolves to a per-wallet dict.

    There are deliberately TWO ways that is satisfied, which is why this does
    not simply assert ``hasattr``: most stores are built in ``__init__``, but
    some are created on first use by ``_per_wallet_store`` (``_fault_log_stamps``
    is one). Both are fine for ``_forget_wallet``, which skips anything that is
    not a dict. Asserting the stricter "``__init__`` creates all of them" fails
    on the lazy ones for no good reason -- it did, when this was written.
    """
    for attr in LiquidityPlugin._PER_WALLET_STATE_ATTRS:
        store = plugin._per_wallet_store(attr)
        assert isinstance(store, dict), (
            f"{attr} is declared per-wallet state but resolves to "
            f"{type(store).__name__}")


def test_a_directly_subscripted_store_is_built_by_the_constructor() -> None:
    """The hazard the lazy-store tolerance does NOT cover.

    ``_per_wallet_store(name)`` creates a missing dict, so a store reached only
    that way is safe however it was declared. A store reached as
    ``self._foo[wallet]`` or ``self._foo.setdefault(wallet, ...)`` is not: if
    ``__init__`` does not build it, the first real tick raises
    ``AttributeError`` from inside an evaluation.

    And no mocked test would catch that, which is the whole point of checking
    it here. Every other glue module constructs its plugin with
    ``object.__new__`` and then creates each name in
    ``_PER_WALLET_STATE_ATTRS`` itself, so the attribute is always present in
    those tests no matter what ``__init__`` does -- the suite would stay green
    while every real install broke on load.

    Static, over the shipped source, so it covers paths no test happens to
    execute.
    """
    import inspect  # noqa: PLC0415
    import re  # noqa: PLC0415

    source = inspect.getsource(IL)
    # `self._foo = {}` with an optional type annotation. The annotation is
    # matched as "anything up to the =" rather than as a Dict[...] shape: the
    # real ones nest (`Dict['Abstract_Wallet', Dict[str, Any]]`), and a
    # bracket-counting pattern silently missed all three of those, reporting
    # them as offenders when this was first written.
    built_by_init = set(re.findall(
        r"self\.(_[a-z_0-9]+)\s*(?::[^=\n]+)?=\s*\{\}", source))
    assert len(built_by_init) > 5, (
        f"the constructor-scan pattern matched almost nothing "
        f"({sorted(built_by_init)}); it has stopped tracking the source and "
        f"this test would pass whatever the code did")

    offenders = []
    for attr in LiquidityPlugin._PER_WALLET_STATE_ATTRS:
        if attr in built_by_init:
            continue
        direct = re.search(
            rf"self\.{re.escape(attr)}\s*(\[|\.setdefault\(|\.pop\()", source)
        if direct:
            offenders.append(attr)
    assert not offenders, (
        f"these per-wallet stores are subscripted directly but never built by "
        f"__init__, so a real load would AttributeError: {offenders}. Either "
        f"initialise them in __init__ or reach them via _per_wallet_store().")


def _subscribed(callback) -> set:
    """The events ``callback`` is currently registered for.

    Read out of Electrum's live registry rather than a flag the plugin sets, so
    what is asserted is the real subscription. The registry holds WEAK refs,
    and bound methods are wrapped in ``WeakMethodProper`` (a fresh bound-method
    object is created on every ``plugin._on_wallet_event`` access, so a plain
    ``in`` test against the raw method would never match) -- hence going
    through the manager's own wrapper to build a comparable key.
    """
    mgr = IL.util.callback_mgr
    wcb = mgr._wcb_from_any_callback(callback)
    with mgr.callback_lock:
        return {event for event, cbs in mgr._wcallbacks.items() if wcb in cbs}


def test_the_constructor_registers_the_event_callbacks(plugin) -> None:
    """Event wiring is how the plugin learns that anything happened at all.

    Both handlers and the full event set, because the set is deliberately broad
    -- the plugin subscribes to several events whose argument shapes differ and
    re-evaluates on any of them -- and quietly losing one would show up only as
    a plugin that reacts more slowly than it should.
    """
    wallet_events = _subscribed(plugin._on_wallet_event)
    missing = set(IL._TRIGGER_EVENTS) - wallet_events
    assert not missing, f"not subscribed to wallet events: {sorted(missing)}"

    offer_events = _subscribed(plugin._on_offers_event)
    missing = set(IL._OFFER_TRIGGER_EVENTS) - offer_events
    assert not missing, f"not subscribed to offer events: {sorted(missing)}"


def test_the_constructor_builds_the_log_capture(plugin) -> None:
    """The Log tab's ring buffer and its logging handler, which
    ``apply_log_capture_settings`` then sizes from the open wallets."""
    assert plugin.log_buffer is not None
    assert plugin.log_capture is not None
    assert plugin.get_log_lines() is not None


def test_closing_the_plugin_unregisters_its_callbacks(tmp_path) -> None:
    """A disabled plugin must leave no live callback behind.

    Built and closed inside the test rather than via the fixture, because the
    assertion is about the state AFTER close and the fixture closes on the way
    out.
    """
    from electrum.simple_config import SimpleConfig  # type: ignore

    p = LiquidityPlugin(SimpleNamespace(close_plugin=lambda _p: None),
                        SimpleConfig({"electrum_path": str(tmp_path)}),
                        "inbound_liquidity")
    assert _subscribed(p._on_wallet_event), "never subscribed in the first place"
    p.close()
    assert not _subscribed(p._on_wallet_event), \
        "a closed plugin is still subscribed to wallet events"
    assert not _subscribed(p._on_offers_event), \
        "a closed plugin is still subscribed to offer events"


# --- start_wallet ---------------------------------------------------------
def test_start_wallet_wires_the_wallet_up(plugin, loop) -> None:
    """The happy path, end to end through the real method."""
    wallet = _FakeWallet(loop)
    # Record the status transitions instead of reading the status afterwards.
    # `start_wallet` publishes SETTLING and then schedules an evaluation at the
    # loop, and that evaluation moves the status on -- with shipped defaults to
    # "automation disabled". So a single read after the call is a race with
    # work `start_wallet` itself kicked off, and it loses it often enough to be
    # useless. The ORDER is the real property anyway: settling first, before
    # anything is evaluated.
    seen: List[str] = []
    plugin.on_status_changed = lambda w, status: seen.append(status)

    plugin.start_wallet(wallet)

    assert wallet in plugin.wallets, "wallet is not being managed"
    assert isinstance(plugin.wallets[wallet], asyncio.Lock), \
        "the per-wallet re-entrancy lock was not created"
    assert wallet in plugin._started_at, "the startup-grace clock was not stamped"
    assert plugin._started_at[wallet] <= time.time()
    assert wallet in plugin._peer_seen_online, "peer-seen set not seeded"
    assert seen and seen[0] == STATUS_SETTLING, (
        f"a freshly loaded wallet must publish the settling status first; "
        f"got {seen!r}")
    # The close hooks are installed on the lnworker itself, and marked so a
    # re-managed wallet does not stack a second layer of wrappers.
    assert getattr(wallet.lnworker, "_inbound_liquidity_close_hooked", False), \
        "close hooks were not installed on the lnworker"
    # And the heartbeat is running, which is what advances the time-based
    # watchdogs on a wallet that is not emitting events.
    assert wallet in plugin._heartbeat_tasks, "no heartbeat was started"


def test_start_wallet_declines_a_wallet_with_no_lightning(plugin, loop) -> None:
    """Nothing to manage, and nothing may be wired up -- a plugin that
    registered a heartbeat for a non-Lightning wallet would tick forever
    against a wallet it can never act on."""
    wallet = _FakeWallet(loop, lightning=False)
    plugin.start_wallet(wallet)
    assert wallet not in plugin.wallets
    assert wallet not in plugin._heartbeat_tasks
    assert wallet not in plugin._started_at


def test_start_wallet_declines_an_offline_wallet(plugin) -> None:
    """Offline mode (``wallet.network is None``). This is the branch the
    multi-wallet GUI e2e takes -- it builds its daemon with
    ``start_network=False`` -- which is precisely why that suite could not
    cover anything below this line and this file had to exist."""
    wallet = _FakeWallet(None, online=False)
    plugin.start_wallet(wallet)
    assert wallet not in plugin.wallets
    assert wallet not in plugin._heartbeat_tasks


def test_the_close_hooks_record_a_local_close_on_every_path(plugin, loop) -> None:
    """A close WE initiate must be attributed to us, not to the peer.

    All four lnworker entry points are wrapped, two sync and two async, and the
    channel-health watchdog reads ``_local_closes`` to exempt them from the
    "closed by peer" hard fault. Exercised through the wrappers the real
    ``_install_close_hooks`` installed.
    """
    wallet = _FakeWallet(loop)
    plugin.start_wallet(wallet)
    lnworker = wallet.lnworker

    assert lnworker.close_channel(bytes.fromhex("aabb")) == "closed"
    assert lnworker.force_close_channel("ccdd") == "force-closed"
    asyncio.run_coroutine_threadsafe(
        lnworker.request_force_close(bytes.fromhex("eeff")), loop).result(10)
    asyncio.run_coroutine_threadsafe(
        lnworker.schedule_force_closing("1122"), loop).result(10)

    recorded = plugin._local_closes.get(wallet, set())
    assert {"aabb", "ccdd", "eeff", "1122"} <= recorded, (
        f"not every close path was attributed to us: {recorded}")
    # The wrappers still delegate -- a hook that swallowed the close would stop
    # the channel ever closing.
    assert lnworker.closed == ["close_channel", "force_close_channel",
                               "request_force_close", "schedule_force_closing"]


def test_installing_the_close_hooks_twice_does_not_stack_wrappers(plugin, loop) -> None:
    """A wallet can be stopped and started again (close and reopen), and the
    hook installer is idempotent per lnworker so the second pass does not wrap
    the wrappers -- which would record each close twice and, worse, grow a
    layer per reload."""
    wallet = _FakeWallet(loop)
    plugin.start_wallet(wallet)
    first = wallet.lnworker.close_channel
    plugin._install_close_hooks(wallet, wallet.lnworker)
    assert wallet.lnworker.close_channel is first, "the hook was wrapped twice"


# --- stop_wallet ----------------------------------------------------------
def test_stop_wallet_leaves_no_trace_of_the_wallet(plugin, loop) -> None:
    """The leak guard, written so it does not need maintaining.

    Rather than listing the stores to check -- which is the list
    ``_forget_wallet`` already walks, so checking against it would only prove
    the code agrees with itself -- this scans EVERY dict attribute on the
    plugin for the wallet as a key. A new per-wallet dict added to ``__init__``
    and forgotten in ``_PER_WALLET_STATE_ATTRS`` therefore fails here
    immediately, which is the mistake worth catching: the wallet's state would
    otherwise survive a close/reopen and the reopened wallet would inherit a
    stale cooldown, or a wedged ``_dev_fee_paying`` guard that silently stops
    it ever paying out again.
    """
    wallet = _FakeWallet(loop)
    plugin.start_wallet(wallet)
    assert wallet in plugin.wallets

    plugin.stop_wallet(wallet)

    assert wallet not in plugin.wallets, "the wallet is still being managed"
    leaked = sorted(
        name for name, value in vars(plugin).items()
        if isinstance(value, dict) and wallet in value)
    assert not leaked, (
        f"stop_wallet left the wallet in {leaked}. If one of these is new, add "
        f"it to LiquidityPlugin._PER_WALLET_STATE_ATTRS so _forget_wallet "
        f"clears it; otherwise a reopened wallet inherits its stale state.")


def test_a_restarted_wallet_starts_clean(plugin, loop) -> None:
    """Stop then start the same wallet object, the way closing and reopening a
    wallet in one Electrum session does. The second start must manage it again
    from a clean slate -- `_forget_wallet` deliberately leaves the `_stopping`
    flag set, and `start_wallet` is the only thing that retracts it."""
    wallet = _FakeWallet(loop)
    plugin.start_wallet(wallet)
    plugin.stop_wallet(wallet)
    assert plugin._shutting_down(wallet), \
        "the stopping flag should survive _forget_wallet"

    plugin.start_wallet(wallet)
    assert wallet in plugin.wallets
    assert not plugin._shutting_down(wallet), (
        "a restarted wallet is still flagged as shutting down; its next "
        "evaluation would abort between providers")
