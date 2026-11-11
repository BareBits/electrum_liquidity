"""`clean_relay_urls`: hygiene on a provider's announced relay list.

Worth stating plainly, because the name invites the wrong reading: following a
provider's relays is the MECHANISM, not a risk. A provider must be able to say
where it can be reached, and subscribing there is how we hear it. This function
is not a gate on who we will talk to, and these are not security tests.

What it is for: those strings are logged and written to disk (Electrum dumps
them to `recent_swapserver_relays` and reloads them at startup), and this
plugin's standing rule is that text from outside is scrubbed before it reaches
a log line or storage. Plus a malformed offer should not put a non-string, an
unbounded string, or an entry with an embedded newline into a connect list.

So these tests pin exactly two things: junk does not get through, and anything
that is a usable WebSocket URL does -- including the parts of it that are the
provider's business and none of ours.

Pure: no relay, no Electrum, no I/O.
"""
from __future__ import annotations

import pytest

from liquidity_manager import (  # type: ignore
    MAX_RELAY_URL_CHARS,
    MAX_RELAY_URLS,
    clean_relay_urls,
)


# --- what a real announcement looks like ---------------------------------
def test_ordinary_relays_pass_through_in_order() -> None:
    urls = ["wss://relay.one", "ws://127.0.0.1:8080", "wss://relay.two/path"]
    assert clean_relay_urls(urls) == tuple(urls)


def test_order_is_the_providers_own() -> None:
    # It ranks its own relays; we keep that ranking rather than sorting.
    urls = ["wss://z.example", "wss://a.example"]
    assert clean_relay_urls(urls) == ("wss://z.example", "wss://a.example")


def test_surrounding_whitespace_is_trimmed() -> None:
    # Including a trailing CR/LF, which is sloppy formatting rather than an
    # attack: trimming it yields a clean URL. An EMBEDDED newline is a
    # different thing entirely and is refused -- see below.
    assert clean_relay_urls(["  wss://relay.one  "]) == ("wss://relay.one",)
    assert clean_relay_urls(["wss://relay.one\r\n"]) == ("wss://relay.one",)


def test_duplicates_collapse_case_insensitively() -> None:
    out = clean_relay_urls(["wss://Relay.One", "wss://relay.one", "wss://b"])
    assert out == ("wss://Relay.One", "wss://b")


# --- the scheme gate ------------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://relay.one",          # not a websocket
    "https://relay.one",
    "file:///etc/passwd",        # local file
    "ftp://relay.one",
    "javascript:alert(1)",
    "relay.one",                 # no scheme at all
    "//relay.one",
    "wss:/relay.one",            # malformed
    "ws:relay.one",
])
def test_a_non_websocket_url_is_dropped(url) -> None:
    assert clean_relay_urls([url]) == ()


def test_the_scheme_test_is_case_insensitive() -> None:
    # A provider cannot slip one past by shouting.
    assert clean_relay_urls(["WSS://relay.one"]) == ("WSS://relay.one",)


def test_a_bare_scheme_with_no_host_is_dropped() -> None:
    assert clean_relay_urls(["wss://", "ws://"]) == ()


# --- whitespace and control characters ------------------------------------
def test_the_providers_own_url_structure_is_passed_through() -> None:
    # Host, port, path, query and userinfo are the provider's business. Any
    # credentials in here are its own, for its own relay; dropping them would
    # cost a working relay and prevent nothing.
    for url in ("wss://user:pass@relay.one",
                "wss://relay.one:7777/socket",
                "wss://relay.one/?token=abc123",
                "wss://relay.one/path\\sub"):
        assert clean_relay_urls([url]) == (url,), url


@pytest.mark.parametrize("url", [
    "wss://relay.one\nwss://evil.example",
    "wss://relay .one",
    "wss://relay\tone",
    "wss://relay\x00.one",
    "wss://relay\x1b[31m.one",
    "wss://relay‮.one",
    "wss://relay​.one",
])
def test_whitespace_and_control_characters_are_refused(url) -> None:
    assert clean_relay_urls([url]) == ()


def test_one_bad_url_does_not_cost_the_good_ones() -> None:
    out = clean_relay_urls(
        ["wss://good.one", "http://bad", "wss://good.two", "wss://a b"])
    assert out == ("wss://good.one", "wss://good.two")


# --- bounds ---------------------------------------------------------------
def test_the_list_is_capped() -> None:
    urls = [f"wss://relay{i}.example" for i in range(MAX_RELAY_URLS + 25)]
    out = clean_relay_urls(urls)
    assert len(out) == MAX_RELAY_URLS
    assert out[0] == "wss://relay0.example"


def test_the_cap_counts_only_the_urls_that_survive() -> None:
    # A provider padding its list with junk must not use up the budget and
    # push the real relays out.
    urls = ["http://junk"] * 50 + [f"wss://relay{i}.example" for i in range(3)]
    assert clean_relay_urls(urls) == tuple(
        f"wss://relay{i}.example" for i in range(3))


def test_an_over_long_url_is_dropped() -> None:
    long_url = "wss://" + "a" * MAX_RELAY_URL_CHARS
    assert len(long_url) > MAX_RELAY_URL_CHARS
    assert clean_relay_urls([long_url]) == ()


def test_a_url_just_inside_the_bound_survives() -> None:
    url = "wss://" + "a" * (MAX_RELAY_URL_CHARS - len("wss://"))
    assert len(url) == MAX_RELAY_URL_CHARS
    assert clean_relay_urls([url]) == (url,)


# --- degenerate input -----------------------------------------------------
@pytest.mark.parametrize("raw", [None, (), [], "", 0])
def test_nothing_in_nothing_out(raw) -> None:
    assert clean_relay_urls(raw) == ()


def test_non_string_entries_are_skipped() -> None:
    out = clean_relay_urls([None, 42, {"url": "wss://x"}, ["wss://y"],
                            b"wss://z", "wss://real.one"])
    assert out == ("wss://real.one",)


def test_a_non_sequence_does_not_raise() -> None:
    assert clean_relay_urls(42) == ()
    assert clean_relay_urls(object()) == ()


def test_a_string_is_not_treated_as_a_list_of_characters() -> None:
    # Iterating a bare string would yield 'w','s','s',... and quietly produce
    # nothing; make sure that is what happens rather than something stranger.
    assert clean_relay_urls("wss://relay.one") == ()
