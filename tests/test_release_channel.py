"""Guards for the beta release channel.

"A beta build is not offered to ordinary users" is a property with no single
owner: it is produced by three things agreeing, two of which live outside the
Python package and would otherwise be tested by nothing at all.

  1. the release workflow publishes a suffixed tag (``v0.4.0-beta.1``) with
     ``--prerelease``;
  2. GitHub's ``releases/latest`` -- the endpoint the plugin polls -- excludes
     pre-releases; and
  3. ``extract_release`` independently drops any payload flagged ``prerelease``,
     so a changed endpoint cannot re-expose one (covered in
     ``test_update_check``).

Drop the flag in (1) and the next beta tag notifies every user who opted into
update checks -- with a green test suite, because nothing else looks at the
workflow. Hence these.
"""
from __future__ import annotations

import os
import re

import pytest

WORKFLOW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".github", "workflows", "release.yml")


def _workflow_text() -> str:
    with open(WORKFLOW, encoding="utf-8") as f:
        return f.read()


def _release_step_script() -> str:
    """The shell body of the "Create GitHub Release" step."""
    yaml = pytest.importorskip("yaml")
    with open(WORKFLOW, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == "Create GitHub Release":
                return step["run"]
    pytest.fail("no 'Create GitHub Release' step in the workflow")


def test_release_step_can_publish_a_prerelease() -> None:
    script = _release_step_script()
    assert "--prerelease" in script, (
        "the release step no longer passes --prerelease; a beta tag would be "
        "published as a normal release and offered to every update-check user")


def test_prerelease_is_decided_by_a_suffix_on_the_tag() -> None:
    """Semver's own test: a hyphen after the version core marks a pre-release,
    so v0.4.0 ships normally and v0.4.0-beta.1 does not. The guard is that the
    flag is CONDITIONAL -- an unconditional --prerelease would be just as broken
    in the other direction, silently never publishing a real release."""
    script = _release_step_script()
    assert re.search(r"\*-\*\s*\)", script), (
        "the prerelease flag is no longer gated on a hyphenated tag")
    # Both arms must exist: one setting the flag, one clearing it.
    assert re.search(r'prerelease="--prerelease"', script)
    assert re.search(r'prerelease=""', script)


def test_beta_branch_is_built_by_ci() -> None:
    """A release channel nobody tests is worse than no channel."""
    yaml = pytest.importorskip("yaml")
    with open(WORKFLOW, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    # PyYAML parses a bare `on:` key as the boolean True.
    triggers = doc.get("on") or doc.get(True)
    assert "beta" in triggers["push"]["branches"]
    assert "beta" in triggers["pull_request"]["branches"]
    assert "v*" in triggers["push"]["tags"]


def test_update_check_polls_the_endpoint_that_hides_prereleases() -> None:
    """The other half of the mechanism. ``releases/latest`` is documented to
    exclude drafts and pre-releases; polling plain ``/releases`` instead would
    hand the plugin the beta as the newest thing available."""
    pytest.importorskip("electrum.plugins.inbound_liquidity")
    from electrum.plugins.inbound_liquidity import UPDATE_CHECK_URL  # type: ignore

    assert UPDATE_CHECK_URL.endswith("/releases/latest")


def test_a_beta_tag_compares_as_its_base_version() -> None:
    """``parse_version`` matches a numeric PREFIX, so the suffix is ignored for
    ordering. Pinned because it is the reason a beta tag is safe to publish at
    all: a user on 0.3.0 who installs it by hand is on 0.4.0 as far as the
    update check is concerned, not on something unorderable.

    The flip side, accepted deliberately: a beta user is NOT told when the final
    v0.4.0 ships, because the two compare equal. Betas are installed by hand, so
    the person who opted in is the person who can opt back out.
    """
    from liquidity_manager import is_newer_version, parse_version  # type: ignore

    assert parse_version("v0.4.0-beta.1") == (0, 4, 0)
    assert is_newer_version("0.4.0-beta.1", "0.3.0") is True
    assert is_newer_version("0.4.0", "0.4.0-beta.1") is False
