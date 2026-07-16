# Installing AgentDir

AgentDir is distributed on PyPI as `agentdir-cli` and as GitHub Release
assets. Either path installs the same `agentdir` command. No background
service, Redis, or Dovecot is required.

The PyPI distribution is named `agentdir-cli` because the plain `agentdir`
name is held by an unrelated project; the importable package and the CLI are
still `agentdir`.

## Recommended Install

Install from PyPI:

```bash
uv tool install agentdir-cli
# or
pipx install agentdir-cli
```

Or install with the release installer:

```bash
curl -fsSL https://raw.githubusercontent.com/jstxn/agentdir/main/scripts/install.sh | bash
```

The installer downloads release assets anonymously with `curl`. If `curl` is
unavailable or the download fails, it falls back to authenticated GitHub CLI
(`gh auth login`).

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
agentdir --upgrade --upgrade-version v0.7.6
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
AGENTDIR_WHEEL=/path/to/agentdir_cli-0.7.6-py3-none-any.whl bash scripts/install.sh
```

To force the virtual environment installer even when `pipx` is present:

```bash
AGENTDIR_FORCE_VENV=1 AGENTDIR_WHEEL=/path/to/agentdir_cli-0.7.6-py3-none-any.whl bash scripts/install.sh
```

## Roll Back To The Previous Release

Rollback does not rely on the installed `agentdir` binary. It fetches the
installer from the target GitHub Release tag and reinstalls that wheel.

To return to the previous stable release:

```bash
curl -fsSL https://raw.githubusercontent.com/jstxn/agentdir/main/scripts/rollback.sh | bash
```

To choose a specific release:

```bash
curl -fsSL https://raw.githubusercontent.com/jstxn/agentdir/main/scripts/rollback.sh | bash -s -- v0.7.5
```

The equivalent manual rollback is:

```bash
curl -fsSL https://raw.githubusercontent.com/jstxn/agentdir/main/scripts/install.sh | AGENTDIR_VERSION=v0.7.5 bash
```

## Optional Extras

The default install includes the core control-plane dependencies for platform
paths and richer terminal output. Heavier lanes are explicit extras:

```bash
pipx inject agentdir-cli 'agentdir-cli[watch]'
pipx inject agentdir-cli 'agentdir-cli[semantic]'
pipx inject agentdir-cli 'agentdir-cli[team]'
```

`watch` enables the warm index daemon to use file events when available.
`semantic` adds local embeddings and the embedded vector backend configuration
surface. `team` adds optional shared-memory backend clients.

### When The Semantic Extra Is Worth It

The default retrieval backend (`local-hybrid`) combines lexical passage
matching with lightweight hashed vectors. It needs no model download and is
usually enough when memory queries reuse the project's own vocabulary: error
strings, subsystem names, command names, file paths.

Install the `semantic` extra and opt in when retrieval quality matters more
than setup weight:

```bash
pipx inject agentdir-cli 'agentdir-cli[semantic]'
agentdir memory embeddings configure fastembed   # one-time model download
agentdir memory backend configure sqlite-vec     # optional: faster large stores
```

Reach for it when:

- queries are paraphrases rather than the recorded wording ("auth timeouts"
  should match "login requests hang for 30s"),
- memory is federated across several repos with different vocabularies,
- the store has grown past tens of thousands of passages and `sqlite-vec`
  lookup speed starts to matter.

Skip it for single-repo stores with recent, literally-worded memory - the
default backend retrieves those well without the model download.
`agentdir memory backend status` shows which backends are active.

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
project guidance for common agent tools, asks interactive users whether
`.agentdir/` should be added to a project or user-level Git ignore file, runs
doctor, and prints the next workbench command. After that, daily use should be
handled by the coding agent. The user should not need to start sessions, wrap
commands, summarize, or gather evidence manually.

Preview adoption without creating `.agentdir`, hooks, or guidance files:

```bash
agentdir adopt --dry-run --json
agentdir setup --dry-run --json
```

To keep generated integration files inside the project store instead:

```bash
agentdir adopt --install-skill store --install-generic store --integration-target store
```

For non-interactive installs, choose the ignore destination explicitly:

```bash
agentdir adopt --gitignore project  # write <repo>/.gitignore
agentdir adopt --gitignore user     # write the user-level Git excludes file
agentdir adopt --gitignore none     # leave ignore files unchanged
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

## Coexisting With Rule Generators And Hook Managers

Some repos generate their agent guidance files and Git hooks from another
source of truth. Adoption detects the common tools and adapts so managed
content is not silently wiped.

### Rule generators (rulesync)

When a repo uses [rulesync](https://github.com/dyoshikawa/rulesync)
(`.rulesync/` or `rulesync.jsonc` present), `CLAUDE.md`, `AGENTS.md`,
`.cursor/rules/`, and friends are generated files: the next
`rulesync generate` (often wired into `pnpm rulesync`) rewrites them and drops
any managed block. `agentdir adopt` therefore writes the managed rule to the
rulesync source instead:

```text
.rulesync/rules/agentdir.md   # root: false, targets: ["*"]
```

Run `rulesync generate` afterwards to propagate it into every generated tool
file; it now survives regeneration. The skipped generated files are listed in
the adopt output. To force the classic behavior and write tool files directly,
use `agentdir adopt --install-integrations project-files`.

Independent of rulesync, adoption refuses to insert managed blocks into any
existing file whose header marks it as generated (`DO NOT EDIT`,
`@generated`, `generated by ...`) unless `--force` is given.

### Git hook managers (lefthook, husky, pre-commit)

AgentDir hook shims chain whatever hook they replace, so they coexist with a
hook manager - until the manager reinstalls itself. `lefthook install` and
`pre-commit install` rewrite `.git/hooks/*` (package installs typically
trigger lefthook via a postinstall script), and husky points
`core.hooksPath` at a directory it regenerates. Any of these silently stops
AgentDir's git recording.

Adoption now defends against that:

- adopt and `agentdir hooks install` detect lefthook, husky, pre-commit, and a
  configured `core.hooksPath`, and warn about the overwrite behavior up front;
- installed shims are recorded in `.agentdir/hooks.json`, and `agentdir
  doctor` (also run by `status`, `adopt`, and `work finish`) warns when a
  recorded shim was overwritten or removed, naming the tool that did it;
- rerunning `agentdir hooks install` restores the shims without `--force` when
  the clobbering script came from a known hook manager, and chains the
  manager's hook so both keep running;
- shims are installed into the hooks directory git actually consults
  (`core.hooksPath` and linked worktrees included).

After a `pnpm install` or similar in a lefthook repo, either rerun
`agentdir hooks install` or let the next doctor run flag it. If the churn is
unwelcome, adopt with `--no-hooks` and rely on session recording without git
hook events, or run the recorder from the hook manager itself, e.g. in
`lefthook.yml`:

```yaml
post-commit:
  jobs:
    - name: agentdir-record
      run: agentdir hooks record --hook post-commit --original-exit-code 0 || true
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
pipx uninstall agentdir-cli
# installs from v0.7.4 or earlier used the old distribution name:
pipx uninstall agentdir
```

If installed through the virtual environment fallback:

```bash
rm -f "$HOME/.local/bin/agentdir"
rm -rf "$HOME/.local/share/agentdir"
```

## Release Assets

The GitHub Release should contain:

- `agentdir_cli-<version>-py3-none-any.whl`
- `agentdir_cli-<version>.tar.gz`
- `install-agentdir.sh`
- `rollback-agentdir.sh`
