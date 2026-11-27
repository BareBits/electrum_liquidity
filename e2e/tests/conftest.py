"""Suite-wide settings for the rig-based e2e tests.

Two jobs, both of which exist because of choices made outside this directory:

  * give a rig test a deadline it can actually live within. ``pytest.ini`` at
    the repo root gives every test 300s, which is right for the mocked suite
    and would kill nearly every test in here -- several of these legitimately
    wait 10-20 minutes on a real chain, and individual ``_wait_until`` calls in
    this directory already pass timeouts as high as 1200s.

  * refuse to run rig tests in parallel, because the rig cannot be run in
    parallel (see below) and the failure if you try is baffling rather than
    obvious.

Deliberately NOT here: the ``RUN_RIG_E2E`` gate and the per-file helpers. Each
test module still carries its own module-level skip and its own copies of
``_setcfg`` / ``_mine`` / ``_wait_until`` / ... That duplication is real (28
copies of ``_setcfg``, 10 genuinely different variants of ``_wait_until``) but
unpicking it is a mechanical change across every file in here whose only
possible validation is a multi-hour full-suite run, so it does not belong in
the same commit as anything else.
"""
from __future__ import annotations

import os

import pytest

# A rig test's own budget, generous on purpose. The slowest suites in here
# document ~10-20 minutes, and that is the HAPPY path: the number has to leave
# room for a slow chain or a daemon that needed its startup retry, or the
# deadline becomes a flake generator rather than a hang detector. It is still
# far below the CI job budget, so an actually-wedged test fails with a
# traceback pointing at the line it hung on instead of being killed from
# outside with no diagnostic at all.
RIG_TEST_TIMEOUT_SEC = 2700


def pytest_configure(config: pytest.Config) -> None:
    """Reject ``-n`` when rig tests are in play.

    The rig is a singleton on this machine, by construction and in two
    independent ways: ``paths.RUN_DIR`` is a fixed directory that every launch
    WIPES, and ``paths.NOSTR_CONTAINER`` is a fixed docker container name that
    preflight ``docker rm -f``es. Two concurrent rigs therefore delete each
    other's state and fight over one relay, and what you observe is not "two
    rigs collided" -- it is a daemon that mysteriously lost its wallet, or a
    swap that never sees an offer, somewhere else entirely.

    Making them coexist is a real option (both names could be derived per
    worker, and MARKER_VALUE already follows RUN_DIR, so process scoping would
    come along for free) but it is not the case today, so say so plainly up
    front rather than letting the suite discover it.
    """
    if os.environ.get("RUN_RIG_E2E") != "1":
        return
    parallel = config.getoption("numprocesses", None) or hasattr(config, "workerinput")
    if parallel:
        raise pytest.UsageError(
            "the rig e2e suite cannot run under pytest-xdist: the rig is a "
            "singleton (a fixed .run directory it wipes on launch, and a fixed "
            "nostr container name), so concurrent rigs destroy each other's "
            "state. Drop -n and run this suite serially."
        )


def is_rig_fixture(name: str) -> bool:
    """Whether ``name`` is the suite's convention for a fixture that brings the
    rig up: ``rig`` itself, or a qualified variant (``plain_rig``,
    ``sized_rig``, ``deep_dead_rig``).

    A naming convention is load-bearing here, which is uncomfortable but is the
    only signal available at collection time -- nothing about a fixture's NAME
    can prove it launches bitcoind. So the convention is enforced rather than
    hoped for: ``test_rig_unit.test_every_rig_fixture_follows_the_naming_
    convention`` greps the suite for a fixture that builds a ``run_mod.Rig``
    under some other name, and fails in the FAST suite if one appears. Without
    that, such a fixture would silently inherit the 300s default and its test
    would be killed mid-rig for no visible reason.
    """
    return name == "rig" or name.endswith("_rig")


def pytest_collection_modifyitems(config: pytest.Config,
                                  items: list[pytest.Item]) -> None:
    """Stamp every rig-backed test with the marker and the longer deadline.

    Keyed on the test actually requesting a rig fixture rather than on a
    filename pattern, so a rig test is picked up wherever it lives while the
    fast suites in here keep the ordinary 300s budget -- which for them is a
    real hang detector, and which they would lose to a blanket per-directory
    rule. Those are: ``test_rig_unit`` (rig plumbing, imports the orchestrator
    but launches nothing), ``test_lnurl_stub``, and ``test_multi_wallet_gui_e2e``
    (a real Qt GUI over an offline daemon -- seconds, no services).

    ``fixturenames`` is the resolved CLOSURE, not just what the signature asks
    for, so a test taking a fixture that itself depends on the rig
    (``live_wallet`` in test_goal_buffer_e2e) is covered without naming one.
    """
    for item in items:
        if not any(is_rig_fixture(n) for n in getattr(item, "fixturenames", ())):
            continue
        item.add_marker(pytest.mark.rig_e2e)
        # Only if the module has not asked for something specific itself.
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(RIG_TEST_TIMEOUT_SEC))
