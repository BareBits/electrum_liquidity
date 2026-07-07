"""Unit tests for the uptime accumulator's defensive parsing of *persisted* data.

``record_uptime_sample`` / ``uptime_ratio`` read an accumulator that has been
round-tripped through the wallet file (JSON), so a hand-edited or
partially-corrupted store can present malformed buckets or a non-numeric
``last_ts``. These must be skipped (never crash the reliability pass), which is
exactly the branch coverage the happy-path tests miss. Pure engine -- no
Electrum import (conftest puts the module on sys.path).
"""
from __future__ import annotations

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    record_uptime_sample,
    uptime_ratio,
)

WINDOW = 36_000.0   # 10h
BUCKET = 3_600.0    # 1h
NOW = 10_000.0


def test_record_sample_skips_malformed_bucket_values() -> None:
    # Buckets whose value is not a 2-element numeric pair must be dropped, not
    # raise: a string (float() -> ValueError), an empty list (v[0] -> IndexError),
    # and None (v[0] -> TypeError) all exercise the except branch. Keys 10-12 are
    # chosen clear of the current-interval bucket this call folds in (~idx 1, from
    # last_ts below) so we can assert the malformed keys are gone.
    acc = {"buckets": {"10": "notapair", "11": [], "12": None, "9": [10.0, 20.0]},
           "last_ts": NOW - BUCKET, "last_online": True}
    out = record_uptime_sample(acc, NOW, online=True, window_sec=WINDOW,
                               bucket_sec=BUCKET)
    # The one well-formed bucket survives; the malformed ones were skipped.
    assert out["buckets"].get("9") == [10.0, 20.0]
    assert set(out["buckets"]) & {"10", "11", "12"} == set()
    # Still produced a usable accumulator.
    assert out["last_ts"] == NOW and out["last_online"] is True


def test_record_sample_tolerates_malformed_last_ts() -> None:
    # A non-numeric last_ts must degrade to "no prior timestamp" (no interval
    # attributed), not raise. With last_ts unusable there is nothing to fold, so
    # only the current sample's timestamp/state is recorded.
    acc = {"buckets": {}, "last_ts": "not-a-float", "last_online": True}
    out = record_uptime_sample(acc, NOW, online=False, window_sec=WINDOW,
                               bucket_sec=BUCKET)
    assert out["buckets"] == {}          # no interval folded (last_ts was unusable)
    assert out["last_ts"] == NOW and out["last_online"] is False


def test_uptime_ratio_skips_non_integer_bucket_key() -> None:
    # A bucket keyed by a non-integer string (int(k) -> ValueError) is ignored;
    # a valid neighbour still counts, so the ratio reflects only the good bucket.
    acc = {"buckets": {"not-an-int": [5.0, 5.0], "2": [10.0, 20.0]}}
    res = uptime_ratio(acc, NOW, WINDOW, bucket_sec=BUCKET)
    assert res is not None
    online_frac, _observed = res
    assert online_frac == 0.5            # 10/20 from the good bucket alone


def test_uptime_ratio_skips_malformed_bucket_value() -> None:
    # A valid key but a malformed value is skipped cleanly; the good bucket alone
    # determines the ratio. The value must fail on v[0] (None -> TypeError): a
    # value that fails only on v[1] would already have mutated online_sec via the
    # preceding v[0] add, so it would not be a clean skip.
    acc = {"buckets": {"2": [10.0, 20.0], "3": None}}
    res = uptime_ratio(acc, NOW, WINDOW, bucket_sec=BUCKET)
    assert res is not None
    online_frac, _observed = res
    assert online_frac == 0.5            # only the well-formed bucket counted


def test_uptime_ratio_none_when_all_buckets_malformed() -> None:
    # If every bucket is unusable, there is no observed time -> unknown (None),
    # never a divide-by-zero or a spurious 0/0 ratio.
    acc = {"buckets": {"1": "x", "2": None, "bad": [1.0, 2.0]}}
    assert uptime_ratio(acc, NOW, WINDOW, bucket_sec=BUCKET) is None
