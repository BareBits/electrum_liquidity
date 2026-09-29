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
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest

WORKFLOW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".github", "workflows", "release.yml")

PRUNE_STEP = "Delete previous beta releases"

# Address space ceiling for the shell subprocesses these tests spawn. They run
# bash and a stub `gh`; anything that starts allocating in earnest is a bug, not
# a slow test.
_SUBPROCESS_AS_LIMIT = 512 * 1024 * 1024


def _workflow_text() -> str:
    with open(WORKFLOW, encoding="utf-8") as f:
        return f.read()


def _build_steps() -> List[Dict[str, Any]]:
    """Steps of the job that publishes releases, in order."""
    yaml = pytest.importorskip("yaml")
    with open(WORKFLOW, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    for job in doc["jobs"].values():
        steps = job.get("steps", [])
        if any(s.get("name") == "Create GitHub Release" for s in steps):
            return steps
    pytest.fail("no 'Create GitHub Release' step in the workflow")


def _step(name: str) -> Dict[str, Any]:
    for step in _build_steps():
        if step.get("name") == name:
            return step
    pytest.fail(f"no {name!r} step in the workflow")


def _release_step_script() -> str:
    """The shell body of the "Create GitHub Release" step."""
    return _step("Create GitHub Release")["run"]


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


# ---------------------------------------------------------------------------
# Publishing a beta retires the older ones.
#
# The beta channel is meant to hold exactly one installable build. The risk in
# automating that is not the deletion that happens, it is the one that happens
# by mistake: this step runs with contents:write against the release list, and a
# loosened name test would have it deleting real releases. So the shell below is
# exercised for real -- against a stub `gh` that records what it was asked to
# delete -- rather than only grepped.
# ---------------------------------------------------------------------------

_GH_STUB = r"""#!/usr/bin/env bash
# Stands in for gh. Selecting pre-releases out of `release list` is gh's own
# work (it applies --jq internally), so the stub asserts that the filter was
# asked for and then prints what a real gh would have returned: the tag names in
# GH_STUB_PRERELEASES. Every invocation is appended to GH_STUB_LOG verbatim.
set -eu
printf '%s\n' "$*" >> "$GH_STUB_LOG"
if [ "${1:-}" = "release" ] && [ "${2:-}" = "list" ]; then
  case "$*" in
    *"select(.isPrerelease)"*) ;;
    *) echo "stub gh: 'release list' did not filter on isPrerelease: $*" >&2
       exit 64 ;;
  esac
  cat "$GH_STUB_PRERELEASES"
  exit 0
fi
if [ "${1:-}" = "release" ] && [ "${2:-}" = "delete" ]; then
  exit 0
fi
echo "stub gh: unexpected invocation: $*" >&2
exit 65
"""


def _limit_address_space() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (_SUBPROCESS_AS_LIMIT, _SUBPROCESS_AS_LIMIT))


def _run_prune(tmp_path: Path, *, tag: str,
               prereleases: Sequence[str],
               listing_readable: bool = True) -> "tuple[subprocess.CompletedProcess[str], List[str]]":
    """Run the prune step's shell body with `gh` stubbed out.

    Returns the completed process and the stub's call log (one line per gh
    invocation, argv joined by spaces).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "gh"
    stub.write_text(_GH_STUB, encoding="utf-8")
    stub.chmod(0o755)

    listing = tmp_path / "prereleases.txt"
    if listing_readable:
        listing.write_text("".join(f"{t}\n" for t in prereleases), encoding="utf-8")
    log = tmp_path / "gh-calls.log"
    log.write_text("", encoding="utf-8")

    script = tmp_path / "prune.sh"
    script.write_text(_step(PRUNE_STEP)["run"], encoding="utf-8")

    env = dict(os.environ)
    env.update(
        PATH=f"{bindir}{os.pathsep}{env['PATH']}",
        GITHUB_REF_NAME=tag,
        GH_STUB_LOG=str(log),
        GH_STUB_PRERELEASES=str(listing),
        GH_TOKEN="stub-token",
    )
    # `bash -e <file>` is how Actions runs a `run:` block's default shell.
    result = subprocess.run(
        ["bash", "-e", str(script)], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=60,
        preexec_fn=_limit_address_space,
    )
    return result, log.read_text(encoding="utf-8").splitlines()


def _deleted(calls: Sequence[str]) -> List[str]:
    return [c.split()[2] for c in calls if c.startswith("release delete ")]


def test_prune_step_is_gated_on_a_beta_tag() -> None:
    """Two conditions, both load-bearing: a tag push (not a branch build), and a
    -beta.* tag specifically. Without the second, cutting a real v0.4.0 would
    delete the release list's pre-releases as a side effect."""
    cond = _step(PRUNE_STEP)["if"]
    assert "refs/tags/v" in cond
    assert "-beta." in cond and "github.ref_name" in cond


def test_prune_runs_after_the_release_it_replaces_the_betas_with() -> None:
    """Order matters: if the publish fails, the job stops and the existing betas
    are still installable. Pruning first would leave the channel empty."""
    names = [s.get("name") for s in _build_steps()]
    assert names.index(PRUNE_STEP) > names.index("Create GitHub Release")


def test_prune_removes_the_tag_along_with_the_release() -> None:
    script = _step(PRUNE_STEP)["run"]
    assert "--cleanup-tag" in script, "the retired beta's tag would be left behind"
    assert "--yes" in script, "gh release delete would block on a confirmation prompt"


def test_publishing_a_beta_deletes_every_older_beta(tmp_path: Path) -> None:
    """The actual ask. Also covers, via the stub's own assertion, that the
    listing is filtered to pre-releases before any name test is applied."""
    result, calls = _run_prune(
        tmp_path, tag="v0.4.0-beta.3",
        prereleases=["v0.4.0-beta.3", "v0.4.0-beta.2", "v0.4.0-beta.1", "v0.3.0-beta.7"],
    )
    assert result.returncode == 0, result.stderr
    assert _deleted(calls) == ["v0.4.0-beta.2", "v0.4.0-beta.1", "v0.3.0-beta.7"]
    # Deleted with the tag, every time -- not just in the first call.
    for call in calls:
        if call.startswith("release delete "):
            assert "--cleanup-tag" in call and "--yes" in call


def test_prune_keeps_the_beta_just_published(tmp_path: Path) -> None:
    """The new release is in the list it reads; deleting it would make the
    workflow's own output disappear."""
    result, calls = _run_prune(tmp_path, tag="v0.4.0-beta.1",
                               prereleases=["v0.4.0-beta.1"])
    assert result.returncode == 0, result.stderr
    assert _deleted(calls) == []


def test_prune_leaves_other_prerelease_channels_alone(tmp_path: Path) -> None:
    """A hyphenated tag is not automatically a beta. An -rc build is a
    pre-release too, and is not this step's business."""
    result, calls = _run_prune(
        tmp_path, tag="v0.5.0-beta.1",
        prereleases=["v0.5.0-beta.1", "v0.4.0-rc.1", "v0.4.0-alpha.2", "v0.4.0-beta.9"],
    )
    assert result.returncode == 0, result.stderr
    assert _deleted(calls) == ["v0.4.0-beta.9"]


def test_prune_is_a_noop_when_there_are_no_other_releases(tmp_path: Path) -> None:
    result, calls = _run_prune(tmp_path, tag="v0.4.0-beta.1", prereleases=[])
    assert result.returncode == 0, result.stderr
    assert _deleted(calls) == []


def test_a_failed_listing_fails_the_step(tmp_path: Path) -> None:
    """If `gh release list` errors, the step must fail rather than treat "no
    output" as "nothing to prune" -- a silent no-op here is how stale betas
    accumulate unnoticed."""
    result, calls = _run_prune(tmp_path, tag="v0.4.0-beta.1", prereleases=[],
                               listing_readable=False)
    assert result.returncode != 0
    assert _deleted(calls) == []
