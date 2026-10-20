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

The channel's other half is how a beta gets cut at all. Stable is tagged by
hand; beta is published by PUSHING CODE to the `beta` branch, which arrives with
no tag, so the workflow derives one (``v0.5.0-beta.4``) from the manifest version
and the tags that already exist. That derivation, the warning the resulting page
carries, and the pruning of the betas it replaces are each exercised below by
running the workflow's own shell against stub ``gh`` and ``git`` commands.
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
if [ "${1:-}" = "release" ] && [ "${2:-}" = "create" ]; then
  # Stash the body the step assembled, so a test can read what the release page
  # would actually have said rather than only which flags were passed.
  prev=""
  for arg in "$@"; do
    if [ "$prev" = "--notes-file" ]; then
      cp "$arg" "$GH_STUB_NOTES"
    fi
    prev="$arg"
  done
  exit 0
fi
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
        TAG=tag,
        GH_STUB_LOG=str(log),
        GH_STUB_PRERELEASES=str(listing),
        GH_STUB_NOTES=str(tmp_path / "notes-unused.md"),
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


def test_prune_step_is_gated_on_a_beta_release() -> None:
    """The gate is the RESOLVED tag's own classification, not the ref: a beta
    cut from a push to the branch has no tag in its ref, and gating on
    refs/tags/ would silently stop pruning for the automatic channel -- the one
    that generates the betas in the first place.

    Still load-bearing in the other direction: without the -beta test, cutting a
    real v0.5.0 would delete the release list's pre-releases as a side effect.
    `is_beta` is computed by the resolve step from the tag, and is false for a
    stable one."""
    cond = _step(PRUNE_STEP)["if"]
    assert "steps.tag.outputs.is_beta" in cond
    assert "true" in cond
    assert "refs/tags" not in cond, (
        "the prune step is gated on a tag ref again; a beta cut by pushing to "
        "the beta branch would never retire its predecessor")


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


# ---------------------------------------------------------------------------
# What the release page itself says.
#
# --prerelease keeps a beta out of the update check, and nothing more. It does
# nothing for somebody who has already landed on the Releases page, where the
# only thing marking the build is a grey "Pre-release" chip beside the title. So
# the body carries the warning, and it is asserted by running the publish step's
# shell for real and reading the notes file it handed to `gh`.
# ---------------------------------------------------------------------------

RELEASE_STEP = "Create GitHub Release"

_SERVER_URL = "https://github.example"
_REPOSITORY = "BareBits/electrum_liquidity"
# Stands in for github.sha: the commit a derived tag must be created on.
_TARGET_SHA = "0123456789abcdef0123456789abcdef01234567"


def _run_release(tmp_path: Path, *, tag: str,
                 version: str) -> "tuple[subprocess.CompletedProcess[str], List[str], str]":
    """Run the publish step's shell body with `gh` stubbed out.

    Returns the completed process, the stub's call log, and the release notes
    body the step assembled.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "gh"
    stub.write_text(_GH_STUB, encoding="utf-8")
    stub.chmod(0o755)

    zip_path = tmp_path / f"inbound_liquidity-{version}.zip"
    zip_path.write_bytes(b"PK\x05\x06" + b"\0" * 18)  # an empty but valid zip

    log = tmp_path / "gh-calls.log"
    log.write_text("", encoding="utf-8")
    notes = tmp_path / "published-notes.md"

    script = tmp_path / "publish.sh"
    script.write_text(_step(RELEASE_STEP)["run"], encoding="utf-8")

    env = dict(os.environ)
    env.update(
        PATH=f"{bindir}{os.pathsep}{env['PATH']}",
        TAG=tag,
        TARGET=_TARGET_SHA,
        GH_STUB_LOG=str(log),
        GH_STUB_NOTES=str(notes),
        GH_STUB_PRERELEASES=str(tmp_path / "unused.txt"),
        GH_TOKEN="stub-token",
        VERSION=version,
        ZIP=str(zip_path),
        SERVER_URL=_SERVER_URL,
        REPOSITORY=_REPOSITORY,
    )
    result = subprocess.run(
        ["bash", "-e", str(script)], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=60,
        preexec_fn=_limit_address_space,
    )
    body = notes.read_text(encoding="utf-8") if notes.exists() else ""
    return result, log.read_text(encoding="utf-8").splitlines() or [], body


def test_publish_step_body_needs_no_workflow_interpolation() -> None:
    """The tests below execute this step's shell verbatim, which is only sound
    while every `${{ }}` it needs is bound in `env:` instead of substituted into
    the script. A new interpolation would make them test a different program
    than CI runs."""
    step = _step(RELEASE_STEP)
    assert "${{" not in step["run"], (
        "the publish step interpolates a workflow expression into its script; "
        "bind it in the step's env: instead")
    for name in ("TAG", "TARGET", "VERSION", "ZIP", "SERVER_URL", "REPOSITORY",
                 "GH_TOKEN"):
        assert name in step["env"], f"{name} is no longer bound in the step's env"


def test_a_beta_page_warns_that_the_build_is_unstable(tmp_path: Path) -> None:
    """The ask, stated as the operator stated it: a beta page must say the build
    is unstable and should not be used."""
    result, calls, body = _run_release(tmp_path, tag="v0.4.0-beta.8",
                                       version="0.4.0-beta.8")
    assert result.returncode == 0, result.stderr
    assert any(c.startswith("release create ") for c in calls), calls

    lowered = body.lower()
    # The operator's own sentence, asserted close to verbatim. Pinned as one
    # string rather than as keywords so a well-meaning rewrite that drops
    # "funds loss" or the pointer to stable has to come through this test.
    assert ("warning: this is a beta release and should not be installed as it "
            "is unstable and may result in funds loss, use the "
            "[latest stable release]") in lowered
    assert "unstable" in lowered
    # GitHub's own callout syntax, so the warning renders as a red admonition
    # rather than as an ordinary blockquote somebody can skim past.
    assert "> [!WARNING]" in body
    # And a way out: the stable release the reader almost certainly wants,
    # linked from that sentence rather than left as a bare URL below it.
    assert f"]({_SERVER_URL}/{_REPOSITORY}/releases/latest)" in body
    # The warning goes FIRST. Below the install instructions it is furniture.
    assert body.index("[!WARNING]") < body.index("Install by importing")
    # Still a pre-release, still says which version it is.
    assert "--prerelease" in " ".join(calls)
    assert "v0.4.0-beta.8" in body


def test_a_stable_release_page_carries_no_warning(tmp_path: Path) -> None:
    """The other arm, and the one a careless conditional breaks silently: a real
    release must not tell users not to install it."""
    result, calls, body = _run_release(tmp_path, tag="v0.4.0", version="0.4.0")
    assert result.returncode == 0, result.stderr
    assert any(c.startswith("release create ") for c in calls), calls

    assert "[!WARNING]" not in body
    assert "unstable" not in body.lower()
    assert "--prerelease" not in " ".join(calls)
    assert body.strip() == (
        "Inbound Liquidity Manager plugin v0.4.0. Install by importing the "
        "attached zip into Electrum's plugin manager.")


def test_generated_notes_are_still_appended(tmp_path: Path) -> None:
    """--generate-notes is what produces the "What's Changed" list. It is passed
    alongside --notes-file, not replaced by it."""
    result, calls, _ = _run_release(tmp_path, tag="v0.4.0", version="0.4.0")
    assert result.returncode == 0, result.stderr
    create = next(c for c in calls if c.startswith("release create "))
    assert "--generate-notes" in create
    assert "--notes-file" in create


def test_release_step_pins_the_tag_to_the_pushed_commit(tmp_path: Path) -> None:
    """A derived tag does not exist yet, and `gh release create` puts a missing
    tag on the repository's DEFAULT branch unless told otherwise. Without
    --target, a beta cut from the beta branch would be a zip of beta's code
    published against a tag pointing at main."""
    result, calls, _ = _run_release(tmp_path, tag="v0.5.0-beta.1",
                                    version="0.5.0")
    assert result.returncode == 0, result.stderr
    create = next(c for c in calls if c.startswith("release create "))
    assert f"--target {_TARGET_SHA}" in create


def test_release_is_skipped_when_no_tag_was_resolved() -> None:
    """The publish step keys off the resolved tag, so the ref test lives in one
    place (the resolve step) instead of being restated here and drifting."""
    cond = _step(RELEASE_STEP)["if"]
    assert "steps.tag.outputs.tag" in cond


# ---------------------------------------------------------------------------
# Where a beta's tag comes from.
#
# The stable channel is cut by pushing a tag. The beta channel is cut by pushing
# CODE: a push to `beta` carries no tag, so the workflow derives one -- the
# manifest version plus the next free -beta.N. That derivation IS the automatic
# channel; if it misfires the channel either publishes nothing or republishes a
# number it already used, so the shell is executed for real here against a stub
# `git` that serves a fixed tag list.
# ---------------------------------------------------------------------------

RESOLVE_STEP = "Resolve the release tag"

_GIT_STUB = r"""#!/usr/bin/env bash
# Stands in for git. Serves `tag --list <pattern>` out of GIT_STUB_TAGS, doing
# the glob match git would do, so a test controls exactly which tags exist.
set -eu
printf '%s\n' "$*" >> "$GIT_STUB_LOG"
if [ "${1:-}" = "tag" ] && [ "${2:-}" = "--list" ]; then
  pattern="${3:-*}"
  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    # Unquoted on purpose: this is the glob match.
    case "$candidate" in
      $pattern) printf '%s\n' "$candidate" ;;
    esac
  done < "$GIT_STUB_TAGS"
  exit 0
fi
echo "stub git: unexpected invocation: $*" >&2
exit 65
"""


def _run_resolve(tmp_path: Path, *, version: str,
                 tags: Sequence[str],
                 ref_type: str = "branch",
                 ref_name: str = "beta") -> "tuple[subprocess.CompletedProcess[str], Dict[str, str], List[str]]":
    """Run the resolve step's shell body with `git` stubbed out.

    Returns the completed process, the step outputs it wrote to $GITHUB_OUTPUT,
    and the stub's call log.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "git"
    stub.write_text(_GIT_STUB, encoding="utf-8")
    stub.chmod(0o755)

    tagfile = tmp_path / "tags.txt"
    tagfile.write_text("".join(f"{t}\n" for t in tags), encoding="utf-8")
    log = tmp_path / "git-calls.log"
    log.write_text("", encoding="utf-8")
    outputs = tmp_path / "github-output.txt"
    outputs.write_text("", encoding="utf-8")

    script = tmp_path / "resolve.sh"
    script.write_text(_step(RESOLVE_STEP)["run"], encoding="utf-8")

    env = dict(os.environ)
    env.update(
        PATH=f"{bindir}{os.pathsep}{env['PATH']}",
        VERSION=version,
        REF_TYPE=ref_type,
        REF_NAME=ref_name,
        GIT_STUB_TAGS=str(tagfile),
        GIT_STUB_LOG=str(log),
        GITHUB_OUTPUT=str(outputs),
    )
    result = subprocess.run(
        ["bash", "-e", str(script)], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=60,
        preexec_fn=_limit_address_space,
    )
    parsed: Dict[str, str] = {}
    for line in outputs.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            parsed[key] = value
    return result, parsed, log.read_text(encoding="utf-8").splitlines()


def _resolved(tmp_path: Path, **kwargs: Any) -> Dict[str, str]:
    result, outputs, _ = _run_resolve(tmp_path, **kwargs)
    assert result.returncode == 0, result.stderr
    return outputs


def test_resolve_step_needs_no_workflow_interpolation() -> None:
    """Same contract as the publish step: the tests below run this shell
    verbatim, which is only sound while nothing is substituted into it."""
    step = _step(RESOLVE_STEP)
    assert "${{" not in step["run"]
    for name in ("VERSION", "REF_TYPE", "REF_NAME"):
        assert name in step["env"], f"{name} is no longer bound in the step's env"


def test_resolve_runs_for_a_pushed_tag_or_the_beta_branch() -> None:
    """The gate on the whole release path. `beta` is named explicitly, so a
    push to main (or any other branch) builds the zip and publishes nothing."""
    cond = _step(RESOLVE_STEP)["if"]
    assert "refs/heads/beta" in cond
    assert "tag" in cond
    assert "refs/heads/main" not in cond


def test_a_push_to_beta_with_no_existing_beta_starts_at_one(tmp_path: Path) -> None:
    outputs = _resolved(tmp_path, version="0.5.0", tags=["v0.4.0", "v0.3.0"])
    assert outputs["tag"] == "v0.5.0-beta.1"
    assert outputs["is_beta"] == "true"


def test_a_push_to_beta_takes_the_next_number(tmp_path: Path) -> None:
    """The actual ask: pushing code to beta cuts the next beta."""
    outputs = _resolved(
        tmp_path, version="0.5.0",
        tags=["v0.5.0-beta.1", "v0.5.0-beta.2", "v0.5.0-beta.3"])
    assert outputs["tag"] == "v0.5.0-beta.4"


def test_the_number_is_compared_as_an_integer_not_a_string(tmp_path: Path) -> None:
    """beta.10 sorts BEFORE beta.9 as text. A lexical maximum would hand out
    beta.10 a second time and the publish would collide with the live tag."""
    outputs = _resolved(
        tmp_path, version="0.5.0",
        tags=["v0.5.0-beta.9", "v0.5.0-beta.10"])
    assert outputs["tag"] == "v0.5.0-beta.11"


def test_derivation_only_counts_this_version_and_this_channel(tmp_path: Path) -> None:
    """A previous version's betas and a different pre-release channel are not
    this counter's business. Asserted through the stub's glob, so the narrowing
    is git's own pattern rather than a filter applied afterwards."""
    result, outputs, calls = _run_resolve(
        tmp_path, version="0.5.0",
        tags=["v0.4.0-beta.7", "v0.5.0-rc.3", "v0.5.0-beta.2", "v0.6.0-beta.9"])
    assert result.returncode == 0, result.stderr
    assert outputs["tag"] == "v0.5.0-beta.3"
    assert any("tag --list v0.5.0-beta.*" in c for c in calls), calls


def test_derivation_ignores_a_tag_whose_suffix_is_not_a_number(tmp_path: Path) -> None:
    """`v0.5.0-beta.4-fix` must not read as 4 -- that would make the next tag
    beta.5 while beta.4 is the newest real build, skipping a number, or worse
    read as garbage and fail the arithmetic under `set -u`."""
    outputs = _resolved(
        tmp_path, version="0.5.0",
        tags=["v0.5.0-beta.2", "v0.5.0-beta.4-fix", "v0.5.0-beta.x"])
    assert outputs["tag"] == "v0.5.0-beta.3"


def test_a_pushed_tag_is_released_verbatim(tmp_path: Path) -> None:
    """The stable path, untouched: a tag push releases exactly that tag, with no
    derivation and no git lookup at all."""
    result, outputs, calls = _run_resolve(
        tmp_path, version="0.5.0", tags=["v0.5.0-beta.1"],
        ref_type="tag", ref_name="v0.5.0")
    assert result.returncode == 0, result.stderr
    assert outputs["tag"] == "v0.5.0"
    assert outputs["is_beta"] == "false"
    assert calls == [], f"the stable path consulted git: {calls}"


def test_a_hand_pushed_beta_tag_is_still_a_beta(tmp_path: Path) -> None:
    """is_beta is computed from the resolved tag, so the prune step covers a
    beta cut by hand as well as a derived one."""
    outputs = _resolved(tmp_path, version="0.5.0", tags=[],
                        ref_type="tag", ref_name="v0.5.0-beta.6")
    assert outputs["tag"] == "v0.5.0-beta.6"
    assert outputs["is_beta"] == "true"


def test_an_rc_tag_is_a_prerelease_but_not_a_beta(tmp_path: Path) -> None:
    """The two classifications are deliberately different: the publish step
    treats any hyphen as a pre-release, while is_beta -- which authorises
    DELETIONS -- is narrower."""
    outputs = _resolved(tmp_path, version="0.5.0", tags=[],
                        ref_type="tag", ref_name="v0.5.0-rc.1")
    assert outputs["is_beta"] == "false"


def test_the_build_checkout_fetches_tags() -> None:
    """The counter is read off the tags in the checkout. actions/checkout is
    shallow by default and fetches none, which would make every push to beta
    derive beta.1 and collide with the live release."""
    with_ = _step("Checkout").get("with") or {}
    assert with_.get("fetch-depth") == 0, (
        "the build checkout is shallow again; the -beta.N counter would reset "
        "to 1 on every run")
