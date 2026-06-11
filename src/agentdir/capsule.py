from __future__ import annotations

import json
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path

from .capture import DEFAULT_MAX_CAPTURE_BYTES, run_tool
from .events import emit_event
from .git import git_head, workspace_name
from .sessions import ensure_session
from .store import AgentDirError

CAPSULE_MODES = ("copy", "readonly", "write-through")


@dataclass(frozen=True)
class CapsulePlan:
    runtime: str
    image: str
    mode: str
    source: str
    command: list[str]
    container_bin: str
    container_argv: list[str]
    cpus: str | None = None
    memory: str | None = None
    platform: str | None = None
    source_target: str = "/src"
    workdir: str = "/work"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def build_capsule_plan(
    *,
    image: str,
    command: list[str],
    source: str | Path | None = None,
    mode: str = "copy",
    container_bin: str = "container",
    cpus: str | None = None,
    memory: str | None = None,
    platform: str | None = None,
) -> CapsulePlan:
    if not image:
        raise AgentDirError("capsule image is required")
    if not command:
        raise AgentDirError("Usage: agentdir capsule run --image <image> -- <command> [args...]")
    if mode not in CAPSULE_MODES:
        expected = ", ".join(CAPSULE_MODES)
        raise AgentDirError(f"Invalid capsule mode {mode!r}; expected one of {expected}")

    source_path = Path(source or Path.cwd()).expanduser().resolve()
    argv = [container_bin, "run", "--rm", "--init"]
    if cpus:
        argv.extend(["--cpus", cpus])
    if memory:
        argv.extend(["--memory", memory])
    if platform:
        argv.extend(["--platform", platform])

    if mode == "copy":
        source_target = "/src"
        workdir = "/work"
        argv.extend(["--mount", f"type=bind,source={source_path},target={source_target},readonly"])
        script = copy_mode_script(command, source_target=source_target, workdir=workdir)
        argv.extend([image, "sh", "-lc", script])
    else:
        source_target = "/workspace"
        workdir = source_target
        readonly = ",readonly" if mode == "readonly" else ""
        argv.extend(["--mount", f"type=bind,source={source_path},target={source_target}{readonly}"])
        argv.extend(["--workdir", workdir, image, *command])

    return CapsulePlan(
        runtime="apple-container",
        image=image,
        mode=mode,
        source=str(source_path),
        command=list(command),
        container_bin=container_bin,
        container_argv=argv,
        cpus=cpus,
        memory=memory,
        platform=platform,
        source_target=source_target,
        workdir=workdir,
    )


def copy_mode_script(
    command: list[str],
    *,
    source_target: str = "/src",
    workdir: str = "/work",
) -> str:
    quoted_command = " ".join(shlex.quote(part) for part in command)
    return "\n".join(
        [
            "set -eu",
            f"rm -rf {shlex.quote(workdir)}",
            f"mkdir -p {shlex.quote(workdir)}",
            f"cp -a {shlex.quote(source_target)}/. {shlex.quote(workdir)}/",
            f"cd {shlex.quote(workdir)}",
            f"exec {quoted_command}",
        ]
    )


def format_capsule_plan(plan: CapsulePlan) -> str:
    lines = [
        f"runtime={plan.runtime}",
        f"image={plan.image}",
        f"mode={plan.mode}",
        f"source={plan.source}",
        f"workdir={plan.workdir}",
        f"command={' '.join(shlex.quote(part) for part in plan.command)}",
    ]
    if plan.cpus:
        lines.append(f"cpus={plan.cpus}")
    if plan.memory:
        lines.append(f"memory={plan.memory}")
    if plan.platform:
        lines.append(f"platform={plan.platform}")
    lines.append(f"container_argv={' '.join(shlex.quote(part) for part in plan.container_argv)}")
    return "\n".join(lines)


def emit_capsule_plan(
    root: str | Path,
    *,
    session_id: str | None,
    plan: CapsulePlan,
) -> str:
    session = ensure_session(root, session_id, title=f"AgentDir capsule: {plan.image}")
    cwd_path = Path.cwd().resolve()
    body = json.dumps(plan.as_dict(), indent=2, sort_keys=True)
    emit_event(
        root,
        session_id=session.session_id,
        event_type="runtime.capsule",
        subject=f"runtime.capsule {plan.runtime} {plan.image}",
        from_actor="agent",
        body=body,
        workspace=workspace_name(cwd_path),
        git_head=git_head(cwd_path),
        tool="capsule",
        extra_headers={
            "X-AgentDir-Runtime": plan.runtime,
            "X-AgentDir-Capsule-Mode": plan.mode,
            "X-AgentDir-Capsule-Image": plan.image,
        },
    )
    return session.session_id


def run_capsule(
    root: str | Path,
    *,
    plan: CapsulePlan,
    session_id: str | None = None,
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
    redact: bool = True,
) -> int:
    resolved_session = emit_capsule_plan(root, session_id=session_id, plan=plan)
    return run_tool(
        root,
        argv=plan.container_argv,
        session_id=resolved_session,
        tool_name="capsule",
        cwd=Path.cwd(),
        max_capture_bytes=max_capture_bytes,
        redact=redact,
    )
