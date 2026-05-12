from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .store import AgentDirError

DEFAULT_REPO = "jstxn/agentdir"


@dataclass(frozen=True)
class UpgradeOptions:
    repo: str = DEFAULT_REPO
    version: str | None = None
    adopt: bool = True
    install_skill: str = "user"
    hooks: bool = True
    dry_run: bool = False


def upgrade_agentdir(options: UpgradeOptions) -> dict[str, Any]:
    target_version = options.version or _latest_release_tag(options.repo, dry_run=options.dry_run)
    cwd = Path.cwd()
    repo_root = _git_root(cwd) if options.adopt else None
    before = _command_version(_agentdir_bin())
    result: dict[str, Any] = {
        "current_process_version": __version__,
        "installed_version_before": before,
        "target_version": target_version,
        "repo": options.repo,
        "dry_run": options.dry_run,
        "install": {"planned": True, "ok": None},
        "adopt": {
            "planned": bool(repo_root),
            "ok": None,
            "root": str(repo_root) if repo_root else None,
            "install_skill": options.install_skill,
            "hooks": options.hooks,
        },
        "doctor": {"planned": bool(repo_root), "ok": None},
    }
    if options.dry_run:
        result["install"]["command"] = _install_description(options.repo, target_version)
        if repo_root:
            result["adopt"]["command"] = _adopt_description(options.install_skill, options.hooks)
        return result

    install_output = _run_installer(options.repo, target_version)
    result["install"].update({"ok": install_output.returncode == 0, "stderr": install_output.stderr})
    if install_output.returncode != 0:
        result["install"]["stdout"] = install_output.stdout
        return result

    agentdir_bin = _agentdir_bin()
    after = _command_version(agentdir_bin)
    result["installed_version_after"] = after
    result["install"]["version_ok"] = _version_matches(after, target_version)
    if repo_root:
        adopt_output = _run_adopt(agentdir_bin, repo_root, options.install_skill, options.hooks)
        result["adopt"].update(
            {
                "ok": adopt_output.returncode == 0,
                "stdout": adopt_output.stdout,
                "stderr": adopt_output.stderr,
            }
        )
        doctor_output = subprocess.run(
            [agentdir_bin, "doctor", "--json"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        doctor_payload = _json_or_text(doctor_output.stdout)
        result["doctor"].update(
            {
                "ok": doctor_output.returncode == 0,
                "result": doctor_payload,
                "stderr": doctor_output.stderr,
            }
        )
    return result


def format_upgrade_result(result: dict[str, Any]) -> str:
    lines = [
        f"current_process_version={result['current_process_version']}",
        f"installed_version_before={result.get('installed_version_before') or 'unknown'}",
        f"target_version={result['target_version']}",
    ]
    if result["dry_run"]:
        lines.append(f"install={result['install']['command']}")
        if result["adopt"]["planned"]:
            lines.append(f"adopt={result['adopt']['command']}")
        else:
            lines.append("adopt=skipped outside git repository")
        return "\n".join(lines)

    lines.append(f"install_ok={str(bool(result['install']['ok'])).lower()}")
    if not result["install"]["ok"]:
        lines.append("upgrade failed during reinstall")
        return "\n".join(lines)
    lines.append(f"installed_version_after={result.get('installed_version_after') or 'unknown'}")
    lines.append(f"install_version_ok={str(bool(result['install'].get('version_ok'))).lower()}")
    if result["adopt"]["planned"]:
        lines.append(f"adopt_ok={str(bool(result['adopt']['ok'])).lower()}")
        lines.append(f"adopt_root={result['adopt']['root']}")
    else:
        lines.append("adopt=skipped outside git repository")
    if result["doctor"]["planned"]:
        lines.append(f"doctor_ok={str(bool(result['doctor']['ok'])).lower()}")
        doctor_result = result["doctor"].get("result")
        if isinstance(doctor_result, dict):
            for error in doctor_result.get("errors", []):
                lines.append(f"doctor_error={error}")
            for warning in doctor_result.get("warnings", []):
                lines.append(f"doctor_warning={warning}")
    return "\n".join(lines)


def upgrade_exit_code(result: dict[str, Any]) -> int:
    if result["dry_run"]:
        return 0
    if not result["install"]["ok"]:
        return 1
    if not result["install"].get("version_ok"):
        return 1
    if result["adopt"]["planned"] and not result["adopt"]["ok"]:
        return 1
    if result["doctor"]["planned"] and not result["doctor"]["ok"]:
        return 1
    return 0


def _latest_release_tag(repo: str, *, dry_run: bool) -> str:
    if dry_run:
        return "latest"
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/releases/latest", "--jq", ".tag_name"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise AgentDirError(result.stderr.strip() or f"could not resolve latest release for {repo}")
    return result.stdout.strip()


def _run_installer(repo: str, version: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="agentdir-upgrade.") as tmp:
        install_script = Path(tmp) / "install-agentdir.sh"
        fetch = subprocess.run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github.raw",
                f"repos/{repo}/contents/scripts/install.sh?ref={version}",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if fetch.returncode != 0:
            return fetch
        install_script.write_text(fetch.stdout, encoding="utf-8")
        install_script.chmod(0o700)
        env = os.environ.copy()
        env["AGENTDIR_REPO"] = repo
        env["AGENTDIR_VERSION"] = version
        env["AGENTDIR_WHEEL"] = ""
        return subprocess.run(
            ["bash", str(install_script)],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )


def _run_adopt(
    agentdir_bin: str,
    repo_root: Path,
    install_skill: str,
    hooks: bool,
) -> subprocess.CompletedProcess[str]:
    command = [agentdir_bin, "adopt", "--install-skill", install_skill]
    if not hooks:
        command.append("--no-hooks")
    return subprocess.run(command, cwd=repo_root, text=True, capture_output=True, check=False)


def _git_root(cwd: Path) -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _agentdir_bin() -> str:
    return shutil.which("agentdir") or str(Path.home() / ".local" / "bin" / "agentdir")


def _command_version(command: str) -> str | None:
    try:
        result = subprocess.run([command, "--version"], text=True, capture_output=True, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _version_matches(version_output: str | None, target_version: str) -> bool:
    if not version_output:
        return False
    return target_version.lstrip("v") in version_output


def _json_or_text(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _install_description(repo: str, version: str) -> str:
    return f"install {repo}@{version}"


def _adopt_description(install_skill: str, hooks: bool) -> str:
    command = f"agentdir adopt --install-skill {install_skill}"
    if not hooks:
        command += " --no-hooks"
    return command
