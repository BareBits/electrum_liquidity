"""Glue-level tests for the update check: reading the running version, the
once-a-day throttle, what actually goes over the wire, and how the result is
reported. Heavy Electrum objects are faked; skipped outside the Electrum venv.

The check is the plugin's only contact with a server that has nothing to do with
running the wallet, so the properties under test are as much about restraint as
about function: it must not fire when switched off, must not re-fire on every
tick, must not raise into whatever called it, must not follow a doctored link,
and must never install anything.
"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins import inbound_liquidity as pkg  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    UPDATE_CHECK_INTERVAL_SEC,
    UPDATE_CHECK_MAX_BYTES,
    UPDATE_CHECK_URL,
    UPDATE_RELEASES_URL,
    LiquidityPlugin,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ReleaseInfo,
)


class _Config:
    """The update-check ConfigVars as a plain attribute bag."""

    def __init__(self, **overrides: Any) -> None:
        self.INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED = True
        self.INBOUND_LIQUIDITY_UPDATE_CHECK_PROMPTED = True
        self.INBOUND_LIQUIDITY_UPDATE_LAST_CHECK_TS = 0.0
        self.INBOUND_LIQUIDITY_UPDATE_LATEST_VERSION = ""
        self.INBOUND_LIQUIDITY_UPDATE_LATEST_URL = ""
        self.INBOUND_LIQUIDITY_DIAG_LOG_ENABLED = False
        for key, value in overrides.items():
            setattr(self, key, value)


def _plugin(*, version: str = "0.1.14", **config_overrides: Any) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.update")
    p.config = _Config(**config_overrides)
    p._tick_status = {}
    p.wallets = {}
    p._plugin_version_cache = version      # pin the "running" version
    p.statuses: List[str] = []
    p.on_status_changed = lambda wallet, status: p.statuses.append(status)
    return p


class _Wallet:
    """A wallet stand-in. A real ``Abstract_Wallet`` is hashable (the plugin keys
    its per-wallet state by it); ``SimpleNamespace`` is not, so this is a class."""

    def __init__(self, *, proxy: Any = None) -> None:
        self.network = SimpleNamespace(proxy=proxy, asyncio_loop=None)

    def basename(self) -> str:
        return "w"


def _wallet(*, proxy: Any = None) -> _Wallet:
    return _Wallet(proxy=proxy)


# --- the running version --------------------------------------------------
def test_plugin_version_comes_from_the_manifest() -> None:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.update")
    p.config = _Config()
    # Read through pkgutil, so this is the same path a ZIP install takes.
    from importlib.resources import files
    manifest = json.loads(
        (files("electrum.plugins.inbound_liquidity") / "manifest.json").read_text())
    assert p.plugin_version() == manifest["version"]


def test_plugin_version_is_cached() -> None:
    p = _plugin(version="9.9.9")
    assert p.plugin_version() == "9.9.9"     # the pinned cache, not the manifest


# --- what gets reported ---------------------------------------------------
def test_update_status_is_silent_when_the_check_is_disabled() -> None:
    # A user who declined must not see the feature anywhere, including a line
    # telling them they are up to date.
    p = _plugin(INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED=False,
                INBOUND_LIQUIDITY_UPDATE_LATEST_VERSION="0.9.9")
    assert p.update_status() == ""
    assert p.update_available() is True       # the fact is still knowable


def test_update_status_reports_up_to_date_and_behind() -> None:
    p = _plugin(version="0.1.14", INBOUND_LIQUIDITY_UPDATE_LATEST_VERSION="0.1.14")
    assert p.update_status() == "version 0.1.14 (up to date)"
    assert p.update_available() is False

    p.config.INBOUND_LIQUIDITY_UPDATE_LATEST_VERSION = "0.1.15"
    assert p.update_available() is True
    assert p.update_status() == "update available: 0.1.15 (running 0.1.14)"


def test_release_url_falls_back_to_the_releases_page() -> None:
    p = _plugin()
    assert p.latest_release_url() == UPDATE_RELEASES_URL
    p.config.INBOUND_LIQUIDITY_UPDATE_LATEST_URL = "https://example.invalid/r/1"
    assert p.latest_release_url() == "https://example.invalid/r/1"


def test_an_unreadable_running_version_never_claims_an_update() -> None:
    p = _plugin(version="", INBOUND_LIQUIDITY_UPDATE_LATEST_VERSION="0.1.15")
    assert p.update_available() is False
    assert p.update_status() == "version unknown (up to date)"


# --- the throttle ---------------------------------------------------------
def _stub_fetch(p: LiquidityPlugin, release: Optional[ReleaseInfo]) -> List[int]:
    calls: List[int] = []

    async def _fetch(wallet):
        calls.append(1)
        return release
    p._fetch_latest_release = _fetch
    return calls


def test_no_request_at_all_when_the_check_is_disabled() -> None:
    p = _plugin(INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED=False)
    calls = _stub_fetch(p, ReleaseInfo(version="0.1.15", url="https://x/1"))
    asyncio.run(p._maybe_check_for_update(_wallet()))
    assert calls == []
    # And nothing is stamped, so enabling it later checks immediately.
    assert p.config.INBOUND_LIQUIDITY_UPDATE_LAST_CHECK_TS == 0.0


def test_the_check_runs_at_most_once_per_interval() -> None:
    p = _plugin()
    calls = _stub_fetch(p, ReleaseInfo(version="0.1.15", url="https://x/1"))

    asyncio.run(p._maybe_check_for_update(_wallet()))
    asyncio.run(p._maybe_check_for_update(_wallet()))
    asyncio.run(p._maybe_check_for_update(_wallet()))
    assert len(calls) == 1

    # A day later it is due again.
    p.config.INBOUND_LIQUIDITY_UPDATE_LAST_CHECK_TS -= UPDATE_CHECK_INTERVAL_SEC + 1
    asyncio.run(p._maybe_check_for_update(_wallet()))
    assert len(calls) == 2


def test_a_failed_lookup_still_costs_a_day() -> None:
    # The timestamp is stamped BEFORE the request precisely so an endpoint that
    # is down (or rate-limiting us) is not retried on every heartbeat.
    p = _plugin()
    calls = _stub_fetch(p, None)
    asyncio.run(p._maybe_check_for_update(_wallet()))
    asyncio.run(p._maybe_check_for_update(_wallet()))
    assert len(calls) == 1
    assert p.config.INBOUND_LIQUIDITY_UPDATE_LAST_CHECK_TS > 0.0
    # Nothing was cached from a failure, so no stale claim is made.
    assert p.config.INBOUND_LIQUIDITY_UPDATE_LATEST_VERSION == ""


def test_a_successful_check_caches_the_answer_and_notifies_the_gui() -> None:
    p = _plugin(version="0.1.14")
    _stub_fetch(p, ReleaseInfo(version="0.1.15", url="https://x/rel"))
    asyncio.run(p._maybe_check_for_update(_wallet()))

    assert p.config.INBOUND_LIQUIDITY_UPDATE_LATEST_VERSION == "0.1.15"
    assert p.config.INBOUND_LIQUIDITY_UPDATE_LATEST_URL == "https://x/rel"
    assert p.update_available() is True
    assert p.statuses, "the tab was never told to re-read the status"


def test_being_up_to_date_is_recorded_without_a_warning(caplog) -> None:
    p = _plugin(version="0.1.14")
    _stub_fetch(p, ReleaseInfo(version="0.1.14", url="https://x/rel"))
    with caplog.at_level(logging.INFO):
        asyncio.run(p._maybe_check_for_update(_wallet()))
    assert any("up to date" in r.message for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_an_available_update_is_logged_once_per_version(caplog) -> None:
    p = _plugin(version="0.1.14")
    _stub_fetch(p, ReleaseInfo(version="0.1.15", url="https://x/rel"))
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            p.config.INBOUND_LIQUIDITY_UPDATE_LAST_CHECK_TS = 0.0   # force it due
            asyncio.run(p._maybe_check_for_update(_wallet()))
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "0.1.15" in warnings[0].message and "https://x/rel" in warnings[0].message


# --- what goes over the wire ----------------------------------------------
class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body
        self.content = SimpleNamespace(read=self._read)

    async def _read(self, limit: int) -> bytes:
        return self._body[:limit]

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeSession:
    def __init__(self, response: _FakeResponse, record: Dict) -> None:
        self._response = response
        self._record = record

    def get(self, url: str) -> _FakeResponse:
        self._record["url"] = url
        return self._response

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc) -> None:
        return None


def _patch_session(monkeypatch, status: int = 200, body: Optional[bytes] = None) -> Dict:
    """Swap the module's ``make_aiohttp_session`` for a fake, recording what the
    plugin asked for (URL, proxy, headers, timeout)."""
    record: Dict[str, Any] = {}
    if body is None:
        body = json.dumps({
            "tag_name": "v0.1.15", "draft": False, "prerelease": False,
            "html_url": "https://github.com/BareBits/electrum_liquidity/releases/tag/v0.1.15",
        }).encode("utf-8")
    response = _FakeResponse(status, body)

    def _make(proxy, headers=None, timeout=None):
        record["proxy"] = proxy
        record["headers"] = headers
        record["timeout"] = timeout
        return _FakeSession(response, record)

    monkeypatch.setattr(pkg, "make_aiohttp_session", _make)
    return record


def test_the_request_goes_to_the_releases_endpoint_through_electrums_proxy(monkeypatch) -> None:
    # The one request this plugin makes to a non-wallet server must not be the
    # one that ignores a Tor user's proxy.
    p = _plugin()
    proxy = object()
    record = _patch_session(monkeypatch)
    release = asyncio.run(p._fetch_latest_release(_wallet(proxy=proxy)))

    assert record["url"] == UPDATE_CHECK_URL
    assert record["proxy"] is proxy
    assert record["timeout"] and record["timeout"] > 0
    assert release == ReleaseInfo(
        version="0.1.15",
        url="https://github.com/BareBits/electrum_liquidity/releases/tag/v0.1.15")


@pytest.mark.parametrize("status", [304, 403, 404, 429, 500])
def test_a_non_200_reply_is_not_a_release(monkeypatch, status: int) -> None:
    # 403/429 is what rate limiting looks like; none of these may be parsed.
    p = _plugin()
    _patch_session(monkeypatch, status=status)
    assert asyncio.run(p._fetch_latest_release(_wallet())) is None


@pytest.mark.parametrize("body", [
    b"<!DOCTYPE html><html>rate limited</html>",
    b"{not json",
    b"{}",
    b'{"tag_name": "v0.1.15", "prerelease": true}',
])
def test_a_junk_body_is_not_a_release(monkeypatch, body: bytes) -> None:
    p = _plugin()
    _patch_session(monkeypatch, body=body)
    assert asyncio.run(p._fetch_latest_release(_wallet())) is None


def test_an_oversized_body_is_refused(monkeypatch) -> None:
    p = _plugin()
    _patch_session(monkeypatch, body=b"x" * (UPDATE_CHECK_MAX_BYTES + 10))
    assert asyncio.run(p._fetch_latest_release(_wallet())) is None


def test_a_network_error_is_swallowed(monkeypatch) -> None:
    p = _plugin()

    def _boom(proxy, headers=None, timeout=None):
        raise OSError("network unreachable")
    monkeypatch.setattr(pkg, "make_aiohttp_session", _boom)
    assert asyncio.run(p._fetch_latest_release(_wallet())) is None


def test_a_check_failure_never_escapes_into_the_caller(monkeypatch) -> None:
    # _request_update_check is fired from wallet start and the heartbeat; a
    # courtesy lookup must not be able to break either.
    p = _plugin()

    async def _boom(wallet):
        raise RuntimeError("boom")
    p._fetch_latest_release = _boom
    with pytest.raises(RuntimeError):
        asyncio.run(p._maybe_check_for_update(_wallet()))   # the raw coroutine propagates
    # ...but the scheduling wrapper (no loop available here) never raises.
    p._request_update_check(_wallet())


def test_cancellation_is_not_swallowed(monkeypatch) -> None:
    # Shutdown must actually cancel the lookup rather than have it log-and-return.
    p = _plugin()

    def _make(proxy, headers=None, timeout=None):
        raise asyncio.CancelledError()
    monkeypatch.setattr(pkg, "make_aiohttp_session", _make)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(p._fetch_latest_release(_wallet()))
