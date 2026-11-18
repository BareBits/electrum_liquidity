"""The pure half of swap-provider display names.

A provider's swap offer carries no name at all, so the only human-readable
identity it can have is a nostr kind-0 profile event, whose ``content`` is a
JSON string the provider wrote itself. That makes every byte of it
attacker-controlled display input, and these tests pin the two pure functions
that stand between it and a table cell:

* ``clean_display_name`` -- the sanitiser, shared with the channel-partner node
  alias (see test_node_alias_glue for the alias side of it);
* ``parse_provider_name`` -- the kind-0 ``content`` reader, which must yield
  ``""`` rather than raise for anything malformed.

Pure: no Electrum, no GUI, no I/O.
"""
from __future__ import annotations

import json

from liquidity_manager import (  # type: ignore
    MAX_NODE_ALIAS_CHARS,
    MAX_PROFILE_CONTENT_CHARS,
    MAX_PROVIDER_NAME_CHARS,
    clean_display_name,
    parse_provider_name,
)


# --- clean_display_name ---------------------------------------------------
def test_an_ordinary_name_passes_through() -> None:
    assert clean_display_name("Fast Swaps") == "Fast Swaps"


def test_non_ascii_names_survive() -> None:
    # Printable is printable; a provider with a non-Latin name is not hostile.
    assert clean_display_name("Крипто Свопы") == "Крипто Свопы"
    assert clean_display_name("スワップ") == "スワップ"


def test_blank_and_missing_names_clean_to_nothing() -> None:
    for raw in (None, "", "   ", "\n\t", "​​"):
        assert clean_display_name(raw) == "", repr(raw)


def test_control_characters_cannot_break_out_of_the_cell() -> None:
    # A newline in a table cell means a row that spills over its neighbours.
    assert clean_display_name("Fast\nSwaps") == "Fast Swaps"
    assert clean_display_name("Fast\r\nSwaps") == "Fast Swaps"
    assert clean_display_name("Fast\tSwaps") == "Fast Swaps"
    assert clean_display_name("Fast\x00Swaps") == "FastSwaps"


def test_runs_of_whitespace_collapse_and_edges_are_trimmed() -> None:
    assert clean_display_name("  Fast     Swaps  ") == "Fast Swaps"


def test_zero_width_padding_cannot_disguise_a_name() -> None:
    # These sit INSIDE a word to make it render as another provider's name, so
    # they are deleted rather than turned into a space -- otherwise the result
    # would read "Fast Swaps" and differ from the real thing only invisibly.
    assert clean_display_name("Fast​Swaps") == "FastSwaps"
    assert clean_display_name("Fast­Swaps") == "FastSwaps"


def test_bidi_overrides_cannot_reverse_a_name() -> None:
    # U+202E (RTL override) renders the following text backwards, which is how
    # one name is made to look like another.
    assert clean_display_name("‮Fast Swaps") == "Fast Swaps"


def test_a_long_name_is_bounded_and_marked_as_cut() -> None:
    name = clean_display_name("S" * 200, max_chars=MAX_PROVIDER_NAME_CHARS)
    assert len(name) == MAX_PROVIDER_NAME_CHARS
    assert name.endswith("…")


def test_the_bound_is_a_parameter_so_each_caller_keeps_its_own() -> None:
    # The alias and profile-name bounds happen to be equal today; the point is
    # that neither caller is hardcoded into the shared sanitiser.
    assert clean_display_name("S" * 50, max_chars=8) == "SSSSSSS…"
    assert MAX_NODE_ALIAS_CHARS == 32
    assert MAX_PROVIDER_NAME_CHARS == 32


def test_a_name_exactly_at_the_bound_is_not_truncated() -> None:
    exact = "S" * MAX_PROVIDER_NAME_CHARS
    assert clean_display_name(exact, max_chars=MAX_PROVIDER_NAME_CHARS) == exact


def test_an_object_whose_str_raises_costs_a_name_not_a_crash() -> None:
    class _Hostile:
        def __str__(self) -> str:
            raise RuntimeError("no")

        def __bool__(self) -> bool:
            return True

    assert clean_display_name(_Hostile()) == ""


# --- parse_provider_name --------------------------------------------------
def test_display_name_is_preferred_over_name() -> None:
    # Most clients write both and treat display_name as the one for display.
    content = json.dumps({"name": "fastswaps", "display_name": "Fast Swaps"})
    assert parse_provider_name(content) == "Fast Swaps"


def test_name_is_used_when_there_is_no_display_name() -> None:
    assert parse_provider_name(json.dumps({"name": "fastswaps"})) == "fastswaps"


def test_the_camelcase_spelling_is_accepted() -> None:
    assert parse_provider_name(json.dumps({"displayName": "Fast"})) == "Fast"


def test_a_blank_display_name_falls_through_to_name() -> None:
    # A provider that writes display_name:"" has not named itself with it.
    content = json.dumps({"display_name": "   ", "name": "fastswaps"})
    assert parse_provider_name(content) == "fastswaps"


def test_a_profile_with_no_name_field_yields_nothing() -> None:
    content = json.dumps({"about": "we do swaps", "picture": "http://x/y.png"})
    assert parse_provider_name(content) == ""


def test_a_name_that_is_not_a_string_is_not_a_name() -> None:
    # str() of a dict or a number would otherwise land in the table.
    for value in ({"a": 1}, ["Fast"], 42, 4.2, True, None):
        assert parse_provider_name(json.dumps({"name": value})) == "", repr(value)


def test_non_json_content_yields_nothing() -> None:
    for raw in ("", "   ", "not json", "{", "{'single': 'quotes'}", "\x00"):
        assert parse_provider_name(raw) == "", repr(raw)


def test_json_that_is_not_an_object_yields_nothing() -> None:
    for raw in ("[1, 2, 3]", '"a string"', "42", "null", "true"):
        assert parse_provider_name(raw) == "", repr(raw)


def test_non_string_content_yields_nothing() -> None:
    # The transport hands us whatever the event carried; a dict is not content.
    for raw in (None, 42, {"name": "Fast"}, ["x"], b'{"name": "Fast"}'):
        assert parse_provider_name(raw) == "", repr(raw)


def test_an_oversized_payload_is_refused_before_it_is_parsed() -> None:
    # The cheap length check comes first, so a provider cannot make us parse an
    # unbounded string.
    content = json.dumps({"name": "Fast", "about": "x" * MAX_PROFILE_CONTENT_CHARS})
    assert len(content) > MAX_PROFILE_CONTENT_CHARS
    assert parse_provider_name(content) == ""


def test_a_payload_just_inside_the_bound_is_still_read() -> None:
    padding = "x" * (MAX_PROFILE_CONTENT_CHARS - 64)
    content = json.dumps({"name": "Fast Swaps", "about": padding})
    assert len(content) <= MAX_PROFILE_CONTENT_CHARS
    assert parse_provider_name(content) == "Fast Swaps"


def test_a_hostile_name_is_sanitised_on_the_way_out() -> None:
    # The whole point of routing kind-0 through the sanitiser.
    content = json.dumps({"display_name": "Fast‮\nSwaps​"})
    assert parse_provider_name(content) == "Fast Swaps"


def test_a_name_that_sanitises_to_nothing_yields_nothing() -> None:
    content = json.dumps({"display_name": "​​", "name": "\n\t"})
    assert parse_provider_name(content) == ""


def test_a_long_profile_name_is_bounded() -> None:
    name = parse_provider_name(json.dumps({"name": "S" * 500}))
    assert len(name) == MAX_PROVIDER_NAME_CHARS


def test_the_deepest_nesting_that_fits_the_bound_is_handled() -> None:
    # A nesting-depth attack is answered by the length bound rather than by the
    # parser: the deepest payload that fits in MAX_PROFILE_CONTENT_CHARS is two
    # characters per level, so it cannot reach a depth CPython's json refuses.
    # It parses, is not an object, and yields no name -- no exception either way.
    depth = MAX_PROFILE_CONTENT_CHARS // 2
    content = "[" * depth + "]" * depth
    assert len(content) == MAX_PROFILE_CONTENT_CHARS
    assert parse_provider_name(content) == ""
