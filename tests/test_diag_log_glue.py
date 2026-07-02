"""Glue tests: the plugin mirrors decision-log entries (and file-only
operational events) to the on-disk diagnostic log, gated by the opt-in toggle.

Imports the real plugin package (needs the Electrum venv); skipped otherwise.
The plugin instance is built WITHOUT BasePlugin.__init__ (no network/parent),
wiring only the attributes the log helpers touch — same approach as
test_decision_log.py.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List

import pytest

inbound_liquidity = pytest.importorskip("electrum.plugins.inbound_liquidity")
from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    LOG_DB_KEY,
    DIAG_LOG_DIRNAME,
)


class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _FakeWallet:
    def __init__(self, name: str = "testwallet") -> None:
        self.db = _FakeDB()
        self._name = name

    def basename(self) -> str:
        return self._name

    def save_db(self) -> None:
        pass


class _FakeConfig:
    def __init__(self, path: str, enabled: bool) -> None:
        self.path = path
        self.INBOUND_LIQUIDITY_DIAG_LOG_ENABLED = enabled
        self.INBOUND_LIQUIDITY_LOG_RETENTION_DAYS = 30


def _make_plugin(path: str, enabled: bool) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.config = _FakeConfig(path, enabled)
    p.logger = logging.getLogger("test.inbound_liquidity.diag")
    p._last_decline_sigs = {}
    return p


def _log_dir(tmp_path, wallet_name: str = "testwallet"):
    return tmp_path / DIAG_LOG_DIRNAME / wallet_name


def _lines(tmp_path, wallet_name: str = "testwallet") -> List[Dict]:
    folder = _log_dir(tmp_path, wallet_name)
    out: List[Dict] = []
    for f in sorted(folder.glob("inbound_liquidity-*.log")):
        for raw in f.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                out.append(json.loads(raw))
    return out


# --- toggle gating --------------------------------------------------------
def test_disabled_toggle_writes_no_file(tmp_path) -> None:
    p = _make_plugin(str(tmp_path), enabled=False)
    w = _FakeWallet()
    p._log_action(w, kind="open", amount_sat=1, source="on-chain", dest="x",
                  reason="opened", detail=None, state={})
    p._diag_event(w, category="error", kind="evaluation", reason="boom")
    # Decision log still lands in wallet.db, but no diagnostic dir is created.
    assert w.db.get(LOG_DB_KEY)
    assert not (tmp_path / DIAG_LOG_DIRNAME).exists()


def test_enabled_toggle_mirrors_decision_entries(tmp_path) -> None:
    p = _make_plugin(str(tmp_path), enabled=True)
    w = _FakeWallet()
    p._log_action(w, kind="open", amount_sat=1_990_000, source="on-chain",
                  dest="peerpub", reason="cold wallet", detail="txid abc", state={})
    p._log_decline(
        w,
        _DummyDecline(kind="swap", channel_id="aa", short_id="1x1x1", reason="cost too high"),
        state={})
    entries = _lines(tmp_path)
    assert [e["category"] for e in entries] == ["action", "decline"]
    assert entries[0]["kind"] == "open" and entries[0]["reason"] == "cold wallet"
    assert entries[1]["category"] == "decline" and entries[1]["reason"] == "cost too high"
    # Each line carries the readable UTC time the file logger injects.
    assert all("time" in e for e in entries)


def test_operational_event_is_file_only(tmp_path) -> None:
    p = _make_plugin(str(tmp_path), enabled=True)
    w = _FakeWallet()
    p._diag_event(w, category="error", kind="swap",
                  reason="provider declined reverse swap", source="npub1xyz",
                  detail="25000 sat: uneconomical")
    # Not written to the wallet-db decision log (GUI tabs unchanged)...
    assert not w.db.get(LOG_DB_KEY)
    # ...but present in the diagnostic file.
    entries = _lines(tmp_path)
    assert len(entries) == 1
    assert entries[0]["category"] == "error" and entries[0]["kind"] == "swap"


# --- sensitive-data safety ------------------------------------------------
def test_file_contains_no_key_material_and_abbreviates_ids(tmp_path) -> None:
    p = _make_plugin(str(tmp_path), enabled=True)
    w = _FakeWallet()
    node = "02b2a9bbdd7513a559deb22666afd04a4c80fe400eeda34dd7ba53d3e027a06501"
    p._log_action(w, kind="open", amount_sat=1, source="on-chain", dest=node,
                  reason="opened", detail=None, state={})
    raw_text = "".join(
        f.read_text(encoding="utf-8")
        for f in _log_dir(tmp_path).glob("inbound_liquidity-*.log"))
    # The full 64-hex node id must NOT appear verbatim (it is abbreviated, same
    # as the GUI log); and no key-like material is present.
    assert node not in raw_text
    assert "02b2a9…6501" in raw_text
    for needle in ("xprv", "xpub", "seed", "privkey", "private_key"):
        assert needle not in raw_text.lower()


class _DummyDecline:
    """Stand-in for liquidity_manager.DeclineRecord (only fields the log reads)."""
    def __init__(self, *, kind, reason, channel_id=None, short_id=None,
                 amount_sat=None) -> None:
        self.kind = kind
        self.reason = reason
        self.channel_id = channel_id
        self.short_id = short_id
        self.amount_sat = amount_sat


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
