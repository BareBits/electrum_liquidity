"""Tests for the "Managed by" column the plugin adds to Electrum's Channels tab.

The column is grafted onto Electrum's ``ChannelsList`` by extending its 0-based
column enum (see the monkeypatch in ``qt.py``). These tests cover the risky enum
surgery (values stay 0-based/contiguous so the widget's index math survives) and
the plugin/manual label logic. Skipped outside the Electrum Qt venv.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("electrum.gui.qt.channels_list")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    PLUGIN_OPENED_CHANNELS_DB_KEY,
)
from electrum.plugins.inbound_liquidity.qt import (  # type: ignore  # noqa: E402
    _managed_label_for_channel,
    _patch_channels_list_managed_column,
)


def test_patch_adds_managed_column_and_is_idempotent() -> None:
    assert _patch_channels_list_managed_column() is True
    from electrum.gui.qt.channels_list import ChannelsList

    assert hasattr(ChannelsList.Columns, "MANAGED")
    # Values must stay 0-based and contiguous, or `items[self.Columns.X]` breaks.
    values = sorted(m.value for m in ChannelsList.Columns)
    assert values == list(range(len(values)))
    # MANAGED is the last (highest) column and has a header.
    assert ChannelsList.Columns.MANAGED.value == max(values)
    assert ChannelsList.Columns.MANAGED in ChannelsList.headers
    # Every pre-existing member kept its name (so all references still resolve).
    for name in ("FEATURES", "SHORT_CHANID", "NODE_ALIAS", "CAPACITY",
                 "LOCAL_BALANCE", "REMOTE_BALANCE", "CHANNEL_STATUS", "LONG_CHANID"):
        assert hasattr(ChannelsList.Columns, name)
    # Second call is a no-op success.
    assert _patch_channels_list_managed_column() is True


class _FakeDB:
    def __init__(self, data=None) -> None:
        self._d = dict(data or {})

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


def test_managed_label_reflects_plugin_opened_tag() -> None:
    cid = b"\x01" * 32
    chan = SimpleNamespace(channel_id=cid)
    db = _FakeDB()
    wallet = SimpleNamespace(db=db)

    # Untagged -> Manual.
    assert _managed_label_for_channel(wallet, chan) == _label("Manual")
    # Tagged as plugin-opened -> Plugin.
    db.put(PLUGIN_OPENED_CHANNELS_DB_KEY, [cid.hex()])
    assert _managed_label_for_channel(wallet, chan) == _label("Plugin")


def test_managed_label_defensive_on_bad_wallet() -> None:
    # A wallet whose db access raises must not crash the column render.
    class _Boom:
        @property
        def db(self):
            raise RuntimeError("no db")

    chan = SimpleNamespace(channel_id=b"\x02" * 32)
    assert _managed_label_for_channel(_Boom(), chan) == ""


def _label(text: str) -> str:
    from electrum.i18n import _
    return _(text)
