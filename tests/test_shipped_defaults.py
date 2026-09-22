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


# The liquidity goal ships ACTIVE rather than at 0/off, so it is funds-affecting
# on upgrade as well as on a fresh install: an existing wallet that is at its
# channel ceiling with a drained, sub-goal, plugin-opened channel will have that
# channel replaced. Pinned here so changing it has to be deliberate.
EXPECTED_LIQUIDITY_GOAL_SAT: int = 100_000


def test_liquidity_sink_ships_off() -> None:
    """The sink is opt-in. It sends a channel's outbound to a third party rather
    than converting it to this wallet's own on-chain coins, so an untouched
    install must never do it, and "disable submarine swaps" must never be on --
    together they would stop the plugin draining anything at all."""
    address = SimpleConfig.INBOUND_LIQUIDITY_SINK_ADDRESS.get_default_value()
    assert address == ""
    assert isinstance(address, str)
    disabled = SimpleConfig.INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS.get_default_value()
    assert disabled is False


def test_the_sink_ships_held_back_until_the_goal_is_met() -> None:
    """``defer_sink_until_goal`` ships ON, and unlike the settings above it is
    funds-affecting on UPGRADE too: an existing wallet with a sink configured
    stops paying it until the build-out reaches the liquidity goal, swapping
    instead. That is the conservative direction (the drained balance comes back
    to this wallet as on-chain coins rather than leaving it), which is why it is
    the default -- but it changes behaviour, so it is pinned here."""
    default = SimpleConfig.INBOUND_LIQUIDITY_DEFER_SINK_UNTIL_GOAL.get_default_value()
    assert default is True


def test_the_sink_gate_is_inert_on_a_fresh_install() -> None:
    # It only qualifies a sink, and the sink itself ships off -- so a fresh
    # install swaps exactly as it did before the feature existed.
    assert SimpleConfig.INBOUND_LIQUIDITY_SINK_ADDRESS.get_default_value() == ""


def test_liquidity_goal_default() -> None:
    default = SimpleConfig.INBOUND_LIQUIDITY_GOAL_SAT.get_default_value()
    assert default == EXPECTED_LIQUIDITY_GOAL_SAT
    assert isinstance(default, int)


def test_liquidity_goal_ships_enabled() -> None:
    # 0 would disable the rule entirely; the shipped value must not be that.
    assert SimpleConfig.INBOUND_LIQUIDITY_GOAL_SAT.get_default_value() > 0


def test_liquidity_goal_is_reachable_from_the_default_open_floor() -> None:
    """A goal the plugin could never fund would silently do nothing. The
    replacement rule needs on-chain funds of at least max(goal, funding floor)
    plus the reserve, so pin that the shipped goal is above the shipped open
    floor -- i.e. replacing a channel really does aim at a BIGGER one."""
    goal = SimpleConfig.INBOUND_LIQUIDITY_GOAL_SAT.get_default_value()
    assert goal > EXPECTED_MIN_ONCHAIN_TO_OPEN_SAT


def test_default_open_floor_is_below_electrums_stock_funding_floor() -> None:
    """The default is only useful if it actually engages the floor override --
    at or above Electrum's stock MIN_FUNDING_SAT the plugin would leave the
    stock floor in place and no small channel could ever be opened."""
    from electrum.lnutil import MIN_FUNDING_SAT  # type: ignore

    assert EXPECTED_MIN_ONCHAIN_TO_OPEN_SAT < MIN_FUNDING_SAT


# The gates on force-closing a wedged channel open. These are funds-affecting in
# the same way as the two above: together they decide how long an untouched
# install waits before spending a mining fee on an irreversible force-close.
EXPECTED_STUCK_OPEN_CLOSE_TIMEOUT_MIN: int = 360      # 6 hours
EXPECTED_WEDGE_REQUIRED_CHECKS: int = 5
EXPECTED_WEDGE_CHECK_SPREAD_MIN: int = 60
EXPECTED_COOP_BEFORE_FORCE_MIN: int = 10
EXPECTED_CONFIRMING_FREEZE_DAYS: float = 3.0


def test_wedged_open_close_gate_defaults() -> None:
    for attr, expected in (
        ("INBOUND_LIQUIDITY_STUCK_OPEN_CLOSE_TIMEOUT_MIN",
         EXPECTED_STUCK_OPEN_CLOSE_TIMEOUT_MIN),
        ("INBOUND_LIQUIDITY_WEDGE_REQUIRED_CHECKS", EXPECTED_WEDGE_REQUIRED_CHECKS),
        ("INBOUND_LIQUIDITY_WEDGE_CHECK_SPREAD_MIN", EXPECTED_WEDGE_CHECK_SPREAD_MIN),
        ("INBOUND_LIQUIDITY_COOP_BEFORE_FORCE_MIN", EXPECTED_COOP_BEFORE_FORCE_MIN),
    ):
        default = getattr(SimpleConfig, attr).get_default_value()
        assert default == expected, attr
        assert isinstance(default, int), attr


def test_confirming_freeze_days_default() -> None:
    default = SimpleConfig.INBOUND_LIQUIDITY_CONFIRMING_FREEZE_DAYS.get_default_value()
    assert default == pytest.approx(EXPECTED_CONFIRMING_FREEZE_DAYS)
    assert isinstance(default, float)


def test_force_close_timeout_is_well_above_the_freeze_escape() -> None:
    """The two clocks were once a single setting, which is what made raising the
    safety margin on the irreversible action also stall the plugin for hours.
    Un-freezing must stay fast while force-closing stays slow."""
    freeze = SimpleConfig.INBOUND_LIQUIDITY_STUCK_OPEN_TIMEOUT_MIN.get_default_value()
    close = SimpleConfig.INBOUND_LIQUIDITY_STUCK_OPEN_CLOSE_TIMEOUT_MIN.get_default_value()
    assert close >= 4 * freeze, (freeze, close)


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
