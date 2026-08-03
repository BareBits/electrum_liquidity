"""Unit tests for the pure half of the update check: version ordering and
reading a GitHub release payload. These import the engine module directly (via
conftest's sys.path shim), with no Electrum and no network.

What is worth pinning here is mostly about NOT crying wolf. The plugin tells the
user they are behind; every way that claim can be produced by accident -- string
ordering ("0.1.9" > "0.1.10"), an error page parsed as a version, a draft or
pre-release treated as published -- is a false alarm about the software that
manages their channels, so each has a test.
"""
from __future__ import annotations

from liquidity_manager import (  # type: ignore
    ReleaseInfo,
    extract_release,
    is_newer_version,
    parse_version,
)


# --- parsing --------------------------------------------------------------
def test_parse_version_reads_dotted_numbers_and_the_tag_prefix():
    assert parse_version("0.1.14") == (0, 1, 14)
    assert parse_version("v0.1.14") == (0, 1, 14)
    assert parse_version("V2.0") == (2, 0)
    assert parse_version("  v1.2.3  ") == (1, 2, 3)
    assert parse_version("1") == (1,)


def test_parse_version_ignores_a_trailing_suffix():
    # Release tags in the wild carry suffixes; the numeric part is the ordering.
    assert parse_version("v0.2.0-rc1") == (0, 2, 0)
    assert parse_version("0.2.0+build7") == (0, 2, 0)


def test_parse_version_rejects_anything_that_is_not_a_version():
    for text in ("", None, "latest", "vNext", "<!DOCTYPE html>", "release-x", {}):
        assert parse_version(text) is None, text


# --- ordering -------------------------------------------------------------
def test_component_ordering_is_numeric_not_lexicographic():
    # The whole reason this is not a string compare.
    assert is_newer_version("0.1.10", "0.1.9")
    assert not is_newer_version("0.1.9", "0.1.10")
    assert is_newer_version("0.2.0", "0.1.99")
    assert is_newer_version("1.0", "0.99.99")


def test_same_version_is_not_an_update():
    assert not is_newer_version("0.1.14", "0.1.14")
    assert not is_newer_version("v0.1.14", "0.1.14")
    # Shorter versions are zero-padded: 0.2 and 0.2.0 are the same release.
    assert not is_newer_version("0.2", "0.2.0")
    assert not is_newer_version("0.2.0", "0.2")


def test_a_longer_version_string_is_newer_only_when_it_is():
    assert is_newer_version("0.2.1", "0.2")
    assert not is_newer_version("0.2.0.0", "0.2")


def test_unparseable_versions_never_claim_an_update():
    # An unreadable answer must read as "no idea", never as "you are behind".
    assert not is_newer_version("garbage", "0.1.14")
    assert not is_newer_version("0.9.9", "garbage")
    assert not is_newer_version(None, "0.1.14")
    assert not is_newer_version("0.9.9", None)
    assert not is_newer_version("", "")


# --- reading the release payload ------------------------------------------
def _payload(**overrides) -> dict:
    # Shaped like the real api.github.com/repos/.../releases/latest reply.
    base = {
        "tag_name": "v0.1.15",
        "name": "v0.1.15",
        "draft": False,
        "prerelease": False,
        "html_url": "https://github.com/BareBits/electrum_liquidity/releases/tag/v0.1.15",
    }
    base.update(overrides)
    return base


def test_extract_release_reads_tag_and_url():
    release = extract_release(_payload())
    assert release == ReleaseInfo(
        version="0.1.15",
        url="https://github.com/BareBits/electrum_liquidity/releases/tag/v0.1.15")


def test_extract_release_skips_drafts_and_prereleases():
    # `releases/latest` is documented to exclude both -- but this is a remote
    # party's JSON, and an unreleased build must not be advertised as current.
    assert extract_release(_payload(draft=True)) is None
    assert extract_release(_payload(prerelease=True)) is None


def test_extract_release_falls_back_to_the_name_when_there_is_no_tag():
    release = extract_release(_payload(tag_name=None, name="0.1.15"))
    assert release is not None and release.version == "0.1.15"


def test_extract_release_rejects_unusable_payloads():
    for payload in (None, {}, "not a mapping", [1, 2],
                    _payload(tag_name=None, name=None),
                    _payload(tag_name="latest", name="latest")):
        assert extract_release(payload) is None, payload


def test_extract_release_drops_a_non_https_url():
    # A doctored payload must not get to choose the scheme of something the user
    # is invited to click. The version still stands; only the link is dropped,
    # and the caller substitutes the plugin's own releases page.
    for bad in ("javascript:alert(1)", "http://example.com/x", "file:///etc/passwd", 7):
        release = extract_release(_payload(html_url=bad))
        assert release is not None and release.url == "", bad


def test_extract_release_normalises_the_version_to_digits_and_dots():
    # `parse_version` matches a PREFIX so a suffix cannot break ordering, which
    # means the raw tag can carry arbitrary trailing text -- and this string is
    # rendered in a rich-text label and shown beside a link. Anything that is
    # not part of the number is dropped here, at the boundary.
    hostile = '0.1.15<img src="http://tracker.invalid/x">'
    release = extract_release(_payload(tag_name=hostile))
    assert release is not None
    assert release.version == "0.1.15"
    assert "<" not in release.version and '"' not in release.version


def test_extract_release_strips_a_prerelease_suffix_from_the_reported_version():
    # `releases/latest` excludes pre-releases, so a suffix reaching here is an
    # oddity; the reported version is the numeric release it belongs to.
    release = extract_release(_payload(tag_name="v0.2.0-rc1", prerelease=False))
    assert release is not None and release.version == "0.2.0"

