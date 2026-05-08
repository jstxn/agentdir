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
chmod +x dist/install-agentdir.sh
```

Expected assets:

```text
dist/agentdir-0.3.3-py3-none-any.whl
dist/agentdir-0.3.3.tar.gz
dist/install-agentdir.sh
```

## Tag And Release

```bash
git tag -a v0.3.3 -m "Release AgentDir v0.3.3"
git push origin main
git push origin v0.3.3

gh release create v0.3.3 \
  dist/agentdir-0.3.3-py3-none-any.whl \
  dist/agentdir-0.3.3.tar.gz \
  dist/install-agentdir.sh \
  --repo jstxn/agentdir \
  --title "AgentDir v0.3.3" \
  --notes-file docs/releases/v0.3.3.md
```

## Release Verification

Use a disposable environment:

```bash
tmp="$(mktemp -d)"
gh release download v0.3.3 --repo jstxn/agentdir --pattern install-agentdir.sh --dir "$tmp"
AGENTDIR_PREFIX="$tmp/prefix" AGENTDIR_HOME="$tmp/home" bash "$tmp/install-agentdir.sh"
"$tmp/prefix/bin/agentdir" --help
```

Then run a real local session:

```bash
repo="$tmp/repo"
mkdir -p "$repo"
git -C "$repo" init
printf 'release smoke\n' > "$tmp/body.txt"
cd "$repo"
"$tmp/prefix/bin/agentdir" setup --codex-skill store
"$tmp/prefix/bin/agentdir" session ensure --id release-smoke --title "release smoke"
"$tmp/prefix/bin/agentdir" run -- python3 -c "print('release smoke')"
"$tmp/prefix/bin/agentdir" context build "release smoke"
"$tmp/prefix/bin/agentdir" memory search "release smoke"
"$tmp/prefix/bin/agentdir" summarize
"$tmp/prefix/bin/agentdir" evidence
"$tmp/prefix/bin/agentdir" doctor
```
