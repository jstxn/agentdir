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
  'repos/jstxn/agentdir/contents/scripts/install.sh?ref=v0.7.0' | bash
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

## Upgrade Existing Installs

From any repo where AgentDir should stay adopted:

```bash
agentdir --upgrade
```

The upgrade command resolves the latest GitHub Release, reinstalls AgentDir,
then re-runs adoption for the current git repository. It refreshes hooks, the
Codex skill, and broad project guidance by default, then runs `doctor`.

Useful variants:

```bash
agentdir --upgrade --upgrade-install-skill none
agentdir --upgrade --upgrade-no-adopt
agentdir --upgrade --upgrade-version v0.7.0
agentdir --upgrade --upgrade-dry-run
```

## Verify Install

```bash
agentdir --help
agentdir --version

repo="$(mktemp -d)/agentdir-install-smoke"
mkdir -p "$repo"
git -C "$repo" init
cd "$repo"
agentdir adopt --install-skill store
agentdir work start "install smoke" --emit-context
agentdir run -- python3 -c "print('agentdir install smoke')"
agentdir evidence --brief
agentdir timeline
agentdir context build "install smoke"
agentdir memory search "install smoke"
agentdir status
agentdir report final --format json
agentdir work finish --json
```

## Install From A Downloaded Wheel

If you already have the wheel asset:

```bash
AGENTDIR_WHEEL=/path/to/agentdir-0.7.0-py3-none-any.whl bash scripts/install.sh
```

To force the virtual environment installer even when `pipx` is present:

```bash
AGENTDIR_FORCE_VENV=1 AGENTDIR_WHEEL=/path/to/agentdir-0.7.0-py3-none-any.whl bash scripts/install.sh
```

## Roll Back To The Previous Release

Rollback does not rely on the installed `agentdir` binary. It fetches the
installer from the target GitHub Release tag and reinstalls that wheel.

To return to the previous stable release:

```bash
gh api -H "Accept: application/vnd.github.raw" \
  'repos/jstxn/agentdir/contents/scripts/rollback.sh?ref=v0.7.0' | bash
```

To choose a specific release:

```bash
gh api -H "Accept: application/vnd.github.raw" \
  'repos/jstxn/agentdir/contents/scripts/rollback.sh?ref=v0.7.0' | bash -s -- v0.6.0
```

The equivalent manual rollback is:

```bash
gh api -H "Accept: application/vnd.github.raw" \
  'repos/jstxn/agentdir/contents/scripts/install.sh?ref=v0.6.0' | AGENTDIR_VERSION=v0.6.0 bash
```

## Optional Extras

The default install includes the core control-plane dependencies for platform
paths and richer terminal output. Heavier lanes are explicit extras:

```bash
pipx inject agentdir 'agentdir[watch]'
pipx inject agentdir 'agentdir[semantic]'
pipx inject agentdir 'agentdir[team]'
```

`watch` enables the warm index daemon to use file events when available.
`semantic` adds local embeddings and the embedded vector backend configuration
surface. `team` adds optional shared-memory backend clients.

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
agentdir root --scope user      # platform user data root, legacy ~/.agentdir if it already exists
agentdir root --scope global    # alias for user scope
agentdir root --scope machine   # platform site data root, legacy machine root if it already exists
```

The machine root may require elevated permissions. Override it for managed machines:

```bash
AGENTDIR_MACHINE_ROOT=/opt/agentdir agentdir init --scope machine
```

## Agent-First Setup

`agentdir adopt` is the recommended one-command project setup:

```bash
agentdir adopt
```

It initializes the default project store, installs AgentDir-managed Git hook
shims, installs the Codex skill in the user skill directory, writes broad
project guidance for common agent tools, runs doctor, and prints the next
workbench command. After that, daily use should be handled by the coding agent.
The user should not need to start sessions, wrap commands, summarize, or gather
evidence manually.

Preview adoption without creating `.agentdir`, hooks, or guidance files:

```bash
agentdir adopt --dry-run --json
agentdir setup --dry-run --json
```

To keep generated integration files inside the project store instead:

```bash
agentdir adopt --install-skill store --install-generic store --integration-target store
```

To undo managed setup while keeping `.agentdir` evidence:

```bash
agentdir unadopt          # dry-run
agentdir unadopt --apply  # restore hooks and remove managed guidance
```

To install only the Codex skill or generic guidance:

```bash
agentdir skills install codex --target user
agentdir skills install generic --target project
```

To install broad agent guidance explicitly:

```bash
agentdir integrations install all --target project
agentdir integrations doctor --json
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

- `agentdir-0.7.0-py3-none-any.whl`
- `agentdir-0.7.0.tar.gz`
- `install-agentdir.sh`
- `rollback-agentdir.sh`
