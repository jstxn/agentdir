# Releasing AgentDir

AgentDir releases go to GitHub Releases and PyPI. The PyPI distribution is
`agentdir-cli` (the plain `agentdir` name is held by an unrelated project);
the importable package and CLI remain `agentdir`. PyPI publishing happens
automatically: the `publish.yml` workflow builds and uploads to PyPI via
trusted publishing whenever a GitHub Release is published.

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
npm pack --pack-destination dist
cp scripts/install.sh dist/install-agentdir.sh
cp scripts/rollback.sh dist/rollback-agentdir.sh
chmod +x dist/install-agentdir.sh
chmod +x dist/rollback-agentdir.sh
```

Expected assets (substitute the release version):

```text
dist/agentdir_cli-0.7.6-py3-none-any.whl
dist/agentdir_cli-0.7.6.tar.gz
dist/jstxn-agentdir-pi-0.7.6.tgz
dist/install-agentdir.sh
dist/rollback-agentdir.sh
```

## Tag And Release

```bash
git tag -a v0.7.6 -m "Release AgentDir v0.7.6"
git push origin main
git push origin v0.7.6

gh release create v0.7.6 \
  dist/agentdir_cli-0.7.6-py3-none-any.whl \
  dist/agentdir_cli-0.7.6.tar.gz \
  dist/jstxn-agentdir-pi-0.7.6.tgz \
  dist/install-agentdir.sh \
  dist/rollback-agentdir.sh \
  --repo jstxn/agentdir \
  --title "AgentDir v0.7.6" \
  --notes-file docs/releases/v0.7.6.md
```

Publishing the release triggers `publish.yml`, which rebuilds the sdist and
wheel from the tag and uploads them to PyPI as `agentdir-cli`.

## Release Verification

Use a disposable environment:

```bash
tmp="$(mktemp -d)"
gh release download v0.7.6 --repo jstxn/agentdir --pattern install-agentdir.sh --dir "$tmp"
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
AGENTDIR_WHEEL="$PWD/dist/agentdir_cli-0.7.6-py3-none-any.whl" \
  bash dist/install-agentdir.sh

AGENTDIR_PREFIX="$tmp/prefix" \
AGENTDIR_HOME="$tmp/home" \
AGENTDIR_FORCE_VENV=1 \
  bash dist/rollback-agentdir.sh v0.7.5

"$tmp/prefix/bin/agentdir" --help
```
