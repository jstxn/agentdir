from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .artifacts import artifact_path
from .envelope import parse_envelope, validate_required
from .mailbox import iter_records
from .redaction import looks_secret_bearing
from .secrets import scan_secret_records
from .store import discover_mailboxes, require_root


@dataclass
class DoctorReport:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


def run_doctor(root: str | Path) -> DoctorReport:
    report = DoctorReport()
    try:
        paths = require_root(root)
    except Exception as exc:
        report.add_error(str(exc))
        return report

    for required in (paths.meta, paths.sessions, paths.actors, paths.artifacts, paths.indexes):
        if not required.exists():
            report.add_error(f"missing required path: {required}")

    seen: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for _kind, mailbox in discover_mailboxes(root):
        for record in iter_records(mailbox):
            try:
                parsed = parse_envelope(record.path)
                missing = validate_required(parsed)
                if missing:
                    report.add_error(f"{record.path}: missing headers {', '.join(missing)}")
                relative_path = str(record.path.relative_to(paths.root))
                if parsed.message_id:
                    seen[parsed.message_id].append((relative_path, parsed.body_sha256))
                for sha in parsed.message.get_all("X-AgentDir-Blob-SHA256", []):
                    if not artifact_path(root, sha).exists():
                        report.add_error(f"{record.path}: missing artifact blob {sha}")
            except Exception as exc:
                report.add_error(f"{record.path}: parse error: {exc}")

    for message_id, file_hashes in seen.items():
        if len(file_hashes) > 1:
            files = [path for path, _hash in file_hashes]
            body_hashes = {_hash for _path, _hash in file_hashes}
            if len(body_hashes) == 1:
                report.add_warning(f"replicated Message-ID {message_id}: {json.dumps(files)}")
            else:
                report.add_error(f"conflicting duplicate Message-ID {message_id}: {json.dumps(files)}")

    for tmp in paths.root.glob("**/Maildir/tmp/*"):
        if tmp.is_file():
            report.add_warning(f"incomplete tmp record ignored: {tmp.relative_to(paths.root)}")
    secret_findings = scan_secret_records(paths.root)
    for finding in secret_findings:
        labels = ",".join(finding.labels)
        report.add_error(
            f"{paths.root / finding.path}: body contains secret-like text "
            f"labels={labels}; run 'agentdir secrets redact --apply'"
        )
    if not secret_findings:
        for finding in _index_secret_findings(paths.index_path):
            report.add_error(
                f"{paths.index_path}: {finding} contains secret-like indexed text; "
                "run 'agentdir index rebuild'"
            )
    return report


def _index_secret_findings(index_path: Path) -> list[str]:
    if not index_path.is_file():
        return []
    findings: list[str] = []
    tables = (
        ("messages", "id", "body_text"),
        ("memory_documents", "id", "body_text"),
        ("memory_passages", "id", "body_text"),
    )
    try:
        with sqlite3.connect(index_path) as conn:
            for table, id_column, body_column in tables:
                if not _table_exists(conn, table):
                    continue
                for row_id, body_text in conn.execute(f"select {id_column}, {body_column} from {table}"):
                    if looks_secret_bearing(body_text or ""):
                        findings.append(f"{table}.{id_column}={row_id}")
                        break
    except sqlite3.DatabaseError as exc:
        findings.append(f"index scan failed: {exc}")
    return findings


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("select 1 from sqlite_master where type = 'table' and name = ?", (table,)).fetchone()
    return row is not None
