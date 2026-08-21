from __future__ import annotations

import ast
import re
import shlex
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from .index import update_index
from .query import query_messages
from .sessions import read_current_session, read_last_session, require_current_session

EVIDENCE_FAMILIES = ("test", "lint", "typecheck", "build", "doctor", "release", "diagnostic")

_FAMILY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "test": (
        "pytest",
        "unittest",
        "test",
        "tests",
        "npm test",
        "pnpm test",
        "yarn test",
        "go test",
        "cargo test",
        "rspec",
    ),
    "lint": ("lint", "eslint", "ruff", "flake8", "prettier", "clippy", "golangci-lint"),
    "typecheck": ("typecheck", "type-check", "tsc", "mypy", "pyright", "sorbet", "staticcheck"),
    "build": ("build", "npm run build", "pnpm build", "yarn build", "cargo build", "go build", "make"),
    "doctor": ("doctor", "health", "diagnose", "diagnostic"),
    "release": ("release", "publish", "pack", "version", "tag", "twine", "npm publish"),
}

_DIAGNOSTIC_EVENTS = {"file.diff"}

_IDENTITY_FAMILIES = {
    "build": "build",
    "clippy": "lint",
    "compileall": "build",
    "doctor": "doctor",
    "eslint": "lint",
    "flake8": "lint",
    "golangci-lint": "lint",
    "jest": "test",
    "lint": "lint",
    "mypy": "typecheck",
    "npm-test": "test",
    "prettier": "lint",
    "publish": "release",
    "py.test": "test",
    "py_compile": "build",
    "pyright": "typecheck",
    "pytest": "test",
    "release": "release",
    "rspec": "test",
    "ruff": "lint",
    "sorbet": "typecheck",
    "staticcheck": "typecheck",
    "tag": "release",
    "test": "test",
    "tests": "test",
    "tsc": "typecheck",
    "twine": "release",
    "type-check": "typecheck",
    "typecheck": "typecheck",
    "unittest": "test",
    "version": "release",
    "vitest": "test",
}

_PACKAGE_MANAGERS = {"bun", "npm", "pnpm", "yarn"}
_PACKAGE_EXECUTORS = {"bunx", "npx", "pnpx"}
_INLINE_INTERPRETERS = {"bash", "node", "sh", "zsh"}
_ENV_LONG_OPTIONS_WITH_VALUES = {"--argv0", "--chdir", "--unset"}
_ENV_LONG_FLAG_OPTIONS = {"--debug", "--ignore-environment"}
_STRUCTURED_FIRST_SUBCOMMAND_FAMILIES = {
    "bazel": {"build": "build", "test": "test"},
    "bazelisk": {"build": "build", "test": "test"},
    "dart": {"analyze": "typecheck", "compile": "build", "test": "test"},
    "deno": {"check": "typecheck", "compile": "build", "lint": "lint", "test": "test"},
    "dotnet": {"build": "build", "pack": "release", "publish": "release", "test": "test"},
    "flutter": {"analyze": "typecheck", "build": "build", "test": "test"},
    "gradle": {"build": "build", "check": "test", "test": "test"},
    "gradlew": {"build": "build", "check": "test", "test": "test"},
    "meson": {"compile": "build", "test": "test"},
    "mvn": {
        "compile": "build",
        "integration-test": "test",
        "package": "build",
        "test": "test",
        "verify": "test",
    },
    "mvnw": {
        "compile": "build",
        "integration-test": "test",
        "package": "build",
        "test": "test",
        "verify": "test",
    },
    "swift": {"build": "build", "test": "test"},
    "xcodebuild": {
        "archive": "build",
        "build": "build",
        "build-for-testing": "build",
        "test": "test",
        "test-without-building": "test",
    },
}
_MULTI_ACTION_CLEAN_RUNNERS = {"gradle", "gradlew", "mvn", "mvnw", "xcodebuild"}
_RUNNER_ASSIGNMENT_RUNNERS = {"gmake", "just", "make"}
_RUNNER_VALUE_OPTIONS = {
    "bazel": {"--output_base"},
    "bazelisk": {"--output_base"},
    "bun": {"--cwd"},
    "cargo": {"--target-dir"},
    "dotnet": {"--verbosity"},
    "gmake": {"--directory", "-C"},
    "go": {"-C"},
    "gradle": {"--project-dir", "-p"},
    "gradlew": {"--project-dir", "-p"},
    "make": {"--directory", "-C"},
    "mvn": {"--file", "-f"},
    "mvnw": {"--file", "-f"},
    "npm": {"--prefix"},
    "pnpm": {"--dir", "-C"},
    "swift": {"--package-path"},
    "xcodebuild": {"-scheme"},
    "yarn": {"--cwd"},
}
_RUNNER_FLAG_OPTIONS = {
    "gradle": {"--no-daemon"},
    "gradlew": {"--no-daemon"},
    "mvn": {"--quiet", "-q"},
    "mvnw": {"--quiet", "-q"},
}
_UV_OPTIONS_WITH_VALUES = {
    "--allow-insecure-host",
    "--cache-dir",
    "--color",
    "--config-file",
    "--config-setting",
    "--config-settings-package",
    "--default-index",
    "--directory",
    "--env-file",
    "--exclude-newer",
    "--exclude-newer-package",
    "--extra",
    "--extra-index-url",
    "--find-links",
    "--fork-strategy",
    "--group",
    "--index",
    "--index-strategy",
    "--index-url",
    "--keyring-provider",
    "--link-mode",
    "--no-build-isolation-package",
    "--no-build-package",
    "--no-binary-package",
    "--no-editable-package",
    "--no-extra",
    "--no-group",
    "--no-sources-package",
    "--only-group",
    "--package",
    "--prerelease",
    "--project",
    "--python",
    "--python-platform",
    "--refresh-package",
    "--reinstall-package",
    "--resolution",
    "--upgrade-group",
    "--upgrade-package",
    "--with",
    "--with-editable",
    "--with-requirements",
    "-C",
    "-P",
    "-f",
    "-i",
    "-p",
    "-w",
}
_UV_FLAG_OPTIONS = {
    "--active",
    "--all-extras",
    "--all-groups",
    "--all-packages",
    "--compile-bytecode",
    "--exact",
    "--frozen",
    "--help",
    "--isolated",
    "--locked",
    "--managed-python",
    "--no-binary",
    "--no-build",
    "--no-build-isolation",
    "--no-cache",
    "--no-config",
    "--no-default-groups",
    "--no-dev",
    "--no-editable",
    "--no-env-file",
    "--no-index",
    "--no-managed-python",
    "--no-progress",
    "--no-project",
    "--no-python-downloads",
    "--no-sources",
    "--no-sync",
    "--offline",
    "--only-dev",
    "--quiet",
    "--refresh",
    "--reinstall",
    "--system-certs",
    "--upgrade",
    "--verbose",
    "-U",
    "-h",
    "-n",
    "-q",
    "-v",
}


def resolve_review_session(root: str | Path, session_id: str | None) -> str:
    if session_id:
        return session_id
    current = read_current_session(root)
    if current and current.status == "active":
        return current.session_id
    latest = read_last_session(root)
    if latest:
        return latest.session_id
    return require_current_session(root).session_id


def ensure_index(root: str | Path) -> None:
    update_index(root)


def summarize_session(root: str | Path, session_id: str | None = None, *, rebuild: bool = True) -> dict[str, Any]:
    resolved = resolve_review_session(root, session_id)
    if rebuild:
        ensure_index(root)
    rows = query_messages(root, session_id=resolved, limit=10_000)
    counts = Counter(row.get("event_type") or "unknown" for row in rows)
    tool_results = [row for row in rows if row.get("event_type") == "tool.result"]
    failure_state = evidence_failure_state(tool_results)
    return {
        "session_id": resolved,
        "events": len(rows),
        "event_counts": dict(sorted(counts.items())),
        "tool_results": len(tool_results),
        # Keep the original historical counter additive and stable. The two
        # stateful counters explain whether those failures still describe the
        # latest result in their evidence family.
        "failed_tools": len(failure_state["historical"]),
        "resolved_failed_tools": len(failure_state["resolved"]),
        "unresolved_failed_tools": len(failure_state["unresolved"]),
        "first_event": rows[0]["date_header"] if rows else None,
        "last_event": rows[-1]["date_header"] if rows else None,
    }


def format_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"session={summary['session_id']}",
        f"events={summary['events']}",
        f"tool_results={summary['tool_results']}",
        f"failed_tools={summary['failed_tools']}",
        f"resolved_failed_tools={summary.get('resolved_failed_tools', 0)}",
        f"unresolved_failed_tools={summary.get('unresolved_failed_tools', summary['failed_tools'])}",
    ]
    for event_type, count in summary["event_counts"].items():
        lines.append(f"{event_type}={count}")
    return "\n".join(lines)


def evidence_rows(root: str | Path, session_id: str | None = None, *, rebuild: bool = True) -> list[dict[str, Any]]:
    resolved = resolve_review_session(root, session_id)
    if rebuild:
        ensure_index(root)
    rows = query_messages(root, session_id=resolved, limit=10_000)
    wanted = {"tool.call", "tool.result", "file.diff"}
    evidence = [
        row
        for row in rows
        if row.get("event_type") in wanted or str(row.get("event_type") or "").startswith("git.hook.")
    ]
    for row in evidence:
        row["family"] = classify_evidence(row)
        row["failed"] = evidence_failed(row)
        row["truncated"] = evidence_truncated(row)
    return evidence


def classify_evidence(row: dict[str, Any]) -> str:
    event_type = str(row.get("event_type") or "")
    if event_type in _DIAGNOSTIC_EVENTS or event_type.startswith("git.hook."):
        return "diagnostic"
    command = _evidence_command_argv(row)
    if command:
        return classify_evidence_command(
            command,
            tool_name=str(row.get("tool") or ""),
        )
    tool_family = _identity_family(str(row.get("tool") or ""))
    if tool_family:
        return tool_family

    # Legacy and manually emitted tool events may not have the structured
    # command/argv prelude written by ``agentdir run``. Keep their prior
    # body-based behavior without letting cwd or captured output influence
    # ordinary run classification.
    legacy_text = " ".join(
        str(row.get(key) or "") for key in ("subject", "body_text", "event_type", "message_id")
    ).lower()
    return _family_from_text(legacy_text) or "diagnostic"


def classify_evidence_command(argv: Sequence[str], *, tool_name: str | None = None) -> str:
    """Classify one command from invocation data, never cwd or captured output."""
    command = [str(part) for part in argv]
    if not command:
        return "diagnostic"
    executable = Path(command[0]).name.lower()
    tool_identity = Path(str(tool_name or "")).name.lower()
    if executable in {"test", "["} and tool_identity in {"", executable}:
        return "diagnostic"
    tool_family = _identity_family(tool_name or "")
    if tool_family:
        return tool_family

    lowered = [part.lower() for part in command]
    executable_family = _identity_family(executable)
    if executable_family:
        return executable_family
    if executable == "git" and "diff" in lowered[1:] and "--check" in lowered[1:]:
        return "diagnostic"

    env_family = _env_command_family(executable, command[1:])
    if env_family is not None:
        return env_family
    uv_family = _uv_command_family(executable, lowered[1:])
    if uv_family is not None:
        return uv_family
    python_family = _python_command_family(executable, lowered[1:])
    if python_family is not None:
        return python_family
    package_family = _package_command_family(executable, command[1:])
    if package_family is not None:
        return package_family
    subcommand_family = _structured_subcommand_family(executable, command[1:])
    if subcommand_family is not None:
        return subcommand_family

    # Unknown executables remain diagnostic. Their operands are data, not a
    # second chance to infer command identity (for example, ``echo pytest``
    # must not become passing test evidence).
    return "diagnostic"


def _env_command_family(executable: str, argv: Sequence[str]) -> str | None:
    """Classify the command executed by ``env``, not its assignments or options."""
    if executable != "env":
        return None

    wrapped = list(argv)
    index = 0
    options_active = True
    while index < len(wrapped):
        part = wrapped[index]
        if options_active:
            if part == "--":
                options_active = False
                index += 1
                continue
            if part in _ENV_LONG_OPTIONS_WITH_VALUES:
                if index + 1 >= len(wrapped):
                    return "diagnostic"
                index += 2
                continue
            if any(
                part.startswith(f"{option}=")
                for option in _ENV_LONG_OPTIONS_WITH_VALUES
            ):
                index += 1
                continue
            if part == "--split-string":
                if index + 1 >= len(wrapped):
                    return "diagnostic"
                return _classify_split_env_string(wrapped[index + 1], wrapped[index + 2 :])
            if part.startswith("--split-string="):
                return _classify_split_env_string(
                    part.split("=", 1)[1],
                    wrapped[index + 1 :],
                )
            if part == "--null":
                # BSD env rejects -0/--null when a utility is supplied, so
                # these invocations can never support a downstream claim.
                return "diagnostic"
            if part in _ENV_LONG_FLAG_OPTIONS or part == "-":
                index += 1
                continue
            if part.startswith("-"):
                short_kind, short_value = _parse_env_short_option(part)
                if short_kind == "flag" or short_kind == "value_attached":
                    index += 1
                    continue
                if short_kind == "value_separate":
                    if index + 1 >= len(wrapped):
                        return "diagnostic"
                    index += 2
                    continue
                if short_kind == "split_attached":
                    return _classify_split_env_string(
                        short_value or "",
                        wrapped[index + 1 :],
                    )
                if short_kind == "split_separate":
                    if index + 1 >= len(wrapped):
                        return "diagnostic"
                    return _classify_split_env_string(
                        wrapped[index + 1],
                        wrapped[index + 2 :],
                    )
                # Unknown and non-executing short options fail closed so
                # their operands cannot be promoted into evidence identities.
                return "diagnostic"
            if _is_env_assignment(part):
                # BSD env recognizes options only before the first assignment.
                options_active = False
                index += 1
                continue
        if _is_env_assignment(part):
            index += 1
            continue
        if not part or any(character.isspace() for character in part):
            return "diagnostic"
        if part.startswith("-"):
            # After an assignment or ``--``, an option-like token is the
            # utility name. Fail closed rather than pretending a later operand
            # was executed.
            return "diagnostic"
        return classify_evidence_command(wrapped[index:])
    return "diagnostic"


def _classify_split_env_string(value: str, remaining: Sequence[str]) -> str:
    if (
        "\\" in value
        or "$" in value
        or "#" in value
        or any(character.isspace() and character not in {" ", "\t"} for character in value)
    ):
        # env -S has its own escape, substitution, and whitespace grammar.
        # Only parse the conservative subset that is equivalent to shlex;
        # unsupported forms fail closed instead of inventing a utility.
        return "diagnostic"
    try:
        split = shlex.split(value)
    except ValueError:
        return "diagnostic"
    if not split or not split[0]:
        return "diagnostic"
    return _env_command_family("env", [*split, *remaining]) or "diagnostic"


def _parse_env_short_option(value: str) -> tuple[str, str | None]:
    """Parse a BSD/GNU env short-option cluster without consuming its utility."""
    if not value.startswith("-") or value.startswith("--") or value == "-":
        return ("invalid", None)
    for index, option in enumerate(value[1:], start=1):
        if option in {"i", "v"}:
            continue
        if option == "0":
            return ("non_executing", None)
        if option in {"C", "P", "a", "u"}:
            attached = value[index + 1 :]
            return ("value_attached", attached) if attached else ("value_separate", None)
        if option == "S":
            attached = value[index + 1 :]
            return ("split_attached", attached) if attached else ("split_separate", None)
        return ("invalid", None)
    return ("flag", None)


def _is_env_assignment(value: str) -> bool:
    name, separator, _ = value.partition("=")
    return bool(name and separator)


def _uv_command_family(executable: str, argv: Sequence[str]) -> str | None:
    """Classify the command executed by ``uv run``, not its environment options."""
    if executable != "uv":
        return None

    run_index = _uv_run_index(argv)
    if run_index is None:
        return "diagnostic"
    wrapped = list(argv[run_index + 1 :])
    index = 0
    while index < len(wrapped):
        part = wrapped[index]
        if part == "--":
            command = wrapped[index + 1 :]
            return classify_evidence_command(command) if command else "diagnostic"
        if part in {"-m", "--module"}:
            if index + 1 >= len(wrapped):
                return "diagnostic"
            return classify_evidence_command(["python", "-m", *wrapped[index + 1 :]])
        if part.startswith("--module="):
            module = part.split("=", 1)[1]
            return classify_evidence_command(["python", "-m", module, *wrapped[index + 1 :]])
        if part in {"-s", "--script", "--gui-script"}:
            if index + 1 >= len(wrapped):
                return "diagnostic"
            return classify_evidence_command(["python", *wrapped[index + 1 :]])
        if part.startswith(("--script=", "--gui-script=")):
            script = part.split("=", 1)[1]
            return classify_evidence_command(["python", script, *wrapped[index + 1 :]])
        if part in _UV_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if any(part.startswith(f"{option}=") for option in _UV_OPTIONS_WITH_VALUES if option.startswith("--")):
            index += 1
            continue
        if part in _UV_FLAG_OPTIONS or (
            len(part) > 2 and part.startswith("-") and set(part[1:]) <= {"q", "v"}
        ):
            index += 1
            continue
        if part.startswith("-"):
            # A newly added or malformed uv option must not let its value be
            # mistaken for the command identity.
            return "diagnostic"
        return classify_evidence_command(wrapped[index:])
    return "diagnostic"


def _uv_run_index(argv: Sequence[str]) -> int | None:
    """Find uv's top-level ``run`` subcommand without trusting option values."""
    index = 0
    while index < len(argv):
        part = argv[index]
        if part == "run":
            return index
        if part in _UV_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if any(part.startswith(f"{option}=") for option in _UV_OPTIONS_WITH_VALUES if option.startswith("--")):
            index += 1
            continue
        if part in _UV_FLAG_OPTIONS or (
            len(part) > 2 and part.startswith("-") and set(part[1:]) <= {"q", "v"}
        ):
            index += 1
            continue
        return None
    return None


def _family_from_text(text: str) -> str | None:
    for family in EVIDENCE_FAMILIES:
        if family == "diagnostic":
            continue
        if any(keyword in text for keyword in _FAMILY_KEYWORDS.get(family, ())):
            return family
    return None


def _identity_family(value: str) -> str | None:
    """Classify a command identity, not an arbitrary operand or path value."""
    if not value:
        return None
    name = Path(value.lower()).name
    direct = _IDENTITY_FAMILIES.get(name)
    if direct:
        return direct
    for suffix in (".py", ".js", ".mjs", ".cjs", ".sh"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    direct = _IDENTITY_FAMILIES.get(name)
    if direct:
        return direct
    for token in re.findall(r"[a-z0-9]+", name):
        family = _IDENTITY_FAMILIES.get(token)
        if family:
            return family
    return None


def _evidence_command_argv(row: dict[str, Any]) -> list[str]:
    body = str(row.get("body_text") or "")
    for line in body.splitlines():
        if line == "stdout:":
            break
        if line.startswith("argv="):
            try:
                value = ast.literal_eval(line.removeprefix("argv="))
            except (SyntaxError, ValueError):
                continue
            if isinstance(value, (list, tuple)) and all(isinstance(part, str) for part in value):
                return list(value)
        if line.startswith("command="):
            command = line.removeprefix("command=")
            try:
                return shlex.split(command)
            except ValueError:
                return command.split()
    return []


def _python_command_family(executable: str, argv: Sequence[str]) -> str | None:
    if not executable.startswith("python"):
        return None
    if "-c" in argv:
        return "diagnostic"
    if "-m" in argv:
        module_index = argv.index("-m") + 1
        if module_index >= len(argv):
            return "diagnostic"
        module = argv[module_index].split(".", 1)[0]
        module_family = _identity_family(module)
        if module_family:
            return module_family
        return "diagnostic"
    if not argv or argv[0].startswith("-"):
        return "diagnostic"
    return _identity_family(Path(argv[0]).name) or "diagnostic"


def _package_command_family(executable: str, argv: Sequence[str]) -> str | None:
    if executable not in _PACKAGE_MANAGERS | _PACKAGE_EXECUTORS:
        return None
    action = _runner_action(executable, argv)
    if action is None:
        return "diagnostic"
    identity, identity_index = action
    identity_key = identity.lower()
    if executable in _PACKAGE_EXECUTORS:
        return _identity_family(identity) or "diagnostic"
    if identity_key in {"dlx", "exec", "x"}:
        next_index = identity_index + 1
        if next_index >= len(argv) or argv[next_index] == "--":
            return "diagnostic"
        return _identity_family(argv[next_index]) or "diagnostic"
    if identity_key == "run":
        script_index = identity_index + 1
        if script_index >= len(argv) or argv[script_index] == "--":
            return "diagnostic"
        return _identity_family(argv[script_index]) or "diagnostic"
    if identity_key in {"pack", "publish", "version"}:
        return "release"
    return _identity_family(identity) or "diagnostic"


def _structured_subcommand_family(executable: str, argv: Sequence[str]) -> str | None:
    if executable == "agentdir":
        return "doctor" if argv and argv[0].lower() == "doctor" else "diagnostic"
    if executable == "just":
        action = _runner_action(executable, argv)
        if action is None:
            return "diagnostic"
        return _identity_family(action[0]) or "diagnostic"
    if executable in _STRUCTURED_FIRST_SUBCOMMAND_FAMILIES:
        action = _runner_action(executable, argv)
        if action is None:
            return "diagnostic"
        identity, action_index = action
        identity_key = identity.lower()
        if executable in _MULTI_ACTION_CLEAN_RUNNERS and identity_key == "clean":
            action_index += 1
            if action_index >= len(argv):
                return "diagnostic"
            identity = argv[action_index]
            identity_key = identity.lower()
        if identity.startswith("-"):
            return "diagnostic"
        family = _STRUCTURED_FIRST_SUBCOMMAND_FAMILIES[executable].get(identity_key)
        if family:
            return family
        if executable in {"gradle", "gradlew"}:
            terminal_task = identity_key.rsplit(":", 1)[-1]
            return _STRUCTURED_FIRST_SUBCOMMAND_FAMILIES[executable].get(
                terminal_task,
                "diagnostic",
            )
        return "diagnostic"
    if executable == "cargo":
        action = _runner_action(executable, argv)
        identity = action[0].lower() if action else ""
        return {
            "bench": "test",
            "build": "build",
            "check": "typecheck",
            "clippy": "lint",
            "fmt": "lint",
            "package": "release",
            "publish": "release",
            "test": "test",
        }.get(identity or "", "diagnostic")
    if executable == "go":
        action = _runner_action(executable, argv)
        identity = action[0].lower() if action else ""
        return {
            "build": "build",
            "fmt": "lint",
            "test": "test",
            "vet": "lint",
        }.get(identity or "", "diagnostic")
    if executable in {"make", "gmake"}:
        action = _runner_action(executable, argv, allow_default=True)
        if action is None:
            return "diagnostic"
        identity = action[0]
        return _identity_family(identity) or ("build" if not identity else "diagnostic")
    if executable in _INLINE_INTERPRETERS and "-c" in argv:
        return "diagnostic"
    return None


def _runner_action(
    executable: str,
    argv: Sequence[str],
    *,
    allow_default: bool = False,
) -> tuple[str, int] | None:
    """Return the first action after recognized leading options."""
    value_options = _RUNNER_VALUE_OPTIONS.get(executable, set())
    flag_options = _RUNNER_FLAG_OPTIONS.get(executable, set())
    index = 0
    while index < len(argv):
        part = argv[index]
        if executable in _RUNNER_ASSIGNMENT_RUNNERS and "=" in part:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", part):
                index += 1
                continue
            return None
        if not part.startswith("-"):
            return part, index
        if part == "--" or part in flag_options:
            if part == "--":
                return None
            index += 1
            continue
        if part in value_options:
            if index + 1 >= len(argv):
                return None
            index += 2
            continue
        if any(
            part.startswith(f"{option}=")
            or (option.startswith("-") and not option.startswith("--") and part.startswith(option) and part != option)
            for option in value_options
        ):
            index += 1
            continue
        return None
    return ("", len(argv)) if allow_default else None


def evidence_failed(row: dict[str, Any]) -> bool:
    return row.get("event_type") == "tool.result" and row.get("tool_exit_code") not in (None, 0)


def evidence_truncated(row: dict[str, Any]) -> bool:
    if row.get("event_type") != "tool.result":
        return False
    # Only trust the structured prelude that run_tool writes before the
    # "stdout:" marker; captured tool output cannot spoof these lines.
    for line in str(row.get("body_text") or "").splitlines():
        if line == "stdout:":
            break
        if line in ("stdout_truncated=true", "stderr_truncated=true"):
            return True
    return False


def evidence_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_type": row.get("event_type"),
        "family": row.get("family") or classify_evidence(row),
        "tool": row.get("tool"),
        "exit_code": row.get("tool_exit_code"),
        "subject": row.get("subject"),
        "date": row.get("date_header") or row.get("date_utc") or row.get("indexed_at"),
        "path": row.get("file_path"),
        "failed": row.get("failed") if "failed" in row else evidence_failed(row),
        "truncated": row.get("truncated") if "truncated" in row else evidence_truncated(row),
    }


def evidence_failure_state(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Fold append-only tool results into historical and current failure views.

    A newer passing result resolves earlier failures in the same evidence
    family without deleting them. If the latest result in a family is still
    failing, all of that family's failures remain unresolved and visible.
    """
    tool_results = [row for row in rows if row.get("event_type") == "tool.result"]
    latest_by_family: dict[str, dict[str, Any]] = {}
    for row in tool_results:
        # A legacy/manual result without an exit code has no outcome. It stays
        # visible in history but cannot turn a prior failure into a pass.
        if row.get("tool_exit_code") is None:
            continue
        latest_by_family[str(row.get("family") or classify_evidence(row))] = row
    currently_failing = {
        family
        for family, row in latest_by_family.items()
        if (row.get("failed") if "failed" in row else evidence_failed(row))
    }
    historical = [
        row
        for row in tool_results
        if (row.get("failed") if "failed" in row else evidence_failed(row))
    ]
    unresolved = [
        row
        for row in historical
        if str(row.get("family") or classify_evidence(row)) in currently_failing
    ]
    resolved = [
        row
        for row in historical
        if str(row.get("family") or classify_evidence(row)) not in currently_failing
    ]
    return {
        "historical": historical,
        "resolved": resolved,
        "unresolved": unresolved,
    }


def filter_evidence(
    rows: list[dict[str, Any]],
    *,
    family: str | None = None,
    failed: bool = False,
) -> list[dict[str, Any]]:
    if family and family not in EVIDENCE_FAMILIES:
        raise ValueError(f"unknown evidence family: {family}")
    filtered: list[dict[str, Any]] = []
    for row in rows:
        row_family = row.get("family") or classify_evidence(row)
        row_failed = row.get("failed") if "failed" in row else evidence_failed(row)
        if family and row_family != family:
            continue
        if failed and not row_failed:
            continue
        copied = dict(row)
        copied["family"] = row_family
        copied["failed"] = row_failed
        filtered.append(copied)
    return filtered


def evidence_brief(rows: list[dict[str, Any]], *, family: str | None = None, failed: bool = False) -> dict[str, Any]:
    eligible = filter_evidence(rows, family=family, failed=False)
    filtered = filter_evidence(eligible, failed=failed)
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in EVIDENCE_FAMILIES}
    for row in filtered:
        grouped[str(row.get("family") or classify_evidence(row))].append(row)
    state_grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in EVIDENCE_FAMILIES}
    for row in eligible:
        state_grouped[str(row.get("family") or classify_evidence(row))].append(row)
    failure_state = evidence_failure_state(eligible)
    failed_rows = failure_state["historical"]
    resolved_failed_rows = failure_state["resolved"]
    unresolved_failed_rows = failure_state["unresolved"]
    families: list[dict[str, Any]] = []
    latest_by_family: dict[str, dict[str, Any]] = {}
    for name in EVIDENCE_FAMILIES:
        items = grouped[name]
        if not items:
            continue
        state_items = state_grouped[name]
        latest = evidence_ref(state_items[-1])
        family_tool_results = [
            item for item in state_items if item.get("event_type") == "tool.result"
        ]
        family_failure_state = evidence_failure_state(family_tool_results)
        latest_by_family[name] = latest
        families.append(
            {
                "family": name,
                "total": len(items),
                "failed": sum(1 for row in items if row.get("failed") or evidence_failed(row)),
                "resolved_failed": len(family_failure_state["resolved"]),
                "unresolved_failed": len(family_failure_state["unresolved"]),
                "currently_failing": bool(family_failure_state["unresolved"]),
                "latest": latest,
            }
        )
    return {
        "families": families,
        "latest_by_family": latest_by_family,
        "failed_evidence": [evidence_ref(row) for row in unresolved_failed_rows],
        "historical_failed_evidence": [evidence_ref(row) for row in failed_rows],
        "resolved_failed_evidence": [evidence_ref(row) for row in resolved_failed_rows],
        "unresolved_failed_evidence": [evidence_ref(row) for row in unresolved_failed_rows],
        "counts": {
            "total": len(filtered),
            "failed": len(failed_rows),
            "resolved_failed": len(resolved_failed_rows),
            "unresolved_failed": len(unresolved_failed_rows),
            "families": len(families),
        },
    }


def format_evidence_brief(brief: dict[str, Any]) -> str:
    lines: list[str] = []
    failed = brief.get("failed_evidence") or []
    if failed:
        lines.append("Unresolved failed evidence:")
        for item in failed:
            lines.append(_format_evidence_ref(item))
    resolved = brief.get("resolved_failed_evidence") or []
    if resolved:
        if lines:
            lines.append("")
        lines.append("Resolved historical failures:")
        for item in resolved:
            lines.append(_format_evidence_ref(item))
    families = brief.get("families") or []
    if families:
        if lines:
            lines.append("")
        lines.append("Latest evidence by family:")
        for family in families:
            lines.append(_format_evidence_ref(family["latest"], prefix=f"{family['family']}: "))
    if not lines:
        return "No evidence captured."
    return "\n".join(lines)


def timeline_rows(
    root: str | Path,
    session_id: str | None = None,
    *,
    limit: int = 100,
    rebuild: bool = True,
) -> list[dict[str, Any]]:
    resolved = resolve_review_session(root, session_id)
    if rebuild:
        ensure_index(root)
    rows = query_messages(root, session_id=resolved, limit=limit)
    return [timeline_ref(row) for row in rows]


def timeline_ref(row: dict[str, Any]) -> dict[str, Any]:
    event_type = row.get("event_type")
    item = {
        "date": row.get("date_header") or row.get("date_utc") or row.get("indexed_at"),
        "event_type": event_type,
        "subject": row.get("subject"),
        "tool": row.get("tool"),
        "exit_code": row.get("tool_exit_code"),
        "actor": row.get("from_actor"),
        "path": row.get("file_path"),
    }
    if event_type in {"tool.call", "tool.result", "file.diff"} or str(event_type or "").startswith("git.hook."):
        item["family"] = classify_evidence(row)
        item["failed"] = evidence_failed(row)
    return item


def format_timeline(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        date = row.get("date") or "unknown-date"
        event_type = row.get("event_type") or "unknown"
        subject = row.get("subject") or ""
        tool = row.get("tool") or ""
        exit_code = row.get("exit_code")
        family = row.get("family") or ""
        detail_parts = []
        if tool:
            detail_parts.append(str(tool))
        if family:
            detail_parts.append(f"family={family}")
        if exit_code is not None:
            detail_parts.append(f"exit={exit_code}")
        detail = " ".join(detail_parts)
        lines.append(f"{date} {event_type} {detail} {subject}".rstrip())
    return "\n".join(lines)


def format_evidence(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        date = row.get("date_header") or row.get("indexed_at") or "unknown-date"
        event_type = row.get("event_type") or "unknown"
        tool = row.get("tool") or ""
        exit_code = row.get("tool_exit_code")
        subject = row.get("subject") or ""
        file_path = row.get("file_path") or ""
        family = row.get("family") or classify_evidence(row)
        detail = f" exit={exit_code}" if exit_code is not None else ""
        family_detail = f" family={family}" if family else ""
        truncated_detail = " truncated=true" if (row.get("truncated") if "truncated" in row else evidence_truncated(row)) else ""
        lines.append(f"{date} {event_type} {tool}{detail}{family_detail}{truncated_detail} {subject} [{file_path}]")
    return "\n".join(lines)


def _format_evidence_ref(item: dict[str, Any], *, prefix: str = "- ") -> str:
    tool = item.get("tool") or ""
    exit_code = item.get("exit_code")
    subject = item.get("subject") or ""
    date = item.get("date") or "unknown-date"
    exit_text = f" exit={exit_code}" if exit_code is not None else ""
    failed = " failed=true" if item.get("failed") else ""
    truncated = " truncated=true" if item.get("truncated") else ""
    return f"{prefix}{date} {item.get('event_type') or 'unknown'} {tool}{exit_text}{failed}{truncated} {subject}".rstrip()
