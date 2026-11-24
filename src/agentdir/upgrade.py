from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .git import git_root
from .store import AgentDirDependencyError, AgentDirError

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
    detected_repo = git_root(cwd) if options.adopt else None
    repo_root = (
        detected_repo
        if detected_repo is not None and (detected_repo / ".agentdir" / "VERSION").is_file()
        else None
    )
    adopt_skip_reason: str | None = None
    if not options.adopt:
        adopt_skip_reason = "disabled"
    elif detected_repo is None:
        adopt_skip_reason = "not_in_git_repository"
    elif repo_root is None:
        adopt_skip_reason = "not_adopted"
    before = _command_version(_agentdir_bin())
    adopt_info: dict[str, Any] = {
        "planned": bool(repo_root),
        "ok": None,
        "root": str(repo_root) if repo_root else None,
        "install_skill": options.install_skill,
        "hooks": options.hooks,
    }
    if adopt_skip_reason is not None:
        adopt_info["reason"] = adopt_skip_reason
    result: dict[str, Any] = {
        "current_process_version": __version__,
        "installed_version_before": before,
        "target_version": target_version,
        "repo": options.repo,
        "dry_run": options.dry_run,
        "install": {"planned": True, "ok": None},
        "adopt": adopt_info,
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


def _adopt_skip_line(result: dict[str, Any]) -> str:
    reason = result.get("adopt", {}).get("reason")
    if reason == "not_adopted":
        return "adopt=skipped (repository is not adopted)"
    if reason == "disabled":
        return "adopt=skipped (--no-adopt)"
    return "adopt=skipped outside git repository"


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
            lines.append(_adopt_skip_line(result))
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
        lines.append(_adopt_skip_line(result))
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
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/releases/latest", "--jq", ".tag_name"],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AgentDirDependencyError(
            "GitHub CLI ('gh') is required to resolve the latest release; install gh or pass --version"
        ) from exc
    except OSError as exc:
        raise AgentDirDependencyError(f"failed to run GitHub CLI ('gh'): {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise AgentDirError(result.stderr.strip() or f"could not resolve latest release for {repo}")
    return result.stdout.strip()


def _run_installer(repo: str, version: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="agentdir-upgrade.") as tmp:
        install_script = Path(tmp) / "install-agentdir.sh"
        try:
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
        except FileNotFoundError as exc:
            raise AgentDirDependencyError(
                "GitHub CLI ('gh') is required to download the release installer; install gh and authenticate"
            ) from exc
        except OSError as exc:
            raise AgentDirDependencyError(f"failed to run GitHub CLI ('gh'): {exc}") from exc
        if fetch.returncode != 0:
            return fetch
        install_script.write_text(fetch.stdout, encoding="utf-8")
        install_script.chmod(0o700)
        env = os.environ.copy()
        env["AGENTDIR_REPO"] = repo
        env["AGENTDIR_VERSION"] = version
        env["AGENTDIR_WHEEL"] = ""
        try:
            return subprocess.run(
                ["bash", str(install_script)],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
        except OSError as exc:
            raise AgentDirDependencyError(f"failed to execute release installer with bash: {exc}") from exc


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


def _agentdir_bin() -> str:
    invoked = Path(sys.argv[0])
    if invoked.name == "agentdir" and invoked.exists():
        return str(invoked.resolve())
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
    expected_norm = target_version.strip().removeprefix("v")
    if not expected_norm:
        return False
    pattern = rf"(?<![0-9A-Za-z._+-])v?{re.escape(expected_norm)}(?![0-9A-Za-z._+-])"
    return re.search(pattern, version_output) is not None


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
