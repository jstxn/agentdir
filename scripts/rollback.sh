#!/usr/bin/env bash
set -euo pipefail

AGENTDIR_REPO="${AGENTDIR_REPO:-jstxn/agentdir}"
AGENTDIR_ROLLBACK_VERSION="${1:-${AGENTDIR_ROLLBACK_VERSION:-v0.9.0}}"
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

version_matches() {
  local output="$1"
  local expected="${2#v}"
  [ -n "$expected" ] || return 1
  local escaped
  escaped="$(printf '%s' "$expected" | sed 's/[.[\*^$+?(){}|\\]/\\&/g')"
  printf '%s\n' "$output" | grep -Eq "(^|[^0-9A-Za-z._+-])v?${escaped}($|[^0-9A-Za-z._+-])"
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

  local script_dir=""
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  fi

  local install_script=""
  if [ -n "$script_dir" ] && [ -f "$script_dir/install.sh" ]; then
    install_script="$script_dir/install.sh"
    log "using local installer $install_script"
  else
    install_script="$tmp_dir/install-agentdir.sh"
    local installer_ref="${AGENTDIR_INSTALLER_REF:-main}"
    local raw_url="https://raw.githubusercontent.com/$AGENTDIR_REPO/$installer_ref/scripts/install.sh"
    log "fetching installer from $AGENTDIR_REPO@$installer_ref"
    if ! { command -v curl >/dev/null 2>&1 && curl -fsSL -o "$install_script" "$raw_url"; }; then
      need_cmd gh
      gh auth status >/dev/null 2>&1 || fail "gh is not authenticated; run: gh auth login"
      gh api -H "Accept: application/vnd.github.raw" \
        "repos/$AGENTDIR_REPO/contents/scripts/install.sh?ref=$installer_ref" \
        > "$install_script"
    fi
    chmod +x "$install_script"
  fi

  log "reinstalling AgentDir $AGENTDIR_ROLLBACK_VERSION"
  AGENTDIR_VERSION="$AGENTDIR_ROLLBACK_VERSION" AGENTDIR_WHEEL= bash "$install_script"

  local agentdir_bin="${AGENTDIR_PREFIX:-$HOME/.local}/bin/agentdir"
  local resolved_bin=""
  if [ -x "$agentdir_bin" ]; then
    resolved_bin="$agentdir_bin"
  elif command -v agentdir >/dev/null 2>&1; then
    resolved_bin="$(command -v agentdir)"
  else
    fail "rollback installed, but agentdir was not found on PATH or at $agentdir_bin"
  fi

  local installed_version=""
  if installed_version="$("$resolved_bin" --version 2>/dev/null)"; then
    version_matches "$installed_version" "$AGENTDIR_ROLLBACK_VERSION" ||
      fail "rollback version mismatch: expected $AGENTDIR_ROLLBACK_VERSION, got ${installed_version:-<empty>}"
  else
    "$resolved_bin" --help >/dev/null
  fi

  log "rollback completed to $AGENTDIR_ROLLBACK_VERSION"
}

main "$@"
