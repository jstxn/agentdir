# Installing AgentDir

AgentDir is distributed as GitHub Release assets. The project does not require PyPI, npm, GitHub Packages, Redis, Dovecot, or a background service.

## Recommended Install

For the private `jstxn/agentdir` repository, engineers need GitHub CLI access to the repo:

```bash
gh auth login
gh auth status
```

Download the release installer and run it:

```bash
tmpdir="$(mktemp -d)"
gh release download v0.1.0 \
  --repo jstxn/agentdir \
  --pattern install-agentdir.sh \
  --dir "$tmpdir"
bash "$tmpdir/install-agentdir.sh"
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

root="$(mktemp -d)/agentdir-root"
printf 'agentdir install smoke\n' > /tmp/agentdir-smoke.txt
agentdir init "$root"
agentdir emit --root "$root" --session install-smoke --type agent.message --body /tmp/agentdir-smoke.txt
agentdir index rebuild --root "$root"
agentdir replay --root "$root" --session install-smoke
agentdir doctor --root "$root"
```

## Install From A Downloaded Wheel

If you already have the wheel asset:

```bash
AGENTDIR_WHEEL=/path/to/agentdir-0.1.0-py3-none-any.whl bash scripts/install.sh
```

To force the virtual environment installer even when `pipx` is present:

```bash
AGENTDIR_FORCE_VENV=1 AGENTDIR_WHEEL=/path/to/agentdir-0.1.0-py3-none-any.whl bash scripts/install.sh
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

- `agentdir-0.1.0-py3-none-any.whl`
- `agentdir-0.1.0.tar.gz`
- `install-agentdir.sh`
