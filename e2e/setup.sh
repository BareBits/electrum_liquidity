#!/usr/bin/env bash
# Build everything the electrum_liquidity test rig needs:
#   * a fresh Electrum 4.7.2 source checkout (electrum/),
#   * a dedicated Python venv with Electrum + GUI + nostr deps (.venv-electrum/),
#   * confirms the Fulcrum binary (bin/Fulcrum) and the nostr-rs-relay docker
#     image are present.
#
# Idempotent: an existing checkout/venv is reused. Pass --force to rebuild the
# venv from scratch. Nothing here touches anything outside this project folder
# (the docker image is only *pulled*, never modified).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

ELECTRUM_REPO="${ELECTRUM_REPO:-https://github.com/spesmilo/electrum.git}"
ELECTRUM_REF="${ELECTRUM_REF:-4.7.2}"
ELECTRUM_SRC="$HERE/electrum"
VENV_ELECTRUM="$HERE/.venv-electrum"
FULCRUM_BIN="$HERE/bin/Fulcrum"
NOSTR_IMAGE="${NOSTR_IMAGE:-scsibug/nostr-rs-relay:latest}"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

log() { printf '\033[1;36m[setup]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[setup] %s\033[0m\n' "$*" >&2; exit 1; }

# ---- prerequisites --------------------------------------------------------
command -v bitcoind    >/dev/null || die "bitcoind not found on PATH"
command -v bitcoin-cli >/dev/null || die "bitcoin-cli not found on PATH"
command -v python3     >/dev/null || die "python3 not found on PATH"
command -v git         >/dev/null || die "git not found on PATH"
command -v docker      >/dev/null || die "docker not found on PATH"
[[ -x "$FULCRUM_BIN" ]] || die "Fulcrum binary missing at $FULCRUM_BIN"

log "Bitcoin Core: $(bitcoind --version | head -1)"
log "Fulcrum:      $("$FULCRUM_BIN" --version 2>&1 | head -1)"

# ---- nostr-rs-relay image -------------------------------------------------
if ! docker image inspect "$NOSTR_IMAGE" >/dev/null 2>&1; then
  log "pulling nostr relay image ($NOSTR_IMAGE)"
  docker pull "$NOSTR_IMAGE"
else
  log "nostr relay image present: $NOSTR_IMAGE"
fi

# ---- Electrum source ------------------------------------------------------
if [[ ! -d "$ELECTRUM_SRC/.git" ]]; then
  log "cloning Electrum ($ELECTRUM_REF) -> $ELECTRUM_SRC"
  git clone --depth 1 --branch "$ELECTRUM_REF" "$ELECTRUM_REPO" "$ELECTRUM_SRC"
else
  log "Electrum checkout present: $ELECTRUM_SRC"
fi

# ---- Electrum venv (GUI + nostr) ------------------------------------------
if [[ $FORCE -eq 1 || ! -x "$VENV_ELECTRUM/bin/electrum" ]]; then
  log "building Electrum venv ($VENV_ELECTRUM)"
  rm -rf "$VENV_ELECTRUM"
  python3 -m venv "$VENV_ELECTRUM"
  "$VENV_ELECTRUM/bin/pip" install -q --upgrade pip wheel
  "$VENV_ELECTRUM/bin/pip" install -q -e "$ELECTRUM_SRC"
  "$VENV_ELECTRUM/bin/pip" install -q -r "$ELECTRUM_SRC/contrib/requirements/requirements.txt"
  # Not pulled by the editable install but required at runtime / for the GUI:
  "$VENV_ELECTRUM/bin/pip" install -q cryptography PyQt6
else
  log "Electrum venv present (use --force to rebuild)"
fi

"$VENV_ELECTRUM/bin/python" -c "import electrum, electrum_aionostr; from electrum.version import ELECTRUM_VERSION; print('[setup] electrum', ELECTRUM_VERSION, '+ aionostr OK')"
log "setup complete"
