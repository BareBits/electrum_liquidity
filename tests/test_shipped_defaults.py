"""Regression tests pinning the plugin's *shipped* ConfigVar defaults.

These are the values a fresh install runs with before the user touches the
Settings tab, so they are user-visible behaviour, not incidental constants: they
decide when an untouched install will spend on-chain funds to open a channel and
what all-in swap cost it will accept. Changing one should be a deliberate act
that updates this test (and the README table) alongside it.

Skipped if the plugin package (and Electrum) can't be imported (e.g. running
outside the electrum venv)."""
from __future__ import annotations

import json
import pathlib

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.simple_config import SimpleConfig  # type: ignore  # noqa: E402

# The two funds-affecting gates, and the shipped values documented in README.md.
EXPECTED_MIN_ONCHAIN_TO_OPEN_SAT: int = 60_000
EXPECTED_MAX_SWAP_FEE_PCT: float = 0.9


def test_min_onchain_to_open_sat_default() -> None:
    default = SimpleConfig.INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT.get_default_value()
    assert default == EXPECTED_MIN_ONCHAIN_TO_OPEN_SAT
    assert isinstance(default, int)


def test_max_swap_fee_pct_default() -> None:
    default = SimpleConfig.INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT.get_default_value()
    assert default == pytest.approx(EXPECTED_MAX_SWAP_FEE_PCT)
    assert isinstance(default, float)


def test_default_open_floor_is_below_electrums_stock_funding_floor() -> None:
    """The default is only useful if it actually engages the floor override --
    at or above Electrum's stock MIN_FUNDING_SAT the plugin would leave the
    stock floor in place and no small channel could ever be opened."""
    from electrum.lnutil import MIN_FUNDING_SAT  # type: ignore

    assert EXPECTED_MIN_ONCHAIN_TO_OPEN_SAT < MIN_FUNDING_SAT


def test_readme_documents_the_shipped_defaults() -> None:
    """The README settings table is the user-facing contract for these two; keep
    it from drifting away from the code."""
    readme = (pathlib.Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")

    for line in readme.splitlines():
        if line.startswith("| `min_onchain_to_open_sat`"):
            assert line.rstrip().endswith("| `60_000` |"), line
            break
    else:
        pytest.fail("no `min_onchain_to_open_sat` row in the README settings table")

    for line in readme.splitlines():
        if line.startswith("| `max_swap_fee_pct`"):
            assert line.rstrip().endswith("| `0.9` |"), line
            break
    else:
        pytest.fail("no `max_swap_fee_pct` row in the README settings table")


def test_manifest_version_is_wellformed() -> None:
    """The release workflow reads this value to name the zip and the tag."""
    manifest = json.loads(
        (pathlib.Path(__file__).resolve().parent.parent / "inbound_liquidity" / "manifest.json")
        .read_text(encoding="utf-8"))
    parts = manifest["version"].split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), manifest["version"]
