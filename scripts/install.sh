#!/usr/bin/env bash
set -euo pipefail

AGENTDIR_VERSION="${AGENTDIR_VERSION:-v0.7.0}"
AGENTDIR_REPO="${AGENTDIR_REPO:-jstxn/agentdir}"
AGENTDIR_PACKAGE_VERSION="${AGENTDIR_VERSION#v}"
AGENTDIR_WHEEL_NAME="agentdir-${AGENTDIR_PACKAGE_VERSION}-py3-none-any.whl"
AGENTDIR_PREFIX="${AGENTDIR_PREFIX:-$HOME/.local}"
AGENTDIR_HOME="${AGENTDIR_HOME:-$HOME/.local/share/agentdir}"
AGENTDIR_MIN_PYTHON_MAJOR=3
AGENTDIR_MIN_PYTHON_MINOR=11
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

python_is_supported() {
  local python_cmd="$1"
  "$python_cmd" - "$AGENTDIR_MIN_PYTHON_MAJOR" "$AGENTDIR_MIN_PYTHON_MINOR" <<'PY'
import sys

required = (int(sys.argv[1]), int(sys.argv[2]))
raise SystemExit(0 if sys.version_info[:2] >= required else 1)
PY
}

find_python() {
  if [ -n "${AGENTDIR_PYTHON:-}" ]; then
    command -v "$AGENTDIR_PYTHON" >/dev/null 2>&1 || fail "AGENTDIR_PYTHON not found: $AGENTDIR_PYTHON"
    python_is_supported "$AGENTDIR_PYTHON" || fail "AGENTDIR_PYTHON must be Python 3.11 or newer"
    printf '%s\n' "$AGENTDIR_PYTHON"
    return
  fi

  local candidate
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_is_supported "$candidate"; then
      command -v "$candidate"
      return
    fi
  done

  fail "missing Python 3.11 or newer; set AGENTDIR_PYTHON=/path/to/python3.11+"
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
  local python_cmd="$2"
  local venv="$AGENTDIR_HOME/venv"
  log "installing into $venv"
  "$python_cmd" -m venv "$venv"
  "$venv/bin/python" -m pip install --upgrade pip >/dev/null
  "$venv/bin/python" -m pip install --force-reinstall "$wheel" >/dev/null
  mkdir -p "$AGENTDIR_PREFIX/bin"
  ln -sf "$venv/bin/agentdir" "$AGENTDIR_PREFIX/bin/agentdir"
  log "linked $AGENTDIR_PREFIX/bin/agentdir"
}

main() {
  local python_cmd
  python_cmd="$(find_python)"
  local tmp_dir
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/agentdir-install.XXXXXX")"
  AGENTDIR_TMP_DIR="$tmp_dir"
  trap 'rm -rf "$AGENTDIR_TMP_DIR"' EXIT

  local wheel
  wheel="$(download_wheel "$tmp_dir")"
  local installed_with_venv=0
  if [ "${AGENTDIR_FORCE_VENV:-0}" != "1" ] && install_with_pipx "$wheel"; then
    installed_with_venv=0
  else
    install_with_venv "$wheel" "$python_cmd"
    installed_with_venv=1
  fi

  local agentdir_bin="$AGENTDIR_PREFIX/bin/agentdir"
  if [ "$installed_with_venv" = "1" ]; then
    [ -x "$agentdir_bin" ] || fail "agentdir executable was not found at $agentdir_bin"
    "$agentdir_bin" --help >/dev/null
  elif command -v agentdir >/dev/null 2>&1; then
    agentdir --help >/dev/null
  else
    log "agentdir installed, but not found on PATH"
    log "add this to PATH: $AGENTDIR_PREFIX/bin"
    fail "agentdir executable was not found on PATH after pipx install"
  fi
  log "installed AgentDir $AGENTDIR_VERSION"
}

main "$@"
