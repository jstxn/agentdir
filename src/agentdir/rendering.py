from __future__ import annotations

import io
from typing import Any


def rich_status(status: dict[str, Any]) -> str | None:
    rich = _rich()
    if rich is None:
        return None
    Console, Table = rich
    table = Table(title="AgentDir Status", show_header=True, header_style="bold")
    table.add_column("Area")
    table.add_column("Key")
    table.add_column("Value")
    root = status["root"]
    session = status["session"]
    context = status["context"]
    memory = status["memory"]
    federation = status["federation"]
    health = status["health"]
    table.add_row("root", "path", root["path"])
    table.add_row("root", "scope", root["scope"])
    table.add_row("root", "version", str(root.get("version") or ""))
    table.add_row("session", "active", _bool(session["active"]))
    table.add_row("session", "current", session["current"]["session_id"] if session.get("current") else "")
    table.add_row("context", "latest_pack", context["latest_pack"]["pack_id"] if context.get("latest_pack") else "")
    table.add_row("evidence", "count", str(status["evidence"]["count"]))
    table.add_row("memory", "documents", str(memory.get("memory_documents", 0)))
    table.add_row("memory", "passages", str(memory.get("passages", 0)))
    table.add_row("federation", "registered_roots", str(federation["registered_roots"]))
    table.add_row("federation", "stale_roots", str(federation["stale_roots"]))
    table.add_row("health", "doctor_ok", _bool(health["ok"]))
    return _export(table, Console)


def rich_root_diagnostics(rows: list[dict[str, Any]]) -> str | None:
    rich = _rich()
    if rich is None:
        return None
    Console, Table = rich
    table = Table(title="AgentDir Root Diagnostics", show_header=True, header_style="bold")
    for column in ("Root", "Name", "Status", "Freshness", "Path", "Details"):
        table.add_column(column)
    for row in rows:
        details = "; ".join([*row.get("errors", []), *row.get("warnings", [])])
        table.add_row(
            row["root_id"],
            row["name"],
            "ok" if row["ok"] else "error",
            "stale" if row.get("stale") else "fresh",
            row["root_path"],
            details,
        )
    return _export(table, Console)


def rich_doctor(report: dict[str, Any]) -> str | None:
    rich = _rich()
    if rich is None:
        return None
    Console, Table = rich
    table = Table(title="AgentDir Doctor", show_header=True, header_style="bold")
    table.add_column("Level")
    table.add_column("Message")
    table.add_row("ok", _bool(report["ok"]))
    for warning in report.get("warnings", []):
        table.add_row("warning", warning)
    for error in report.get("errors", []):
        table.add_row("error", error)
    return _export(table, Console)


def _rich() -> tuple[Any, Any] | None:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        return None
    return Console, Table


def _export(renderable: Any, Console: Any) -> str:
    # Render into a private buffer. Without an explicit file, rich writes the
    # table to stdout as well as recording it, and callers that print the
    # exported text then emit it a second time.
    console = Console(
        record=True,
        force_terminal=False,
        color_system=None,
        width=120,
        file=io.StringIO(),
    )
    console.print(renderable)
    return console.export_text(clear=True)


def _bool(value: bool) -> str:
    return "true" if value else "false"
