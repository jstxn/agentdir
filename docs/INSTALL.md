# Installing AgentDir

AgentDir is distributed as GitHub Release assets. The project does not require PyPI, npm, GitHub Packages, Redis, Dovecot, or a background service.

## Recommended Install

For the private `jstxn/agentdir` repository, engineers need GitHub CLI access to the repo:

```bash
gh auth login
gh auth status
```

Install with one command:

```bash
gh api -H "Accept: application/vnd.github.raw" \
  'repos/jstxn/agentdir/contents/scripts/install.sh?ref=v0.4.0' | bash
```

The installer downloads the release wheel and installs it with `pipx` when available. If `pipx` is not installed, it falls back to a self-contained virtual environment at:

```text
~/.local/share/agentdir/venv
```

and links the CLI at:

```text
~/.local/bin/agentdir
```

The virtual environment fallback uses Python 3.11 or newer. Set `AGENTDIR_PYTHON=/path/to/python3.11+` when the default `python3` on PATH is older.

If `~/.local/bin` is not on PATH, add it:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Verify Install

```bash
agentdir --help

repo="$(mktemp -d)/agentdir-install-smoke"
mkdir -p "$repo"
git -C "$repo" init
cd "$repo"
agentdir setup --codex-skill store
agentdir session ensure --id install-smoke --title "install smoke"
agentdir run -- python3 -c "print('agentdir install smoke')"
agentdir context build "install smoke"
agentdir memory search "install smoke"
agentdir summarize
agentdir evidence
agentdir doctor
```

## Install From A Downloaded Wheel

If you already have the wheel asset:

```bash
AGENTDIR_WHEEL=/path/to/agentdir-0.4.0-py3-none-any.whl bash scripts/install.sh
```

To force the virtual environment installer even when `pipx` is present:

```bash
AGENTDIR_FORCE_VENV=1 AGENTDIR_WHEEL=/path/to/agentdir-0.4.0-py3-none-any.whl bash scripts/install.sh
```

## Store Location Scopes

AgentDir stores mailboxes, artifacts, and indexes in an AgentDir root. You can choose the root explicitly or use a scope.

Default behavior:

```bash
agentdir init
agentdir root
```

Inside a git repository, the default root is:

```text
<repo>/.agentdir
```

Available scopes:

```bash
agentdir root --scope project   # nearest git repo .agentdir, or current directory .agentdir
agentdir root --scope user      # ~/.agentdir
agentdir root --scope global    # alias for ~/.agentdir
agentdir root --scope machine   # /Library/Application Support/AgentDir on macOS, /var/lib/agentdir on Linux
```

The machine root may require elevated permissions. Override it for managed machines:

```bash
AGENTDIR_MACHINE_ROOT=/opt/agentdir agentdir init --scope machine
```

## Agent-First Setup

`agentdir setup` is the recommended one-command project setup:

```bash
agentdir setup
```

It initializes the default project store, installs AgentDir-managed Git hook shims, and installs the Codex skill in the user skill directory. After that, daily use should be handled by the coding agent. The user should not need to start sessions, wrap commands, summarize, or gather evidence manually.

To keep generated integration files inside the project store instead:

```bash
agentdir setup --codex-skill store
```

To install only the Codex skill:

```bash
agentdir skills install codex --target user
```

## Install From Source

For local development:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
agentdir --help
```

## Uninstall

If installed through `pipx`:

```bash
pipx uninstall agentdir
```

If installed through the virtual environment fallback:

```bash
rm -f "$HOME/.local/bin/agentdir"
rm -rf "$HOME/.local/share/agentdir"
```

## Release Assets

The GitHub Release should contain:

- `agentdir-0.4.0-py3-none-any.whl`
- `agentdir-0.4.0.tar.gz`
- `install-agentdir.sh`
