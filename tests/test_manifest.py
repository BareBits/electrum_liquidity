"""Pure test (no Electrum needed) that the plugin manifest advertises the two
entry points the README documents: the Qt GUI tab and the headless cmdline."""
from __future__ import annotations

import json
import os


def _manifest() -> dict:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "inbound_liquidity", "manifest.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_manifest_available_for_qt_and_cmdline():
    assert _manifest().get("available_for") == ["qt", "cmdline"]


def test_manifest_has_name_and_description():
    m = _manifest()
    assert m.get("name") == "inbound_liquidity"
    assert m.get("fullname")
    assert m.get("description")
