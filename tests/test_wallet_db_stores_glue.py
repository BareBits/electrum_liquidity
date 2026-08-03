"""Glue-level tests for the plugin's per-wallet stores against a REAL Electrum
``JsonDB`` -- the one collaborator every other glue test fakes, and the reason a
crash reached production with 593 green tests behind it.

The fake db used elsewhere (``{}`` + ``get``/``put``) hands back exactly what it
was given. The real one does not: ``JsonDB.put`` deep-copies into a ``StoredDict``
that re-wraps nested dicts as more ``StoredDict``s, each holding ``_db`` -> the
``JsonDB`` -> a ``threading.RLock``. Reading a store back therefore yields live db
objects, and writing one back made ``put``'s deepcopy raise
``TypeError: cannot pickle '_thread.RLock' object``. In the field that surfaced
inside ``_open_channel``: the *second* peer fault of a run (or the first after a
restart, once the store was on disk) aborted the candidate loop and the whole
evaluation with it, so 8 of 10 partners were never tried.

So these tests exercise the store helpers through the real thing:

  * every load/save pair round-trips *twice* -- the second write is where the
    old code died -- and again across a simulated restart;
  * ``_plain_json_copy`` hands back plain containers with no db back-references;
  * a save helper whose write fails logs and returns instead of raising, so
    bookkeeping can never sink the action it is bookkeeping for.

``JsonDB`` rather than ``WalletDB``: the ``StoredDict`` conversion under test
lives in ``JsonDB`` (``WalletDB`` inherits it unchanged apart from keystore keys),
and it constructs from a bare JSON string without a wallet file. Skipped outside
the Electrum venv.

One caveat on *which* Electrum is underneath, because it changes what a fix has
to be proof against. On 4.7.2 (this checkout) ``JsonDB.put`` skips the deepcopy
when the value compares equal to what is stored, so re-writing a store whose
contents are unchanged quietly survived -- a mutation of a live ``StoredDict``
had already written itself through. The crash needed the write to *change*
something: faulting a second, different peer. The build this was reported from
(``forked_electrum``, older) deep-copies unconditionally, so every re-write
crashed. Both shapes are covered here -- the real ``JsonDB`` for the version we
ship against, and ``_UnconditionalDeepCopyDB`` for the stricter one -- because
the plugin must not depend on which of the two it is loaded into.
"""
from __future__ import annotations

import copy
import json
import logging
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Tuple

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.json_db import JsonDB, StoredDict  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    CHANNEL_UPTIME_DB_KEY,
    LOG_DB_KEY,
    PEER_RELIABILITY_DB_KEY,
    PENDING_SWAPS_DB_KEY,
    RELIABILITY_DB_KEY,
    LiquidityPlugin,
    _plain_json_copy,
)

NODE_A = "02" + "aa" * 32
NODE_B = "03" + "bb" * 32
NPUB_A = "npub1" + "a" * 20


class _JsonDbWallet:
    """A wallet whose ``db`` is a real ``JsonDB``, restartable via ``reopen()``."""

    def __init__(self, dump: str = "{}") -> None:
        self.db = JsonDB(dump)
        self.saved = 0
        self.network = SimpleNamespace(is_connected=lambda: True)

    def save_db(self) -> None:
        self.saved += 1

    def basename(self) -> str:
        return "test_wallet"

    def reopen(self) -> "_JsonDbWallet":
        """A fresh wallet holding the same persisted bytes -- i.e. a restart.
        The store is now db-backed from the first read, which is what made the
        very first fault of the 11:55 run crash where 11:54's first one passed."""
        return _JsonDbWallet(self.db.dump())


def _plugin(**config_overrides: Any) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.db_stores")
    p._local_closes = {}
    p._wedged_faulted = {}
    p._known_chan_states = {}
    p._last_decline_sigs = {}
    p._started_at = {}
    p._peer_seen_online = {}
    p._startup_grace_sec = 0.0
    cfg = dict(
        INBOUND_LIQUIDITY_BANNED_PARTNERS="",
        INBOUND_LIQUIDITY_PEER_RELIABILITY_ENABLED=True,
        INBOUND_LIQUIDITY_PEER_AUTOBAN_FAULTS=99,   # not the subject here
        INBOUND_LIQUIDITY_RELIABILITY_ENABLED=True,
        INBOUND_LIQUIDITY_LOG_RETENTION_DAYS=30,
    )
    cfg.update(config_overrides)
    p.config = SimpleNamespace(**cfg)
    return p


# --- the field failure: repeated peer faults ------------------------------
def test_repeated_peer_faults_survive_a_real_wallet_db() -> None:
    """Three faults in one run. The old code crashed on the second."""
    p, w = _plugin(), _JsonDbWallet()

    p._record_peer_fault(w, NODE_A, "channel open failed: TimeoutError", hard=True)
    p._record_peer_fault(w, NODE_B, "connect failed: ConnectionError", hard=False)
    p._record_peer_fault(w, NODE_A, "channel open failed: GracefulDisconnect", hard=True)

    data = p._load_peer_reliability(w)
    assert data[NODE_A.lower()]["consecutive_faults"] == 2
    assert data[NODE_A.lower()]["hard_fault_count"] == 2
    assert data[NODE_B.lower()]["consecutive_faults"] == 1
    assert data[NODE_B.lower()].get("hard_fault_count", 0) == 0
    # ...and what landed in the db is data, not db objects wearing data's shape.
    assert json.loads(w.db.dump())[PEER_RELIABILITY_DB_KEY][NODE_A.lower()][
        "last_reason"] == "channel open failed: GracefulDisconnect"


def test_peer_fault_survives_a_restart() -> None:
    """The 11:55 case: a store already on disk, faulted again after a restart."""
    p, w = _plugin(), _JsonDbWallet()
    p._record_peer_fault(w, NODE_A, "open failed", hard=True)

    w2 = w.reopen()
    assert p._load_peer_reliability(w2)[NODE_A.lower()]["hard_fault_count"] == 1
    p._record_peer_fault(w2, NODE_A, "open failed again", hard=True)

    s = p._load_peer_reliability(w2)[NODE_A.lower()]
    assert s["hard_fault_count"] == 2 and s["consecutive_faults"] == 2


def test_peer_success_after_a_persisted_fault() -> None:
    # The other half of the recorder pair, on the same db-backed store: a
    # success write follows a fault write, so it hit the identical crash.
    p, w = _plugin(), _JsonDbWallet()
    p._record_peer_fault(w, NODE_A, "open failed", hard=True)
    p._record_peer_success(w, NODE_A)

    s = p._load_peer_reliability(w)[NODE_A.lower()]
    assert s["consecutive_faults"] == 0 and s["success_count"] == 1
    assert s["hard_fault_count"] == 1        # the ban tally is not cleared


class _UnconditionalDeepCopyDB:
    """A db backed by a real ``StoredDict`` store, with the ``put`` semantics of
    the Electrum build this was reported from: deepcopy on every write, with no
    equality short-circuit to hide behind. Anything db-backed that reaches
    ``put`` raises here, which is precisely the property the fix must hold under."""

    def __init__(self) -> None:
        self._db = JsonDB("{}")

    def get(self, key: str, default: Any = None) -> Any:
        return self._db.get(key, default)

    def put(self, key: str, value: Any) -> None:
        self._db.data[key] = copy.deepcopy(value)


def test_peer_faults_survive_the_stricter_put_semantics() -> None:
    p = _plugin()
    w = SimpleNamespace(db=_UnconditionalDeepCopyDB(), save_db=lambda: None,
                        basename=lambda: "w", network=SimpleNamespace(is_connected=lambda: True))

    p._record_peer_fault(w, NODE_A, "open failed", hard=True)
    p._record_peer_fault(w, NODE_A, "open failed", hard=True)   # same peer again
    p._record_peer_fault(w, NODE_B, "connect failed", hard=False)
    p._record_peer_success(w, NODE_A)

    data = p._load_peer_reliability(w)
    assert data[NODE_A.lower()]["hard_fault_count"] == 2
    assert data[NODE_A.lower()]["consecutive_faults"] == 0      # cleared by success
    assert data[NODE_B.lower()]["consecutive_faults"] == 1


# --- every store, twice ---------------------------------------------------
# (load, save, first payload, second payload). Each store's values are nested
# containers -- which is the whole point: a flat store never had the bug.
_STORES: List[Tuple[str, Callable, Callable, Dict, Dict]] = [
    (
        RELIABILITY_DB_KEY,
        lambda p, w: p._load_reliability(w),
        lambda p, w, d: p._save_reliability(w, d),
        {NPUB_A: {"consecutive_faults": 1, "last_reason": "no response"}},
        {NPUB_A: {"consecutive_faults": 2, "last_reason": "stuck swap"}},
    ),
    (
        PENDING_SWAPS_DB_KEY,
        lambda p, w: p._load_pending_swaps(w),
        lambda p, w, d: p._save_pending_swaps(w, d),
        {"ab" * 32: {"npub": NPUB_A, "started_ts": 1.0, "node_id": NODE_A}},
        {"ab" * 32: {"npub": NPUB_A, "started_ts": 1.0, "node_id": NODE_B}},
    ),
    (
        CHANNEL_UPTIME_DB_KEY,
        lambda p, w: p._load_json_dict(w, CHANNEL_UPTIME_DB_KEY),
        lambda p, w, d: p._save_json(w, CHANNEL_UPTIME_DB_KEY, d),
        {"cc" * 32: {"samples": [[1.0, True]], "last_ts": 1.0}},
        {"cc" * 32: {"samples": [[1.0, True], [2.0, False]], "last_ts": 2.0}},
    ),
]


@pytest.mark.parametrize("key,load,save,first,second",
                         _STORES, ids=[s[0] for s in _STORES])
def test_store_round_trips_twice_and_across_a_restart(
        key: str, load: Callable, save: Callable, first: Dict, second: Dict) -> None:
    p, w = _plugin(), _JsonDbWallet()

    save(p, w, first)
    assert load(p, w) == first

    # The write that used to raise: read the db-backed store back, mutate, write.
    data = load(p, w)
    data.update(second)
    save(p, w, data)
    assert load(p, w) == second

    # And once more from disk, where the store is db-backed from the first read.
    w2 = w.reopen()
    data = load(p, w2)
    assert data == second
    data["extra"] = {"nested": {"deep": 1}}
    save(p, w2, data)
    assert load(p, w2)["extra"] == {"nested": {"deep": 1}}


def test_decision_log_appends_repeatedly_and_across_a_restart() -> None:
    p, w = _plugin(), _JsonDbWallet()
    p._log_fault(w, kind="peer", ident=NODE_A, reason="open failed", hard=True)
    p._log_fault(w, kind="peer", ident=NODE_B, reason="connect failed", hard=False)
    assert len(p._load_log(w)) == 2

    w2 = w.reopen()
    p._log_fault(w2, kind="provider", ident=NPUB_A, reason="no response", hard=False)
    entries = p._load_log(w2)
    assert len(entries) == 3
    assert all(isinstance(e, dict) and not isinstance(e, StoredDict) for e in entries)
    assert json.loads(w2.db.dump())[LOG_DB_KEY][-1]["reason"].endswith("no response")


# --- the helper itself ----------------------------------------------------
def test_plain_json_copy_strips_db_backed_containers() -> None:
    db = JsonDB("{}")
    db.put("k", {"peer": {"faults": 1, "history": [{"ts": 1.0}]}})
    raw = db.get("k", {})
    assert isinstance(raw, StoredDict)                  # what the db really hands back
    with pytest.raises(TypeError):                      # ...and why it cannot be re-put
        copy.deepcopy({"outer": raw["peer"]})

    plain = _plain_json_copy(raw)
    assert plain == {"peer": {"faults": 1, "history": [{"ts": 1.0}]}}
    assert type(plain) is dict and type(plain["peer"]) is dict
    assert type(plain["peer"]["history"]) is list
    assert type(plain["peer"]["history"][0]) is dict
    copy.deepcopy(plain)                                # the operation that raised


def test_plain_json_copy_leaves_scalars_and_normalises_tuples() -> None:
    assert _plain_json_copy(5) == 5
    assert _plain_json_copy("x") == "x"
    assert _plain_json_copy(None) is None
    # Tuples become lists -- exactly what a wallet-file round-trip would do, so
    # the in-memory value matches the persisted one.
    out = _plain_json_copy({"a": (1.0, 2.0)})
    assert out == {"a": [1.0, 2.0]} and type(out["a"]) is list


# --- a failing write must not escape --------------------------------------
class _ExplodingDB:
    def get(self, key, default=None):
        return default

    def put(self, key, value):
        raise TypeError("cannot pickle '_thread.RLock' object")


def _exploding_wallet() -> SimpleNamespace:
    return SimpleNamespace(db=_ExplodingDB(), save_db=lambda: None,
                           basename=lambda: "w")


@pytest.mark.parametrize("save", [
    lambda p, w: p._save_peer_reliability(w, {NODE_A: {"consecutive_faults": 1}}),
    lambda p, w: p._save_reliability(w, {NPUB_A: {"consecutive_faults": 1}}),
    lambda p, w: p._save_pending_swaps(w, {"ab" * 32: {"npub": NPUB_A}}),
    lambda p, w: p._save_json(w, CHANNEL_UPTIME_DB_KEY, {"cc" * 32: {"last_ts": 1.0}}),
    lambda p, w: p._save_action_timestamps(w, {"open": [1.0]}),
    lambda p, w: p._save_dev_fee_owed(w, 100),
    lambda p, w: p._save_dev_fee_payments(w, [[1.0, 100.0]]),
], ids=["peer_reliability", "provider_reliability", "pending_swaps",
        "json_dict", "action_timestamps", "dev_fee_owed", "dev_fee_payments"])
def test_save_helpers_swallow_a_failing_put(save: Callable,
                                            caplog: pytest.LogCaptureFixture) -> None:
    p, w = _plugin(), _exploding_wallet()
    with caplog.at_level(logging.ERROR):
        save(p, w)                       # must not raise into the caller's action
    assert any("could not persist" in r.message for r in caplog.records)


def test_append_log_swallows_a_failing_put(caplog: pytest.LogCaptureFixture) -> None:
    p, w = _plugin(), _exploding_wallet()
    p._diag_write = lambda wallet, entry: None
    with caplog.at_level(logging.ERROR):
        p._log_fault(w, kind="peer", ident=NODE_A, reason="open failed", hard=True)
    assert any("could not persist decision log" in r.message for r in caplog.records)
