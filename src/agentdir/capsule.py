from __future__ import annotations

import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, replace
from email.utils import make_msgid
from pathlib import Path
from typing import Iterator

from .capture import DEFAULT_MAX_CAPTURE_BYTES, run_tool_detailed
from .envelope import ParsedEnvelope, parse_envelope
from .events import emit_event
from .git import git_head, workspace_name
from .index import rebuild_index, update_index
from .query import query_messages
from .sessions import ensure_session
from .store import AgentDirError, paths_for, require_root

CAPSULE_MODES = ("copy", "readonly", "write-through")
CHAIN_GENESIS = hashlib.sha256(b"agentdir-capsule-chain/v1").hexdigest()
ATTESTATION_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
ATTESTATION_PREDICATE_TYPE = "https://agentdir.dev/attestations/capsule-run/v1"

FLAKE_VERDICT_EXIT_CODES = {"stable-pass": 0, "flaky": 1, "stable-fail": 2}
VERIFY_VERDICT_EXIT_CODES = {"exact": 0, "verified": 0, "diverged": 1, "cannot-verify": 2}


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
    image_digest: str | None = None
    source_tree: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CapsuleReceipt:
    plan: CapsulePlan
    exit_code: int
    duration_ms: int
    stdout_sha256: str
    stderr_sha256: str
    stdout_truncated: bool
    stderr_truncated: bool
    max_capture_bytes: int
    redact: bool
    session_id: str
    plan_event_id: str
    receipt_event_id: str
    chain_seq: int
    chain_hash: str

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


def pin_capsule_plan(plan: CapsulePlan) -> CapsulePlan:
    """Resolve the inputs that make a capsule receipt re-derivable.

    Best-effort: a missing digest (image not pulled yet) or tree hash (no git,
    unreadable source) leaves the field None rather than failing the run.
    """
    return replace(
        plan,
        image_digest=plan.image_digest or resolve_image_digest(plan.image, plan.container_bin),
        source_tree=plan.source_tree or source_tree_hash(plan.source),
    )


def resolve_image_digest(image: str, container_bin: str = "container") -> str | None:
    if not image:
        return None
    try:
        result = subprocess.run(
            [container_bin, "image", "inspect", image],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or not payload:
        return None
    descriptor = payload[0].get("configuration", {}).get("descriptor", {})
    digest = descriptor.get("digest")
    return str(digest) if digest else None


def source_tree_hash(source: str | Path) -> str | None:
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_dir():
        return None
    tree = _git_tree_hash(source_path)
    if tree:
        return f"git-tree:{tree}"
    digest = _walk_tree_hash(source_path)
    return f"dir-sha256:{digest}" if digest else None


def _git_tree_hash(source_path: Path) -> str | None:
    # A throwaway index lets `git add -A` hash tracked + untracked files
    # (respecting .gitignore) without touching the real staging area. The
    # evidence store itself is dropped from the index afterwards so capsule
    # events never change the tree they pin — `git rm` rather than an
    # `:(exclude)` pathspec because the latter makes git refuse pathspecs
    # that match gitignored paths.
    with tempfile.TemporaryDirectory(prefix="agentdir-tree-") as tmp:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")
        try:
            added = subprocess.run(
                ["git", "add", "-A", "--", "."],
                cwd=source_path,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            if added.returncode != 0:
                return None
            subprocess.run(
                ["git", "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", ".agentdir"],
                cwd=source_path,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            tree = subprocess.run(
                ["git", "write-tree"],
                cwd=source_path,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError:
            return None
    if tree.returncode != 0:
        return None
    return tree.stdout.strip() or None


def _walk_tree_hash(source_path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        files = sorted(
            path
            for path in source_path.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    except OSError:
        return None
    for path in files:
        rel = path.relative_to(source_path).as_posix()
        if rel.startswith((".git/", ".agentdir/")):
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


def format_capsule_plan(plan: CapsulePlan) -> str:
    lines = [
        f"runtime={plan.runtime}",
        f"image={plan.image}",
        f"mode={plan.mode}",
        f"source={plan.source}",
        f"workdir={plan.workdir}",
        f"command={' '.join(shlex.quote(part) for part in plan.command)}",
    ]
    if plan.image_digest:
        lines.append(f"image_digest={plan.image_digest}")
    if plan.source_tree:
        lines.append(f"source_tree={plan.source_tree}")
    if plan.cpus:
        lines.append(f"cpus={plan.cpus}")
    if plan.memory:
        lines.append(f"memory={plan.memory}")
    if plan.platform:
        lines.append(f"platform={plan.platform}")
    lines.append(f"container_argv={' '.join(shlex.quote(part) for part in plan.container_argv)}")
    return "\n".join(lines)


def normalize_event_id(event_id: str) -> str:
    return event_id.strip().strip("<>").strip()


def _chain_path(root: str | Path) -> Path:
    return paths_for(root).state / "capsule.chain"


def _chain_tail(root: str | Path) -> tuple[int, str]:
    path = _chain_path(root)
    if not path.is_file():
        return 0, CHAIN_GENESIS
    tail: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            tail = line
    if tail is None:
        return 0, CHAIN_GENESIS
    parts = tail.split()
    if len(parts) != 4:
        raise AgentDirError(
            f"Malformed capsule chain ledger at {path}; inspect it with `agentdir capsule chain --check`"
        )
    return int(parts[0]), parts[3]


def _chain_append(root: str | Path, *, event_path: Path, message_id: str) -> tuple[int, str]:
    seq, prev_hash = _chain_tail(root)
    event_sha = hashlib.sha256(event_path.read_bytes()).hexdigest()
    chain_hash = hashlib.sha256(f"{prev_hash}{event_sha}".encode("utf-8")).hexdigest()
    path = _chain_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{seq + 1} {message_id} {event_sha} {chain_hash}\n")
    return seq + 1, chain_hash


def _emit_chained(
    root: str | Path,
    *,
    session_id: str,
    event_type: str,
    subject: str,
    body: str,
    parent_message_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[str, int, str]:
    message_id = make_msgid(domain="agentdir.local")
    cwd_path = Path.cwd().resolve()
    seq, prev_hash = _chain_tail(root)
    headers: dict[str, str] = dict(extra_headers or {})
    headers["X-AgentDir-Chain-Seq"] = str(seq + 1)
    headers["X-AgentDir-Chain-Prev"] = prev_hash
    emitted = emit_event(
        root,
        session_id=session_id,
        event_type=event_type,
        subject=subject,
        from_actor="agent",
        body=body,
        workspace=workspace_name(cwd_path),
        git_head=git_head(cwd_path),
        tool="capsule",
        parent_message_id=parent_message_id,
        extra_headers=headers,
        message_id=message_id,
    )
    chain_seq, chain_hash = _chain_append(root, event_path=emitted.path, message_id=message_id)
    return message_id, chain_seq, chain_hash


def emit_capsule_plan(
    root: str | Path,
    *,
    session_id: str | None,
    plan: CapsulePlan,
) -> tuple[str, str]:
    session = ensure_session(root, session_id, title=f"AgentDir capsule: {plan.image}")
    body = json.dumps(plan.as_dict(), indent=2, sort_keys=True)
    message_id, _, _ = _emit_chained(
        root,
        session_id=session.session_id,
        event_type="runtime.capsule",
        subject=f"runtime.capsule {plan.runtime} {plan.image}",
        body=body,
        extra_headers={
            "X-AgentDir-Runtime": plan.runtime,
            "X-AgentDir-Capsule-Mode": plan.mode,
            "X-AgentDir-Capsule-Image": plan.image,
        },
    )
    return session.session_id, message_id


def run_capsule_receipt(
    root: str | Path,
    *,
    plan: CapsulePlan,
    session_id: str | None = None,
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
    redact: bool = True,
    plan_event_id: str | None = None,
) -> CapsuleReceipt:
    if plan_event_id is None:
        resolved_session, plan_event_id = emit_capsule_plan(root, session_id=session_id, plan=plan)
    else:
        resolved_session = ensure_session(
            root, session_id, title=f"AgentDir capsule: {plan.image}"
        ).session_id
    outcome = run_tool_detailed(
        root,
        argv=plan.container_argv,
        session_id=resolved_session,
        tool_name="capsule",
        cwd=Path.cwd(),
        max_capture_bytes=max_capture_bytes,
        redact=redact,
    )
    # The run pulls the image if absent, so a digest that could not be pinned
    # before execution is usually resolvable now.
    pinned = plan
    if pinned.image_digest is None:
        pinned = replace(pinned, image_digest=resolve_image_digest(plan.image, plan.container_bin))

    result_payload = {
        "exit_code": outcome.exit_code,
        "duration_ms": outcome.duration_ms,
        "stdout_sha256": outcome.stdout_sha256,
        "stderr_sha256": outcome.stderr_sha256,
        "stdout_truncated": outcome.stdout_truncated,
        "stderr_truncated": outcome.stderr_truncated,
        "max_capture_bytes": max_capture_bytes,
        "redact": redact,
        "session_id": resolved_session,
        "plan_event_id": normalize_event_id(plan_event_id),
    }
    body = json.dumps({"plan": pinned.as_dict(), "result": result_payload}, indent=2, sort_keys=True)
    receipt_event_id, chain_seq, chain_hash = _emit_chained(
        root,
        session_id=resolved_session,
        event_type="runtime.capsule.result",
        subject=f"runtime.capsule.result {pinned.image} exit {outcome.exit_code}",
        body=body,
        parent_message_id=plan_event_id,
        extra_headers={
            "X-AgentDir-Runtime": pinned.runtime,
            "X-AgentDir-Capsule-Mode": pinned.mode,
            "X-AgentDir-Capsule-Image": pinned.image,
            "X-AgentDir-Tool-Exit-Code": str(outcome.exit_code),
        },
    )
    return CapsuleReceipt(
        plan=pinned,
        exit_code=outcome.exit_code,
        duration_ms=outcome.duration_ms,
        stdout_sha256=outcome.stdout_sha256,
        stderr_sha256=outcome.stderr_sha256,
        stdout_truncated=outcome.stdout_truncated,
        stderr_truncated=outcome.stderr_truncated,
        max_capture_bytes=max_capture_bytes,
        redact=redact,
        session_id=resolved_session,
        plan_event_id=normalize_event_id(plan_event_id),
        receipt_event_id=normalize_event_id(receipt_event_id),
        chain_seq=chain_seq,
        chain_hash=chain_hash,
    )


def run_capsule(
    root: str | Path,
    *,
    plan: CapsulePlan,
    session_id: str | None = None,
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
    redact: bool = True,
) -> int:
    return run_capsule_receipt(
        root,
        plan=plan,
        session_id=session_id,
        max_capture_bytes=max_capture_bytes,
        redact=redact,
    ).exit_code


def _iter_event_files(root: str | Path) -> Iterator[Path]:
    paths = require_root(root)
    for state in ("new", "cur"):
        for path in sorted(paths.sessions.glob(f"*/Maildir/{state}/*")):
            if path.is_file():
                yield path


def find_event(root: str | Path, event_id: str) -> ParsedEnvelope | None:
    wanted = normalize_event_id(event_id)
    if not wanted:
        return None
    for path in _iter_event_files(root):
        try:
            parsed = parse_envelope(path)
        except Exception:
            continue
        message_id = parsed.message_id
        if message_id and normalize_event_id(message_id) == wanted:
            return parsed
    return None


def _load_receipt(root: str | Path, event_id: str) -> ParsedEnvelope:
    parsed = find_event(root, event_id)
    if parsed is None:
        raise AgentDirError(f"No capsule event found for id {event_id!r}")
    event_type = parsed.message.get("X-AgentDir-Event-Type")
    if event_type == "runtime.capsule.result":
        return parsed
    if event_type == "runtime.capsule":
        plan_id = normalize_event_id(parsed.message_id or "")
        children: list[ParsedEnvelope] = []
        for path in _iter_event_files(root):
            try:
                candidate = parse_envelope(path)
            except Exception:
                continue
            if candidate.message.get("X-AgentDir-Event-Type") != "runtime.capsule.result":
                continue
            if normalize_event_id(candidate.message.get("In-Reply-To") or "") == plan_id:
                children.append(candidate)
        if not children:
            raise AgentDirError(
                f"Capsule plan {event_id!r} has no recorded receipt; run the capsule first"
            )
        children.sort(key=lambda env: int(env.message.get("X-AgentDir-Created-Ns") or 0))
        return children[-1]
    raise AgentDirError(
        f"Event {event_id!r} is type {event_type!r}; expected runtime.capsule or runtime.capsule.result"
    )


def _receipt_payload(envelope: ParsedEnvelope) -> tuple[dict, dict]:
    try:
        payload = json.loads(envelope.body_text)
        plan_data = dict(payload["plan"])
        result_data = dict(payload["result"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise AgentDirError(f"Capsule receipt {envelope.message_id!r} has an unreadable body: {exc}")
    return plan_data, result_data


def verify_capsule(
    root: str | Path,
    event_id: str,
    *,
    force: bool = False,
    session_id: str | None = None,
) -> dict[str, object]:
    receipt_env = _load_receipt(root, event_id)
    plan_data, result_data = _receipt_payload(receipt_env)
    receipt_id = normalize_event_id(receipt_env.message_id or event_id)

    recorded_tree = plan_data.get("source_tree")
    current_tree = source_tree_hash(plan_data.get("source") or Path.cwd())
    recorded_digest = plan_data.get("image_digest")
    current_digest = resolve_image_digest(
        str(plan_data.get("image") or ""), str(plan_data.get("container_bin") or "container")
    )
    source_tree_match = None if not recorded_tree else current_tree == recorded_tree
    image_digest_match = (
        None if not (recorded_digest and current_digest) else current_digest == recorded_digest
    )

    blockers: list[str] = []
    if source_tree_match is False:
        blockers.append("source tree has changed since the receipt was recorded")
    if image_digest_match is False:
        blockers.append("image digest no longer matches the receipt")

    session = ensure_session(
        root, session_id, title=f"AgentDir capsule verify: {plan_data.get('image', '?')}"
    )
    report: dict[str, object] = {
        "receipt_event_id": receipt_id,
        "plan_event_id": result_data.get("plan_event_id"),
        "image": plan_data.get("image"),
        "image_digest_recorded": recorded_digest,
        "image_digest_current": current_digest,
        "image_digest_match": image_digest_match,
        "source_tree_recorded": recorded_tree,
        "source_tree_current": current_tree,
        "source_tree_match": source_tree_match,
        "recorded_exit_code": result_data.get("exit_code"),
        "blockers": blockers,
        "forced": force,
    }

    if blockers and not force:
        report["verdict"] = "cannot-verify"
    else:
        outcome = run_tool_detailed(
            root,
            argv=[str(part) for part in plan_data.get("container_argv") or []],
            session_id=session.session_id,
            tool_name="capsule-verify",
            cwd=Path.cwd(),
            max_capture_bytes=int(result_data.get("max_capture_bytes") or DEFAULT_MAX_CAPTURE_BYTES),
            redact=bool(result_data.get("redact", True)),
        )
        exit_match = outcome.exit_code == result_data.get("exit_code")
        stdout_match = outcome.stdout_sha256 == result_data.get("stdout_sha256")
        stderr_match = outcome.stderr_sha256 == result_data.get("stderr_sha256")
        if exit_match and stdout_match and stderr_match:
            verdict = "exact"
        elif exit_match:
            verdict = "verified"
        else:
            verdict = "diverged"
        report.update(
            {
                "verdict": verdict,
                "replayed_exit_code": outcome.exit_code,
                "stdout_match": stdout_match,
                "stderr_match": stderr_match,
            }
        )

    verify_event_id, _, _ = _emit_chained(
        root,
        session_id=session.session_id,
        event_type="runtime.capsule.verify",
        subject=f"runtime.capsule.verify {report['verdict']} {plan_data.get('image', '?')}",
        body=json.dumps(report, indent=2, sort_keys=True),
        parent_message_id=receipt_env.message_id,
        extra_headers={
            "X-AgentDir-Capsule-Image": str(plan_data.get("image") or ""),
            "X-AgentDir-Capsule-Verdict": str(report["verdict"]),
        },
    )
    report["verify_event_id"] = normalize_event_id(verify_event_id)
    return report


def build_capsule_attestation(root: str | Path, event_id: str) -> dict[str, object]:
    receipt_env = _load_receipt(root, event_id)
    plan_data, result_data = _receipt_payload(receipt_env)
    source_tree = str(plan_data.get("source_tree") or "")
    if source_tree.startswith("git-tree:"):
        subject_digest = {"gitTree": source_tree.split(":", 1)[1]}
    elif source_tree.startswith("dir-sha256:"):
        subject_digest = {"sha256": source_tree.split(":", 1)[1]}
    else:
        raise AgentDirError(
            "Capsule receipt has no pinned source tree; rerun `agentdir capsule run` without --no-pin"
        )
    chain_seq = receipt_env.message.get("X-AgentDir-Chain-Seq")
    chain_prev = receipt_env.message.get("X-AgentDir-Chain-Prev")
    return {
        "_type": ATTESTATION_STATEMENT_TYPE,
        "subject": [
            {
                "name": str(plan_data.get("source") or "source"),
                "digest": subject_digest,
            }
        ],
        "predicateType": ATTESTATION_PREDICATE_TYPE,
        "predicate": {
            "runtime": plan_data.get("runtime"),
            "image": plan_data.get("image"),
            "imageDigest": plan_data.get("image_digest"),
            "mode": plan_data.get("mode"),
            "command": plan_data.get("command"),
            "containerArgv": plan_data.get("container_argv"),
            "exitCode": result_data.get("exit_code"),
            "stdoutSha256": result_data.get("stdout_sha256"),
            "stderrSha256": result_data.get("stderr_sha256"),
            "durationMs": result_data.get("duration_ms"),
            "sessionId": result_data.get("session_id"),
            "planEventId": result_data.get("plan_event_id"),
            "receiptEventId": normalize_event_id(receipt_env.message_id or event_id),
            "recordedAt": receipt_env.message.get("Date"),
            "chain": {
                "seq": int(chain_seq) if chain_seq else None,
                "prevHash": chain_prev,
            },
        },
    }


def chain_status(root: str | Path) -> dict[str, object]:
    require_root(root)
    seq, head = _chain_tail(root)
    return {"length": seq, "head": head, "ledger": str(_chain_path(root))}


def verify_chain(root: str | Path) -> dict[str, object]:
    require_root(root)
    path = _chain_path(root)
    if not path.is_file():
        return {"ok": True, "length": 0, "head": CHAIN_GENESIS, "problems": []}

    events_by_id: dict[str, ParsedEnvelope] = {}
    for event_path in _iter_event_files(root):
        try:
            parsed = parse_envelope(event_path)
        except Exception:
            continue
        if parsed.message_id:
            events_by_id.setdefault(normalize_event_id(parsed.message_id), parsed)

    problems: list[str] = []
    prev_hash = CHAIN_GENESIS
    expected_seq = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4:
            problems.append(f"malformed ledger line: {line!r}")
            continue
        seq_text, message_id, event_sha, chain_hash = parts
        expected_seq += 1
        if seq_text != str(expected_seq):
            problems.append(f"ledger sequence gap: expected {expected_seq}, found {seq_text}")
        parsed = events_by_id.get(normalize_event_id(message_id))
        if parsed is None:
            problems.append(f"entry {seq_text}: event {message_id} is missing from the store")
        else:
            actual_sha = hashlib.sha256(parsed.raw).hexdigest()
            if actual_sha != event_sha:
                problems.append(
                    f"entry {seq_text}: event {message_id} content hash mismatch — the event was modified"
                )
            header_prev = parsed.message.get("X-AgentDir-Chain-Prev")
            if header_prev != prev_hash:
                problems.append(
                    f"entry {seq_text}: event {message_id} chain-prev header does not match the ledger"
                )
        recomputed = hashlib.sha256(f"{prev_hash}{event_sha}".encode("utf-8")).hexdigest()
        if recomputed != chain_hash:
            problems.append(f"entry {seq_text}: chain hash mismatch — the ledger was rewritten")
        prev_hash = chain_hash
    return {"ok": not problems, "length": expected_seq, "head": prev_hash, "problems": problems}


_TOOL_FAMILIES = {
    "node": "node",
    "npm": "node",
    "pnpm": "node",
    "yarn": "node",
    "npx": "node",
    "vitest": "node",
    "jest": "node",
    "tsc": "node",
    "eslint": "node",
    "python": "python",
    "python3": "python",
    "pytest": "python",
    "pip": "python",
    "uv": "python",
    "ruff": "python",
    "mypy": "python",
    "cargo": "rust",
    "rustc": "rust",
    "go": "go",
    "gofmt": "go",
    "ruby": "ruby",
    "bundle": "ruby",
    "rake": "ruby",
}

_FAMILY_IMAGES = {
    "node": "node:22",
    "python": "python:3.12",
    "rust": "rust:1",
    "go": "golang:1",
    "ruby": "ruby:3",
}

_MANIFEST_FAMILIES = {
    "package.json": "node",
    "pnpm-lock.yaml": "node",
    "yarn.lock": "node",
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "setup.py": "python",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "Gemfile": "ruby",
}

_DEFAULT_IMAGE = "debian:stable-slim"


def infer_habitat(
    root: str | Path,
    source: str | Path | None = None,
    *,
    limit: int = 2000,
) -> dict[str, object]:
    source_path = Path(source or Path.cwd()).expanduser().resolve()
    require_root(root)
    try:
        update_index(root)
    except sqlite3.DatabaseError:
        rebuild_index(root)
    rows = query_messages(root, event_type="tool.call", limit=limit)

    tool_counts: Counter[str] = Counter()
    for row in rows:
        tool = str(row.get("tool") or "").strip()
        if tool:
            tool_counts[tool] += 1

    family_scores: Counter[str] = Counter()
    for tool, count in tool_counts.items():
        family = _TOOL_FAMILIES.get(tool)
        if family:
            family_scores[family] += count
    manifests = sorted(name for name in _MANIFEST_FAMILIES if (source_path / name).exists())
    for name in manifests:
        family_scores[_MANIFEST_FAMILIES[name]] += 1

    family = family_scores.most_common(1)[0][0] if family_scores else None
    image = _FAMILY_IMAGES.get(family or "", _DEFAULT_IMAGE)
    containerfile = _render_containerfile(image, family, tool_counts, manifests)
    return {
        "image": image,
        "family": family or "unknown",
        "observed_tools": dict(sorted(tool_counts.items())),
        "manifests": manifests,
        "tool_calls_scanned": len(rows),
        "containerfile": containerfile,
    }


def _render_containerfile(
    image: str,
    family: str | None,
    tool_counts: Counter[str],
    manifests: list[str],
) -> str:
    lines = ["# Containerfile inferred by AgentDir from recorded evidence"]
    if tool_counts:
        observed = ", ".join(f"{tool} ({count})" for tool, count in tool_counts.most_common())
        lines.append(f"# Observed tools: {observed}")
    else:
        lines.append("# No tool.call evidence recorded yet; inferred from repo manifests only")
    if manifests:
        lines.append(f"# Manifests: {', '.join(manifests)}")
    lines.extend([f"FROM {image}", "WORKDIR /work"])
    if family == "node":
        lines.append("RUN corepack enable")
    if family == "python":
        lines.append("RUN pip install --no-cache-dir uv")
    return "\n".join(lines) + "\n"


def run_capsule_flake(
    root: str | Path,
    *,
    plan: CapsulePlan,
    runs: int,
    session_id: str | None = None,
    max_capture_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
    redact: bool = True,
) -> dict[str, object]:
    if runs < 2:
        raise AgentDirError("flake detection needs --runs of at least 2")
    resolved_session, plan_event_id = emit_capsule_plan(root, session_id=session_id, plan=plan)
    receipts = [
        run_capsule_receipt(
            root,
            plan=plan,
            session_id=resolved_session,
            max_capture_bytes=max_capture_bytes,
            redact=redact,
            plan_event_id=plan_event_id,
        )
        for _ in range(runs)
    ]

    exit_codes = [receipt.exit_code for receipt in receipts]
    passes = sum(1 for code in exit_codes if code == 0)
    if passes == runs:
        verdict = "stable-pass"
    elif passes == 0 and len(set(exit_codes)) == 1:
        verdict = "stable-fail"
    else:
        verdict = "flaky"

    summary: dict[str, object] = {
        "verdict": verdict,
        "runs": runs,
        "passes": passes,
        "failures": runs - passes,
        "exit_codes": exit_codes,
        "distinct_stdout_hashes": len({receipt.stdout_sha256 for receipt in receipts}),
        "image": plan.image,
        "image_digest": receipts[-1].plan.image_digest,
        "source_tree": plan.source_tree,
        "command": plan.command,
        "session_id": resolved_session,
        "plan_event_id": normalize_event_id(plan_event_id),
        "receipt_event_ids": [receipt.receipt_event_id for receipt in receipts],
    }
    flake_event_id, _, _ = _emit_chained(
        root,
        session_id=resolved_session,
        event_type="runtime.capsule.flake",
        subject=f"runtime.capsule.flake {verdict} {plan.image} {passes}/{runs}",
        body=json.dumps(summary, indent=2, sort_keys=True),
        parent_message_id=plan_event_id,
        extra_headers={
            "X-AgentDir-Capsule-Image": plan.image,
            "X-AgentDir-Capsule-Verdict": verdict,
        },
    )
    summary["flake_event_id"] = normalize_event_id(flake_event_id)
    return summary
