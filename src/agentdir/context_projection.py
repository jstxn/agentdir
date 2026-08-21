from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .context import audit_context_pack
from .context_expansion import audit_context_expansion_inventory
from .context_repository import list_context_packs
from .store import AgentDirError


def build_context_projection(
    root: str | Path,
    session_id: str | None,
    *,
    fallback_any: bool = False,
    rebuild: bool = True,
) -> dict[str, Any]:
    """Build the canonical pack, audit, attention, and receipt view for a session."""
    packs = list_context_packs(
        root,
        session_id,
        fallback_any=fallback_any,
        rebuild=rebuild,
    )
    latest_pack = packs[-1] if packs else None
    audited_packs = (
        packs
        if session_id
        else [
            pack
            for pack in packs
            if latest_pack and pack.get("session_id") == latest_pack.get("session_id")
        ]
    )
    pack_audits = _audit_context_packs(root, audited_packs)
    latest_audit = pack_audits[-1] if pack_audits else None
    blocking_audit = next(
        (audit for audit in pack_audits if not audit.get("finish_allowed", False)),
        None,
    )
    attention_audit = next(
        (audit for audit in pack_audits if context_needs_attention(audit)),
        None,
    )
    inventory_session_id = session_id or str((latest_pack or {}).get("session_id") or "")
    expansion_inventory = (
        _safe_context_expansion_inventory(root, inventory_session_id)
        if inventory_session_id
        else _empty_expansion_inventory()
    )
    return {
        "packs": packs,
        "latest_pack": latest_pack,
        "pack_audits": pack_audits,
        "audit": latest_audit,
        "blocking_audit": blocking_audit,
        "attention_audit": attention_audit,
        "blocking_packs": [
            audit["pack_id"]
            for audit in pack_audits
            if not audit.get("finish_allowed", False)
        ],
        "attention_packs": [
            audit["pack_id"]
            for audit in pack_audits
            if context_needs_attention(audit)
        ],
        "pack_count": len(packs),
        "expansion_receipt_inventory": expansion_inventory,
    }


def context_needs_attention(context_audit: dict[str, Any]) -> bool:
    expansion = context_audit.get("expansion") or {}
    return bool(
        context_audit.get("error")
        or not context_audit.get("lineage_valid", False)
        or not expansion.get("receipts_valid", True)
    )


def _audit_context_packs(
    root: str | Path,
    packs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    audits: list[dict[str, Any]] = []
    for pack in packs:
        if pack.get("identity_error"):
            audits.append(_error_audit(pack["pack_id"], str(pack["identity_error"])))
            continue
        try:
            audits.append(audit_context_pack(root, pack["pack_id"], rebuild=False))
        except AgentDirError as exc:
            audits.append(_error_audit(pack["pack_id"], str(exc)))
    return audits


def _error_audit(pack_id: str, error: str) -> dict[str, Any]:
    return {
        "error": error,
        "pack_id": pack_id,
        "review_status": "error",
        "decision_complete": False,
        "finish_allowed": False,
        "lineage_valid": False,
    }


def _safe_context_expansion_inventory(
    root: str | Path,
    session_id: str,
) -> dict[str, Any]:
    try:
        return audit_context_expansion_inventory(root, session_id)
    except (AgentDirError, sqlite3.Error, OSError) as exc:
        return {
            **_empty_expansion_inventory(),
            "receipts_valid": False,
            "validation_errors": [str(exc)],
        }


def _empty_expansion_inventory() -> dict[str, Any]:
    return {
        "event_count": 0,
        "claimable_event_count": 0,
        "orphan_event_count": 0,
        "receipts_valid": True,
        "validation_errors": [],
    }
