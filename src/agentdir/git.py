from __future__ import annotations

import subprocess
from pathlib import Path


def git_output(args: list[str], cwd: str | Path | None = None) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=Path(cwd).expanduser().resolve() if cwd else None,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_root(cwd: str | Path | None = None) -> Path | None:
    output = git_output(["rev-parse", "--show-toplevel"], cwd)
    return Path(output).resolve() if output else None


def git_head(cwd: str | Path | None = None, *, short: bool = False) -> str | None:
    args = ["rev-parse", "--short", "HEAD"] if short else ["rev-parse", "HEAD"]
    return git_output(args, cwd)


def git_branch(cwd: str | Path | None = None) -> str | None:
    return git_output(["branch", "--show-current"], cwd)


def git_status_short(cwd: str | Path | None = None) -> str:
    return git_output(["status", "--short"], cwd) or ""


def workspace_name(cwd: str | Path | None = None) -> str:
    root = git_root(cwd)
    if root:
        return root.name
    return Path(cwd or Path.cwd()).resolve().name
