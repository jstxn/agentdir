# Releasing AgentDir

AgentDir releases are GitHub-only. Do not publish to PyPI unless that becomes an explicit project decision.

## Preflight

```bash
git status --short --branch
python3 -m compileall src tests
uv run --with pytest pytest -q
bash -n examples/dogfood-session.sh
KEEP_WORKDIR=1 bash examples/dogfood-session.sh
```

## Build

```bash
rm -rf dist
uv run --with build python -m build
cp scripts/install.sh dist/install-agentdir.sh
cp scripts/rollback.sh dist/rollback-agentdir.sh
chmod +x dist/install-agentdir.sh
chmod +x dist/rollback-agentdir.sh
```

Expected assets:

```text
dist/agentdir-0.7.2-py3-none-any.whl
dist/agentdir-0.7.2.tar.gz
dist/install-agentdir.sh
dist/rollback-agentdir.sh
```

## Tag And Release

```bash
git tag -a v0.7.2 -m "Release AgentDir v0.7.2"
git push origin main
git push origin v0.7.2

gh release create v0.7.2 \
  dist/agentdir-0.7.2-py3-none-any.whl \
  dist/agentdir-0.7.2.tar.gz \
  dist/install-agentdir.sh \
  dist/rollback-agentdir.sh \
  --repo jstxn/agentdir \
  --title "AgentDir v0.7.2" \
  --notes-file docs/releases/v0.7.2.md
```

## Release Verification

Use a disposable environment:

```bash
tmp="$(mktemp -d)"
gh release download v0.7.2 --repo jstxn/agentdir --pattern install-agentdir.sh --dir "$tmp"
AGENTDIR_PREFIX="$tmp/prefix" AGENTDIR_HOME="$tmp/home" bash "$tmp/install-agentdir.sh"
"$tmp/prefix/bin/agentdir" --help
"$tmp/prefix/bin/agentdir" --version
```

Then run a real local session:

```bash
repo="$tmp/repo"
mkdir -p "$repo"
git -C "$repo" init
printf 'release smoke\n' > "$tmp/body.txt"
cd "$repo"
"$tmp/prefix/bin/agentdir" adopt --install-skill store
"$tmp/prefix/bin/agentdir" work start "release smoke" --emit-context
"$tmp/prefix/bin/agentdir" run -- python3 -c "print('release smoke')"
"$tmp/prefix/bin/agentdir" context build "release smoke"
"$tmp/prefix/bin/agentdir" memory search "release smoke"
"$tmp/prefix/bin/agentdir" status
"$tmp/prefix/bin/agentdir" evidence --brief
"$tmp/prefix/bin/agentdir" timeline
"$tmp/prefix/bin/agentdir" report final --format json
"$tmp/prefix/bin/agentdir" work finish --json
```

## Rollback Verification

Before publishing a major behavior change, verify rollback from the new build
back to the previous stable release in a disposable environment:

```bash
tmp="$(mktemp -d)"
AGENTDIR_PREFIX="$tmp/prefix" \
AGENTDIR_HOME="$tmp/home" \
AGENTDIR_FORCE_VENV=1 \
AGENTDIR_WHEEL="$PWD/dist/agentdir-0.7.2-py3-none-any.whl" \
  bash dist/install-agentdir.sh

AGENTDIR_PREFIX="$tmp/prefix" \
AGENTDIR_HOME="$tmp/home" \
AGENTDIR_FORCE_VENV=1 \
  bash dist/rollback-agentdir.sh v0.7.1

"$tmp/prefix/bin/agentdir" --help
```
