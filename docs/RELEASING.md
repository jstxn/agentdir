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
dist/agentdir-0.1.1-py3-none-any.whl
dist/agentdir-0.1.1.tar.gz
dist/install-agentdir.sh
```

## Tag And Release

```bash
git tag -a v0.1.1 -m "Release AgentDir v0.1.1"
git push origin main
git push origin v0.1.1

gh release create v0.1.1 \
  dist/agentdir-0.1.1-py3-none-any.whl \
  dist/agentdir-0.1.1.tar.gz \
  dist/install-agentdir.sh \
  --repo jstxn/agentdir \
  --title "AgentDir v0.1.1" \
  --notes-file docs/releases/v0.1.1.md
```

## Release Verification

Use a disposable environment:

```bash
tmp="$(mktemp -d)"
gh release download v0.1.1 --repo jstxn/agentdir --pattern install-agentdir.sh --dir "$tmp"
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
"$tmp/prefix/bin/agentdir" init
"$tmp/prefix/bin/agentdir" root
"$tmp/prefix/bin/agentdir" emit --session release-smoke --type agent.message --body "$tmp/body.txt"
"$tmp/prefix/bin/agentdir" index rebuild
"$tmp/prefix/bin/agentdir" replay --session release-smoke
"$tmp/prefix/bin/agentdir" doctor
```
