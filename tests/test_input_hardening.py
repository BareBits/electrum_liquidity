"""Unit tests for the untrusted-input hardening helpers in ``liquidity_manager``.

These are PURE (no Electrum, no I/O), covering the log/CRLF scrubber, the
provider/peer identity shape gates, and the provider-offer validator that
rejects a hostile provider's negative / NaN / infinite / non-numeric / absurd
economics before they can reach the cost gate or the cheapest-provider ranking.
"""
from __future__ import annotations

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)  # noqa: E402
    ProviderOffer,
    clean_npub,
    looks_like_hex_pubkey,
    scrub_text,
    validate_offer,
)

VALID_NPUB = "npub1" + "q" * 58


# --- scrub_text -----------------------------------------------------------
def test_scrub_escapes_crlf_and_ansi() -> None:
    # The core log-forging vector: a CR/LF that would open a second log line, and
    # an ANSI escape (0x1b) that would rewrite a terminal, are both neutralised.
    assert scrub_text("line1\r\n[FAKE] line2") == r"line1\r\n[FAKE] line2"
    assert scrub_text("\x1b[31mred\x1b[0m") == r"\x1b[31mred\x1b[0m"
    assert scrub_text("tab\there") == r"tab\there"
    # A NUL and a C1 control are escaped as \xNN.
    assert scrub_text("a\x00b\x85c") == r"a\x00b\x85c"


def test_scrub_passes_through_printable_and_unicode() -> None:
    assert scrub_text("normal text 123 !@#") == "normal text 123 !@#"
    assert scrub_text("café ✓ 日本語") == "café ✓ 日本語"


def test_scrub_none_and_non_str() -> None:
    assert scrub_text(None) == ""
    assert scrub_text(RuntimeError("bad\nnews")) == r"bad\nnews"  # coerced via str()
    assert scrub_text(1234) == "1234"


def test_scrub_truncates() -> None:
    out = scrub_text("x" * 500, max_len=50)
    assert out.endswith("…(truncated)")
    assert out.startswith("x" * 50)


def test_scrub_result_has_no_raw_control_bytes() -> None:
    # Whatever the input, the output must never contain a raw C0/C1/DEL byte.
    hostile = "".join(chr(c) for c in range(0, 0xA0)) + "tail"
    out = scrub_text(hostile, max_len=10_000)
    assert not any(ord(ch) < 0x20 or ord(ch) == 0x7F or 0x80 <= ord(ch) <= 0x9F
                   for ch in out)


# --- identity shape gates -------------------------------------------------
def test_looks_like_hex_pubkey() -> None:
    assert looks_like_hex_pubkey("ab" * 32)
    assert looks_like_hex_pubkey("AB" * 32)
    assert not looks_like_hex_pubkey("ab" * 31)      # too short
    assert not looks_like_hex_pubkey("ab" * 33)      # too long
    assert not looks_like_hex_pubkey("gg" * 32)      # non-hex
    assert not looks_like_hex_pubkey("")
    assert not looks_like_hex_pubkey(None)
    assert not looks_like_hex_pubkey("ab" * 31 + "a\n")  # embedded newline


def test_clean_npub() -> None:
    assert clean_npub(VALID_NPUB) == VALID_NPUB
    assert clean_npub("  " + VALID_NPUB + "  ") == VALID_NPUB    # trimmed
    assert clean_npub(VALID_NPUB + "\nnpub1evil") == ""          # embedded CRLF
    assert clean_npub("npub1short") == ""                        # too short
    assert clean_npub("not-an-npub") == ""
    assert clean_npub("npub1 with space") == ""
    assert clean_npub("") == ""
    assert clean_npub(None) == ""


# --- validate_offer -------------------------------------------------------
def test_validate_offer_accepts_sane_terms() -> None:
    o = validate_offer(VALID_NPUB, 0.5, 1_000, 20_000, 1_800_000, 18)
    assert isinstance(o, ProviderOffer)
    assert (o.npub, o.percentage_fee, o.mining_fee_sat, o.min_amount_sat,
            o.max_reverse_sat, o.pow_bits) == (VALID_NPUB, 0.5, 1_000, 20_000, 1_800_000, 18)


def test_validate_offer_coerces_numeric_strings() -> None:
    # aionostr/Electrum may hand us numbers as strings; those still validate.
    o = validate_offer(VALID_NPUB, "0.5", "1000", "20000", "1800000", "0")
    assert o is not None and o.mining_fee_sat == 1000


def test_validate_offer_rejects_hostile_economics() -> None:
    bad_cases = [
        dict(percentage_fee=-1.0),                 # negative fee -> would look "free"
        dict(percentage_fee=float("nan")),         # NaN -> sails through cost gate
        dict(percentage_fee=float("inf")),
        dict(percentage_fee=101.0),                # > 100%
        dict(percentage_fee="not-a-number"),
        dict(mining_fee_sat=-5),                   # negative
        dict(mining_fee_sat=float("inf")),
        dict(mining_fee_sat=10 ** 20),             # above total supply
        dict(min_amount_sat=-1),
        dict(max_reverse_sat=-1),
        dict(max_reverse_sat=10 ** 20),
        dict(pow_bits=-1),
        dict(pow_bits=999),                        # more than a 32-byte hash's bits
    ]
    for override in bad_cases:
        fields = dict(percentage_fee=0.5, mining_fee_sat=1_000, min_amount_sat=20_000,
                      max_reverse_sat=1_800_000, pow_bits=8)
        fields.update(override)
        assert validate_offer(VALID_NPUB, **fields) is None, override


def test_validate_offer_requires_identity() -> None:
    # An empty (already-rejected) npub can never yield an offer, even with sane
    # economics -- there is nothing to attribute or address.
    assert validate_offer("", 0.5, 1_000, 20_000, 1_800_000, 8) is None


def test_validate_offer_boundaries_ok() -> None:
    # Exact bounds are accepted (0% fee, 0 fees, 100% fee, 256 pow bits).
    assert validate_offer(VALID_NPUB, 0.0, 0, 0, 0, 0) is not None
    assert validate_offer(VALID_NPUB, 100.0, 0, 0, 0, 256) is not None
