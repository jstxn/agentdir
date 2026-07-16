#!/usr/bin/env bash
set -euo pipefail

AGENTDIR_REPO="${AGENTDIR_REPO:-jstxn/agentdir}"
AGENTDIR_ROLLBACK_VERSION="${1:-${AGENTDIR_ROLLBACK_VERSION:-v0.7.7}}"
AGENTDIR_TMP_DIR=""

log() {
  printf '[agentdir-rollback] %s\n' "$*" >&2
}

fail() {
  printf '[agentdir-rollback] error: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

main() {
  case "$AGENTDIR_ROLLBACK_VERSION" in
    v*) ;;
    *) fail "rollback version must be a tag like v0.4.0" ;;
  esac

  local tmp_dir
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/agentdir-rollback.XXXXXX")"
  AGENTDIR_TMP_DIR="$tmp_dir"
  trap 'rm -rf "$AGENTDIR_TMP_DIR"' EXIT

  local install_script="$tmp_dir/install-agentdir.sh"
  local raw_url="https://raw.githubusercontent.com/$AGENTDIR_REPO/$AGENTDIR_ROLLBACK_VERSION/scripts/install.sh"
  log "fetching installer from $AGENTDIR_REPO@$AGENTDIR_ROLLBACK_VERSION"
  if ! { command -v curl >/dev/null 2>&1 && curl -fsSL -o "$install_script" "$raw_url"; }; then
    need_cmd gh
    gh auth status >/dev/null 2>&1 || fail "gh is not authenticated; run: gh auth login"
    gh release view "$AGENTDIR_ROLLBACK_VERSION" --repo "$AGENTDIR_REPO" >/dev/null ||
      fail "release not found: $AGENTDIR_REPO@$AGENTDIR_ROLLBACK_VERSION"
    gh api -H "Accept: application/vnd.github.raw" \
      "repos/$AGENTDIR_REPO/contents/scripts/install.sh?ref=$AGENTDIR_ROLLBACK_VERSION" \
      > "$install_script"
  fi
  chmod +x "$install_script"

  log "reinstalling AgentDir $AGENTDIR_ROLLBACK_VERSION"
  AGENTDIR_VERSION="$AGENTDIR_ROLLBACK_VERSION" AGENTDIR_WHEEL= bash "$install_script"

  local agentdir_bin="${AGENTDIR_PREFIX:-$HOME/.local}/bin/agentdir"
  if [ -x "$agentdir_bin" ]; then
    "$agentdir_bin" --version >/dev/null 2>&1 || "$agentdir_bin" --help >/dev/null
  elif command -v agentdir >/dev/null 2>&1; then
    agentdir --version >/dev/null 2>&1 || agentdir --help >/dev/null
  else
    fail "rollback installed, but agentdir was not found on PATH or at $agentdir_bin"
  fi

  log "rollback completed to $AGENTDIR_ROLLBACK_VERSION"
}

main "$@"
