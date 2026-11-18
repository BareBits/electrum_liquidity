"""Bounds on values that arrive from outside, or from a hand-edited wallet file.

Four separate hardenings, grouped because they share one shape: a value whose
range nothing was checking, reaching code that assumed it was sane.

  * a release payload's version/URL. The update check caps the BODY it reads
    (512 KB), but a capped body still holds a quarter of a million version
    components -- "1.1.1.1..." matches the version regex in full, and the parsed
    tuple, then the string rebuilt from it, was persisted to the config and
    rendered in the status footer at that size;
  * the engine's config knobs. ``set_setting`` type-checks a value against its
    ConfigVar but never range-checks it, and the per-wallet store lives in the
    wallet file -- which this plugin's own docs invite users to read, and which
    they can therefore edit. A negative reserve or a NaN ceiling was reachable;
  * ``_plain_json_copy``'s recursion, which walked a wallet-db value to whatever
    depth it happened to have;
  * a swap provider's RPC reply, whose "is this an object" check was a bare
    ``assert`` -- stripped entirely under ``python -O``.

None of these loses money today; several are fail-safe by downstream luck
(a negative reserve oversizes the funding amount and dies on Electrum's
NotEnoughFunds; a NaN ceiling fails every comparison and declines every swap).
That is the point: the safety should be a property of the boundary, not an
accident of what happens to be downstream of it.

Skipped outside the Electrum venv where it imports the glue.
"""
from __future__ import annotations

import math

import pytest

from liquidity_manager import (  # type: ignore  # noqa: E402
    MAX_RELEASE_URL_CHARS,
    MAX_VERSION_COMPONENTS,
    clamp_pct,
    clamp_sat,
    extract_release,
    is_newer_version,
    parse_version,
)


# --- release payloads -----------------------------------------------------
def test_an_ordinary_version_still_parses() -> None:
    assert parse_version("v0.5.0") == (0, 5, 0)
    assert parse_version("0.5.0-beta.8") == (0, 5, 0)
    assert parse_version("4.7.2") == (4, 7, 2)
    assert parse_version("2026.11.16") == (2026, 11, 16)


def test_a_version_with_absurdly_many_components_is_refused() -> None:
    """Refused rather than truncated: truncating would invent a DIFFERENT version
    from the one published and then compare against it, which is worse than
    declining to answer."""
    assert parse_version(".".join(["1"] * (MAX_VERSION_COMPONENTS + 1))) is None
    assert parse_version(".".join(["1"] * 100_000)) is None


def test_a_version_with_one_absurdly_long_component_is_refused() -> None:
    """One component passes the count check on its own, so the length of each is
    bounded too."""
    assert parse_version("1" * 100_000) is None
    assert parse_version("1." + "9" * 50_000) is None


def test_an_over_long_version_is_never_reported_as_an_update() -> None:
    """The consequence that matters: an unparseable version must not read as
    "newer", which would nag the user about a release that does not exist."""
    monster = ".".join(["1"] * 100_000)
    assert is_newer_version(monster, "0.5.0") is False
    assert extract_release({"tag_name": monster, "html_url": "https://x/y"}) is None


def test_an_over_long_release_url_is_dropped() -> None:
    """The URL is persisted to the config and rendered in a Qt label, so its
    length is bounded at the boundary like everything else from off-machine."""
    payload = {"tag_name": "v0.6.0",
               "html_url": "https://example.com/" + "a" * MAX_RELEASE_URL_CHARS}
    release = extract_release(payload)
    assert release is not None and release.version == "0.6.0"
    assert release.url == "", "an unbounded URL survived"


def test_a_normal_release_url_is_kept() -> None:
    url = "https://github.com/BareBits/electrum_liquidity/releases/tag/v0.6.0"
    release = extract_release({"tag_name": "v0.6.0", "html_url": url})
    assert release is not None and release.url == url


# --- settings clamps ------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (0, 0),
    (10_000, 10_000),
    (-1, 0),                       # negative reserve: clamped, not trusted
    (-10_000_000, 0),
    (10**30, 21_000_000 * 100_000_000),      # absurd: held to the sat ceiling
    ("50000", 50_000),             # a string the type check let through
    (12345.9, 12345),              # a float setting truncates
])
def test_clamp_sat_holds_a_setting_in_range(value, expected) -> None:
    assert clamp_sat(value) == expected


@pytest.mark.parametrize("value", ["not-a-number", None, object(), [1]])
def test_clamp_sat_falls_back_rather_than_raising(value) -> None:
    """Read on every tick, so an unusable value must cost that setting and not
    the whole evaluation."""
    assert clamp_sat(value, default=7) == 7


@pytest.mark.parametrize("value,expected", [
    (0.0, 0.0),
    (0.9, 0.9),
    (100.0, 100.0),
    (-5.0, 0.0),
    (1e9, 100.0),
])
def test_clamp_pct_holds_a_percentage_in_range(value, expected) -> None:
    assert clamp_pct(value) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_clamp_pct_rejects_nan_and_the_infinities(value) -> None:
    """NaN is rejected, not clamped. Every comparison with NaN is False, so a NaN
    ceiling does not read as "no limit" or as "no swaps" -- it reads as whichever
    way the surrounding `if` happens to be written, which is exactly the kind of
    invisible behaviour this exists to prevent."""
    out = clamp_pct(value, default=0.9)
    assert out == 0.9 and math.isfinite(out)


def test_read_config_clamps_a_hand_edited_wallet_file() -> None:
    """The clamps where they actually matter: on the way into the engine."""
    pytest.importorskip("electrum.plugins.inbound_liquidity")
    from conftest import seed_settings                     # type: ignore
    from test_dev_fee_glue import _FakeWallet, _plugin      # type: ignore

    p = _plugin()
    w = _FakeWallet(None)
    seed_settings(
        p,
        INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT=-50_000,     # negative reserve
        INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT=float("nan"),   # NaN ceiling
        INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT=float("inf"),   # non-finite threshold
        INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT=-1,
        INBOUND_LIQUIDITY_MAX_CHANNELS=-3,
    )
    cfg = p.read_config(w)
    assert cfg.onchain_reserve_sat == 0
    assert math.isfinite(cfg.max_swap_fee_pct)
    assert math.isfinite(cfg.swap_trigger_pct)
    assert cfg.swap_trigger_sat == 0
    assert cfg.max_channels == 0


def test_a_percentage_threshold_above_100_is_preserved() -> None:
    """A threshold over 100% must survive the clamp, because that is how it is
    turned OFF -- and clamping it to 100 inverts the request.

    ``swap_trigger_pct`` means "swap once a channel's local balance exceeds this
    percentage of its capacity". At exactly 100 a fresh channel (local ==
    capacity) is still over the trigger, so clamping a deliberate 1000 down to
    100 does not mean "never swap" -- it means "swap on every fresh channel".
    The e2e suite uses exactly that idiom to stage a swap-free scenario, and an
    earlier version of this hardening broke it.

    ``max_swap_fee_pct`` is the same shape of mistake: an all-in cost CAN exceed
    100% on a small enough amount, because the mining and claim fees are fixed,
    so >100 is a meaningful "accept any cost".
    """
    pytest.importorskip("electrum.plugins.inbound_liquidity")
    from conftest import seed_settings                     # type: ignore
    from test_dev_fee_glue import _FakeWallet, _plugin      # type: ignore

    p = _plugin()
    w = _FakeWallet(None)
    seed_settings(
        p,
        INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT=1000.0,   # the "never swap" idiom
        INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT=500.0,    # "accept any cost"
    )
    cfg = p.read_config(w)
    assert cfg.swap_trigger_pct == 1000.0, (
        "a >100 swap trigger was clamped, which turns 'never' into 'always'")
    assert cfg.max_swap_fee_pct == 500.0


# --- wallet-db recursion --------------------------------------------------
def test_plain_json_copy_is_depth_limited() -> None:
    """A deeply nested value -- hand-edited, or a corrupted store -- must not
    raise RecursionError out of a routine bookkeeping write."""
    pytest.importorskip("electrum.plugins.inbound_liquidity")
    from electrum.plugins.inbound_liquidity import (       # type: ignore
        MAX_JSON_COPY_DEPTH, _plain_json_copy,
    )
    deep: dict = {}
    node = deep
    for _ in range(MAX_JSON_COPY_DEPTH * 50):
        node["next"] = {}
        node = node["next"]
    out = _plain_json_copy(deep)            # must not raise
    # Walk down and confirm it terminates at the limit rather than going on.
    depth = 0
    node = out
    while isinstance(node, dict) and "next" in node and node["next"] is not None:
        node = node["next"]
        depth += 1
    assert depth <= MAX_JSON_COPY_DEPTH + 1


def test_plain_json_copy_still_copies_real_stores_faithfully() -> None:
    pytest.importorskip("electrum.plugins.inbound_liquidity")
    from electrum.plugins.inbound_liquidity import _plain_json_copy  # type: ignore
    store = {"npub1x": {"fault_count": 2, "last_reason": "stuck",
                        "history": [[1.0, 2.0], [3.0, 4.0]]}}
    assert _plain_json_copy(store) == store


# --- a provider's RPC reply ----------------------------------------------
def test_a_non_object_rpc_reply_is_refused_without_an_assert() -> None:
    """The check was a bare ``assert``, which ``python -O`` removes entirely.

    With it gone, a reply decoding to a string would have been searched for the
    SUBSTRING "error", and a list would have raised an unhandled TypeError out of
    the swap path. Either way a malformed reply is the provider failing to answer,
    so it must surface as that.
    """
    pytest.importorskip("electrum.plugins.inbound_liquidity")
    import asyncio
    import logging
    from electrum.submarine_swaps import SwapServerError    # type: ignore
    from electrum.plugins.inbound_liquidity.swap_transport import (  # type: ignore
        TargetedNostrTransport,
    )

    for reply in ("error-ish text", ["error"], 42, None):
        tr = object.__new__(TargetedNostrTransport)
        tr.logger = logging.getLogger("test.inbound_liquidity.transport")
        tr.target_pubkey = "ab" * 32
        tr.dm_replies = {}

        async def _send(pubkey, body, retries=1):
            return "event-id"
        tr.send_direct_message = _send

        async def _drive():
            # Resolve the reply slot with the malformed value as the DM handler
            # would, then let the RPC read it.
            async def _resolve():
                await asyncio.sleep(0)
                for fut in list(tr.dm_replies.values()):
                    if not fut.done():
                        fut.set_result(reply)
            asyncio.ensure_future(_resolve())
            return await tr.send_request_to_server("createswap", {})

        with pytest.raises(SwapServerError):
            asyncio.run(_drive())
