# AgentDir Pi Package

AgentDir ships a Pi package so Pi can load AgentDir guidance as an Agent Skill.
The package currently exposes one skill:

```text
skills/agentdir/SKILL.md
```

The skill teaches Pi to start AgentDir sessions, wrap evidence-bearing shell
commands, use local memory/context packs, and produce evidence-aware handoffs.
It does not install the `agentdir` CLI; install AgentDir separately first.

## Install

From a local checkout:

```bash
pi install /absolute/path/to/agentdir
```

Temporarily for one Pi run:

```bash
pi -e /absolute/path/to/agentdir
```

From git, pin a tag or commit that contains the Pi package files:

```bash
pi install git:github.com/jstxn/agentdir@<tag-or-commit>
```

If the npm package has been published:

```bash
pi install npm:@jstxn/agentdir-pi@0.7.7
```

Use `pi config` to enable or disable the bundled skill after installation.

## Package Manifest

The root `package.json` declares the Pi resources:

```json
{
  "keywords": ["pi-package"],
  "pi": {
    "skills": ["./skills"],
    "image": "https://raw.githubusercontent.com/jstxn/agentdir/v0.7.7/docs/assets/agentdir-overview.png"
  }
}
```

The `pi-package` keyword makes the package discoverable by the Pi package
gallery, `pi.skills` points Pi at the skill directory, and `pi.image` gives the
gallery a preview image from this repository.

## Release Notes

- Keep the npm package version aligned with the AgentDir release version.
- Before publishing, run `npm pack --dry-run` and confirm the tarball includes
  `package.json`, `skills/agentdir/SKILL.md`, `README.md`, and `LICENSE`.
