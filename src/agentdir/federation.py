from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .index import rebuild_index
from .memory import DEFAULT_MIN_SCORE, RETRIEVAL_HYBRID, search_memory
from .rendering import rich_root_diagnostics
from .store import AgentDirError, paths_for, require_root, validate_id

ROOT_REGISTRY_FILE = "registered-roots.json"
VISIBILITY_CHOICES = ("private", "team", "machine")


def register_root(
    controller_root: str | Path,
    root: str | Path,
    *,
    name: str | None = None,
    visibility: str = "private",
) -> dict[str, Any]:
    if visibility not in VISIBILITY_CHOICES:
        raise AgentDirError(
            f"Invalid visibility {visibility!r}; expected one of {', '.join(VISIBILITY_CHOICES)}"
        )
    controller = require_root(controller_root)
    source_root = resolve_registered_root(root)
    root_id = root_id_for_path(source_root)
    registry = _read_registry(controller.root)
    entry = {
        "root_id": root_id,
        "name": name or default_root_name(source_root),
        "root_path": str(source_root),
        "visibility": visibility,
        "registered_at": now_iso(),
    }
    roots = [item for item in registry["roots"] if item["root_id"] != root_id]
    roots.append(entry)
    registry["roots"] = sorted(roots, key=lambda item: (item["name"], item["root_id"]))
    _write_registry(controller.root, registry)
    return entry


def list_registered_roots(controller_root: str | Path, *, group: str | None = None) -> list[dict[str, Any]]:
    controller = require_root(controller_root)
    registry = _read_registry(controller.root)
    roots = registry["roots"]
    if group:
        wanted = set(_root_ids_for_group(registry, group))
        roots = [root for root in roots if root["root_id"] in wanted]
    return [{**root, "available": Path(root["root_path"]).joinpath("VERSION").is_file()} for root in roots]


def remove_registered_root(controller_root: str | Path, identifier: str) -> dict[str, Any]:
    controller = require_root(controller_root)
    registry = _read_registry(controller.root)
    remaining: list[dict[str, Any]] = []
    removed: dict[str, Any] | None = None
    for root in registry["roots"]:
        if identifier in {root["root_id"], root["name"], root["root_path"]}:
            removed = root
        else:
            remaining.append(root)
    if removed is None:
        raise AgentDirError(f"Unknown registered root: {identifier}")
    registry["roots"] = remaining
    _write_registry(controller.root, registry)
    return removed


def rebuild_registered_roots(
    controller_root: str | Path,
    *,
    group: str | None = None,
    stale_only: bool = False,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    stale_ids = {
        item["root_id"]
        for item in doctor_registered_roots(controller_root, group=group)
        if item.get("stale")
    } if stale_only else set()
    for root in list_registered_roots(controller_root, group=group):
        item = {**root}
        if stale_only and root["root_id"] not in stale_ids:
            item["ok"] = True
            item["skipped"] = True
            item["reason"] = "fresh"
            results.append(item)
            continue
        if not root["available"]:
            item["ok"] = False
            item["error"] = "missing root"
            results.append(item)
            continue
        try:
            result = rebuild_index(root["root_path"])
        except AgentDirError as exc:
            item["ok"] = False
            item["error"] = str(exc)
        else:
            item["ok"] = True
            item["indexed"] = result.indexed
            item["malformed"] = result.malformed
            item["duplicates"] = result.duplicates
        results.append(item)
    return results


def search_federated_memory(
    controller_root: str | Path,
    query: str,
    *,
    session_id: str | None = None,
    event_type: str | None = None,
    actor: str | None = None,
    task_id: str | None = None,
    tool: str | None = None,
    git_head: str | None = None,
    workspace: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10,
    min_score: float = DEFAULT_MIN_SCORE,
    retrieval_mode: str = RETRIEVAL_HYBRID,
    rebuild: bool = True,
    group: str | None = None,
) -> list[dict[str, Any]]:
    roots = list_registered_roots(controller_root, group=group)
    if not roots:
        if group:
            raise AgentDirError(f"No registered roots in group {group!r}")
        raise AgentDirError("No registered roots; run agentdir roots register <root>")

    per_root_limit = max(limit, 5)
    hits: list[dict[str, Any]] = []
    for root in roots:
        root_path = Path(root["root_path"])
        if not root["available"]:
            continue
        if rebuild:
            rebuild_index(root_path)
        for row in search_memory(
            root_path,
            query,
            session_id=session_id,
            event_type=event_type,
            actor=actor,
            task_id=task_id,
            tool=tool,
            git_head=git_head,
            workspace=workspace,
            since=since,
            until=until,
            limit=per_root_limit,
            min_score=min_score,
            retrieval_mode=retrieval_mode,
        ):
            hits.append(_federated_row(root, row))

    hits.sort(
        key=lambda row: (
            -float(row.get("memory_score") or 0),
            row.get("date_utc") or row.get("indexed_at") or "",
            row.get("source_id") or "",
        )
    )
    return hits[:limit]


def suggest_roots(controller_root: str | Path, *, near: str | Path | None = None) -> list[dict[str, Any]]:
    controller = require_root(controller_root)
    registry = _read_registry(controller.root)
    registered_ids = {root["root_id"] for root in registry["roots"]}
    start = Path(near).expanduser().resolve() if near else Path.cwd().resolve()
    candidates: set[Path] = set()
    for candidate in _nearby_root_candidates(start):
        resolved = _candidate_agentdir_root(candidate)
        if resolved:
            candidates.add(resolved)
    suggestions: list[dict[str, Any]] = []
    for root in sorted(candidates, key=lambda path: str(path)):
        root_id = root_id_for_path(root)
        project_path = root.parent if root.name == ".agentdir" else root
        suggestions.append(
            {
                "root_id": root_id,
                "name": default_root_name(root),
                "root_path": str(root),
                "project_path": str(project_path),
                "registered": root_id in registered_ids,
                "available": True,
                "git_remote": _git_remote(project_path),
                "last_indexed_at": _index_mtime(root),
            }
        )
    return suggestions


def doctor_registered_roots(controller_root: str | Path, *, group: str | None = None) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for root in list_registered_roots(controller_root, group=group):
        item = {**root, "ok": True, "errors": [], "warnings": []}
        root_path = Path(root["root_path"])
        if not root["available"]:
            item["ok"] = False
            item["errors"].append("missing root")
            item["index_exists"] = False
            item["stale"] = True
            diagnostics.append(item)
            continue
        paths = paths_for(root_path)
        index_path = paths.index_path
        latest_record_mtime = _latest_record_mtime(paths.root)
        index_mtime = index_path.stat().st_mtime if index_path.is_file() else None
        stale = index_mtime is None or (
            latest_record_mtime is not None and latest_record_mtime > index_mtime
        )
        item.update(
            {
                "index_exists": index_path.is_file(),
                "index_path": str(index_path),
                "index_mtime": index_mtime,
                "latest_record_mtime": latest_record_mtime,
                "stale": stale,
            }
        )
        if not index_path.is_file():
            item["warnings"].append("index missing")
        elif stale:
            item["warnings"].append("index stale")
        diagnostics.append(item)
    return diagnostics


def create_root_group(controller_root: str | Path, name: str, root_ids: list[str]) -> dict[str, Any]:
    controller = require_root(controller_root)
    validate_id(name, "root group")
    registry = _read_registry(controller.root)
    normalized = _validate_registered_root_ids(registry, root_ids)
    if any(group["name"] == name for group in registry["groups"]):
        raise AgentDirError(f"Root group already exists: {name}")
    now = now_iso()
    group = {"name": name, "root_ids": normalized, "created_at": now, "updated_at": now}
    registry["groups"].append(group)
    registry["groups"] = sorted(registry["groups"], key=lambda item: item["name"])
    _write_registry(controller.root, registry)
    return group


def list_root_groups(controller_root: str | Path) -> list[dict[str, Any]]:
    controller = require_root(controller_root)
    registry = _read_registry(controller.root)
    roots_by_id = {root["root_id"]: root for root in registry["roots"]}
    groups: list[dict[str, Any]] = []
    for group in registry["groups"]:
        groups.append(
            {
                **group,
                "roots": [
                    roots_by_id[root_id]
                    for root_id in group["root_ids"]
                    if root_id in roots_by_id
                ],
                "missing_root_ids": [
                    root_id for root_id in group["root_ids"] if root_id not in roots_by_id
                ],
            }
        )
    return groups


def add_root_to_group(controller_root: str | Path, name: str, root_id: str) -> dict[str, Any]:
    controller = require_root(controller_root)
    registry = _read_registry(controller.root)
    _validate_registered_root_ids(registry, [root_id])
    group = _group_entry(registry, name)
    if root_id not in group["root_ids"]:
        group["root_ids"].append(root_id)
        group["root_ids"].sort()
        group["updated_at"] = now_iso()
        _write_registry(controller.root, registry)
    return group


def remove_root_from_group(controller_root: str | Path, name: str, root_id: str) -> dict[str, Any]:
    controller = require_root(controller_root)
    registry = _read_registry(controller.root)
    group = _group_entry(registry, name)
    group["root_ids"] = [candidate for candidate in group["root_ids"] if candidate != root_id]
    group["updated_at"] = now_iso()
    _write_registry(controller.root, registry)
    return group


def format_registered_roots(roots: list[dict[str, Any]]) -> str:
    if not roots:
        return "No registered roots.\n"
    lines: list[str] = []
    for root in roots:
        status = "available" if root["available"] else "missing"
        lines.append(
            f"{root['root_id']} {root['name']} visibility={root['visibility']} "
            f"status={status} {root['root_path']}"
        )
    return "\n".join(lines) + "\n"


def format_root_suggestions(suggestions: list[dict[str, Any]]) -> str:
    if not suggestions:
        return "No AgentDir roots found.\n"
    lines: list[str] = []
    for suggestion in suggestions:
        state = "registered" if suggestion["registered"] else "unregistered"
        remote = f" remote={suggestion['git_remote']}" if suggestion.get("git_remote") else ""
        lines.append(
            f"{suggestion['root_id']} {suggestion['name']} {state} "
            f"{suggestion['root_path']}{remote}"
        )
    return "\n".join(lines) + "\n"


def format_root_diagnostics(rows: list[dict[str, Any]]) -> str:
    rendered = rich_root_diagnostics(rows)
    if rendered is not None:
        return rendered
    if not rows:
        return "No registered roots.\n"
    lines: list[str] = []
    for row in rows:
        status = "ok" if row["ok"] else "error"
        stale = "stale" if row.get("stale") else "fresh"
        details = "; ".join([*row.get("errors", []), *row.get("warnings", [])])
        suffix = f" {details}" if details else ""
        lines.append(f"{row['root_id']} {row['name']} {status} {stale} {row['root_path']}{suffix}")
    return "\n".join(lines) + "\n"


def format_root_groups(groups: list[dict[str, Any]]) -> str:
    if not groups:
        return "No root groups.\n"
    lines: list[str] = []
    for group in groups:
        lines.append(f"{group['name']} roots={len(group['root_ids'])} {','.join(group['root_ids'])}")
    return "\n".join(lines) + "\n"


def format_federated_hits(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        body = (row.get("body_text") or "").strip().replace("\n", "\\n")
        if len(body) > 220:
            body = body[:217] + "..."
        lines.append(
            f"{row.get('memory_score'):.3f} "
            f"root={row.get('source_root_name')} "
            f"{row.get('event_type') or 'unknown'} "
            f"{row.get('subject') or ''} "
            f"session={row.get('session_id') or ''} "
            f"{body} "
            f"{row.get('source_file_path') or ''}"
        )
    return "\n".join(lines)


def resolve_registered_root(root: str | Path) -> Path:
    candidate = Path(root).expanduser().resolve()
    if (candidate / "VERSION").is_file():
        require_root(candidate)
        return candidate
    nested = candidate / ".agentdir"
    if (nested / "VERSION").is_file():
        require_root(nested)
        return nested
    raise AgentDirError(f"Not an AgentDir root or project with .agentdir: {candidate}")


def root_id_for_path(root: Path) -> str:
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    return f"root-{digest[:12]}"


def default_root_name(root: Path) -> str:
    if root.name == ".agentdir" and root.parent.name:
        return root.parent.name
    return root.name


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _registry_path(root: str | Path) -> Path:
    return paths_for(root).state / ROOT_REGISTRY_FILE


def _read_registry(root: str | Path) -> dict[str, Any]:
    path = _registry_path(root)
    if not path.exists():
        return {"version": 1, "roots": [], "groups": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise AgentDirError(f"Unsupported root registry version: {data.get('version')}")
    data.setdefault("roots", [])
    data.setdefault("groups", [])
    return data


def _write_registry(root: str | Path, registry: dict[str, Any]) -> None:
    path = _registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _federated_row(root: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    original_source_id = str(row.get("source_id") or "")
    federated_source_id = f"{root['root_id']}:{original_source_id}"
    body_text = _excerpt(row.get("body_text") or "", 500)
    return {
        **row,
        "body_text": body_text,
        "body_text_truncated": body_text != (row.get("body_text") or ""),
        "source_root_id": root["root_id"],
        "source_root_name": root["name"],
        "source_root_path": root["root_path"],
        "source_root_visibility": root["visibility"],
        "source_id_original": original_source_id,
        "source_file_path": row.get("file_path"),
        "source_id": federated_source_id,
    }


def _excerpt(text: str, limit: int) -> str:
    collapsed = " ".join(text.strip().split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def _root_ids_for_group(registry: dict[str, Any], name: str) -> list[str]:
    return list(_group_entry(registry, name)["root_ids"])


def _group_entry(registry: dict[str, Any], name: str) -> dict[str, Any]:
    for group in registry["groups"]:
        if group["name"] == name:
            return group
    raise AgentDirError(f"Unknown root group: {name}")


def _validate_registered_root_ids(registry: dict[str, Any], root_ids: list[str]) -> list[str]:
    registered = {root["root_id"] for root in registry["roots"]}
    normalized: list[str] = []
    for root_id in root_ids:
        validate_id(root_id, "root id")
        if root_id not in registered:
            raise AgentDirError(f"Unknown registered root: {root_id}")
        if root_id not in normalized:
            normalized.append(root_id)
    return normalized


def _candidate_agentdir_root(candidate: Path) -> Path | None:
    if (candidate / "VERSION").is_file():
        return candidate.resolve()
    nested = candidate / ".agentdir"
    if (nested / "VERSION").is_file():
        return nested.resolve()
    return None


def _nearby_root_candidates(start: Path) -> list[Path]:
    candidates = [start]
    if start.parent != start:
        candidates.append(start.parent)
        try:
            candidates.extend(path for path in start.parent.iterdir() if path.is_dir())
        except OSError:
            pass
    try:
        candidates.extend(path for path in start.iterdir() if path.is_dir())
    except OSError:
        pass
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _git_remote(project_path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=project_path,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _index_mtime(root: Path) -> float | None:
    path = paths_for(root).index_path
    return path.stat().st_mtime if path.is_file() else None


def _latest_record_mtime(root: Path) -> float | None:
    latest: float | None = None
    for container in ("sessions", "actors", "queues"):
        base = root / container
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                mtime = path.stat().st_mtime
                latest = mtime if latest is None else max(latest, mtime)
    return latest
