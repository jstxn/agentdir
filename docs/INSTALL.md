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
  repos/jstxn/agentdir/contents/scripts/install.sh?ref=v0.1.1 | bash
```

The installer downloads the release wheel and installs it with `pipx` when available. If `pipx` is not installed, it falls back to a self-contained virtual environment at:

```text
~/.local/share/agentdir/venv
```

and links the CLI at:

```text
~/.local/bin/agentdir
```

If `~/.local/bin` is not on PATH, add it:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Verify Install

```bash
agentdir --help

repo="$(mktemp -d)/agentdir-install-smoke"
git -C "$repo" init
printf 'agentdir install smoke\n' > /tmp/agentdir-smoke.txt
cd "$repo"
agentdir init
agentdir root
agentdir emit --session install-smoke --type agent.message --body /tmp/agentdir-smoke.txt
agentdir index rebuild
agentdir replay --session install-smoke
agentdir doctor
```

## Install From A Downloaded Wheel

If you already have the wheel asset:

```bash
AGENTDIR_WHEEL=/path/to/agentdir-0.1.1-py3-none-any.whl bash scripts/install.sh
```

To force the virtual environment installer even when `pipx` is present:

```bash
AGENTDIR_FORCE_VENV=1 AGENTDIR_WHEEL=/path/to/agentdir-0.1.1-py3-none-any.whl bash scripts/install.sh
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

- `agentdir-0.1.1-py3-none-any.whl`
- `agentdir-0.1.1.tar.gz`
- `install-agentdir.sh`
