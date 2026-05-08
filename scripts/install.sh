#!/usr/bin/env bash
set -euo pipefail

AGENTDIR_VERSION="${AGENTDIR_VERSION:-v0.3.1}"
AGENTDIR_REPO="${AGENTDIR_REPO:-jstxn/agentdir}"
AGENTDIR_PACKAGE_VERSION="${AGENTDIR_VERSION#v}"
AGENTDIR_WHEEL_NAME="agentdir-${AGENTDIR_PACKAGE_VERSION}-py3-none-any.whl"
AGENTDIR_PREFIX="${AGENTDIR_PREFIX:-$HOME/.local}"
AGENTDIR_HOME="${AGENTDIR_HOME:-$HOME/.local/share/agentdir}"
AGENTDIR_TMP_DIR=""

log() {
  printf '[agentdir-install] %s\n' "$*" >&2
}

fail() {
  printf '[agentdir-install] error: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

download_wheel() {
  local target_dir="$1"
  if [ -n "${AGENTDIR_WHEEL:-}" ]; then
    [ -f "$AGENTDIR_WHEEL" ] || fail "AGENTDIR_WHEEL does not exist: $AGENTDIR_WHEEL"
    printf '%s\n' "$AGENTDIR_WHEEL"
    return
  fi

  need_cmd gh
  gh auth status >/dev/null 2>&1 || fail "gh is not authenticated; run: gh auth login"
  log "downloading $AGENTDIR_WHEEL_NAME from $AGENTDIR_REPO@$AGENTDIR_VERSION"
  gh release download "$AGENTDIR_VERSION" \
    --repo "$AGENTDIR_REPO" \
    --pattern "$AGENTDIR_WHEEL_NAME" \
    --dir "$target_dir" >/dev/null
  printf '%s\n' "$target_dir/$AGENTDIR_WHEEL_NAME"
}

install_with_pipx() {
  local wheel="$1"
  command -v pipx >/dev/null 2>&1 || return 1
  log "installing with pipx"
  pipx install --force "$wheel"
}

install_with_venv() {
  local wheel="$1"
  local venv="$AGENTDIR_HOME/venv"
  need_cmd python3
  log "installing into $venv"
  python3 -m venv "$venv"
  "$venv/bin/python" -m pip install --upgrade pip >/dev/null
  "$venv/bin/python" -m pip install --force-reinstall "$wheel" >/dev/null
  mkdir -p "$AGENTDIR_PREFIX/bin"
  ln -sf "$venv/bin/agentdir" "$AGENTDIR_PREFIX/bin/agentdir"
  log "linked $AGENTDIR_PREFIX/bin/agentdir"
}

main() {
  need_cmd python3
  local tmp_dir
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/agentdir-install.XXXXXX")"
  AGENTDIR_TMP_DIR="$tmp_dir"
  trap 'rm -rf "$AGENTDIR_TMP_DIR"' EXIT

  local wheel
  wheel="$(download_wheel "$tmp_dir")"
  if [ "${AGENTDIR_FORCE_VENV:-0}" != "1" ] && install_with_pipx "$wheel"; then
    :
  else
    install_with_venv "$wheel"
  fi

  if ! command -v agentdir >/dev/null 2>&1; then
    log "agentdir installed, but not found on PATH"
    log "add this to PATH: $AGENTDIR_PREFIX/bin"
    "$AGENTDIR_PREFIX/bin/agentdir" --help >/dev/null
  else
    agentdir --help >/dev/null
  fi
  log "installed AgentDir $AGENTDIR_VERSION"
}

main "$@"
