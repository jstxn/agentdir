from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .artifacts import artifact_path
from .envelope import ParsedEnvelope, header_value, parse_envelope, validate_required
from .git import git_common_root
from .hooks import hooks_manifest_issues
from .mailbox import iter_records
from .redaction import looks_secret_bearing
from .secrets import scan_secret_records
from .store import CONFIG_DIR, AgentDirError, discover_mailboxes, require_root


_CONTEXT_EXPANSION_EVENT = "context.sources.expanded"
_CONTEXT_EXPANSION_IDENTITY_HEADERS = (
    "X-AgentDir-Context-View-Pack-Id",
    "X-AgentDir-Context-View-Id",
    "X-AgentDir-Context-View-Source-Id",
)


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
    verified_artifacts: set[str] = set()
    parsed_by_path: dict[Path, ParsedEnvelope] = {}
    blobs_root = (paths.artifacts / "blobs" / "sha256").resolve()
    mailboxes = discover_mailboxes(paths.root)
    for _kind, mailbox in mailboxes:
        for record in iter_records(mailbox):
            try:
                parsed = parse_envelope(record.path)
                parsed_by_path[record.path] = parsed
                missing = validate_required(parsed)
                if missing:
                    message = f"{record.path}: missing headers {', '.join(missing)}"
                    if _is_context_expansion_receipt(parsed):
                        # Expansion receipts are optional telemetry. Their own
                        # audit reports malformed envelopes and can put a
                        # handoff into needs_attention, but a broken receipt
                        # must not become a denial-of-finish vector.
                        report.add_warning(f"optional context expansion receipt: {message}")
                    else:
                        report.add_error(message)
                relative_path = str(record.path.relative_to(paths.root))
                if parsed.message_id:
                    seen[parsed.message_id].append((relative_path, parsed.body_sha256))
                for sha in parsed.headers("X-AgentDir-Blob-SHA256"):
                    try:
                        blob = artifact_path(paths.root, sha)
                    except AgentDirError as exc:
                        report.add_error(f"{record.path}: invalid artifact blob {sha}: {exc}")
                        continue
                    if blob.is_symlink():
                        report.add_error(f"{record.path}: artifact blob is a symlink {sha}")
                        continue
                    if not blob.exists():
                        report.add_error(f"{record.path}: missing artifact blob {sha}")
                        continue
                    if not blob.is_file():
                        report.add_error(
                            f"{record.path}: artifact blob is not a regular file {sha}"
                        )
                        continue
                    try:
                        resolved_blob = blob.resolve()
                        resolved_blob.relative_to(blobs_root)
                    except (OSError, ValueError):
                        report.add_error(
                            f"{record.path}: artifact blob escapes artifact store {sha}"
                        )
                        continue
                    if sha not in verified_artifacts:
                        try:
                            actual_sha = _sha256_file(resolved_blob)
                        except OSError as exc:
                            report.add_error(f"{record.path}: unreadable artifact blob {sha}: {exc}")
                        else:
                            if actual_sha != sha:
                                report.add_error(
                                    f"{record.path}: artifact blob digest mismatch {sha}"
                                )
                            verified_artifacts.add(sha)
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

    tmp_candidates: list[Path] = []
    for _kind, mailbox in mailboxes:
        tmp_dir = mailbox / "tmp"
        if tmp_dir.is_dir():
            tmp_candidates.extend(sorted(tmp_dir.iterdir()))
    if paths.archives.is_dir():
        tmp_candidates.extend(sorted(paths.archives.glob("sessions/*/Maildir/tmp/*")))
    for tmp in tmp_candidates:
        if tmp.is_file():
            report.add_warning(f"incomplete tmp record ignored: {tmp.relative_to(paths.root)}")
    try:
        for issue in hooks_manifest_issues(paths.root):
            report.add_warning(issue)
    except Exception:
        # Hook drift detection is best-effort; it must never break doctor.
        pass
    split = _split_worktree_store(paths.root)
    if split is not None:
        report.add_warning(split)
    try:
        secret_findings = scan_secret_records(paths.root, parsed_by_path)
    except TypeError:
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
    unfolded = _index_unfolded_headers(paths.index_path)
    if unfolded:
        report.add_warning(
            f"{paths.index_path}: {unfolded} indexed header value(s) keep the whitespace of a "
            "folded header, which was indexed by AgentDir before 0.7.9 on Python 3.11. "
            "Artifact lookups against those rows will not match; run 'agentdir index rebuild'"
        )
    return report


def _is_context_expansion_receipt(parsed: ParsedEnvelope) -> bool:
    return parsed.header("X-AgentDir-Event-Type") == _CONTEXT_EXPANSION_EVENT or any(
        parsed.headers(name) for name in _CONTEXT_EXPANSION_IDENTITY_HEADERS
    )


def _split_worktree_store(root: Path) -> str | None:
    """Warn when this linked worktree keeps a store the main working tree also has.

    New worktrees share the main tree's store, but one created before that
    behaviour existed keeps its own. Nothing is lost, yet memory and evidence
    stay split across the two, so say which command joins them.
    """
    try:
        worktree = root.parent
        main = git_common_root(worktree)
        if main is None or main == worktree:
            return None
        main_store = main / CONFIG_DIR
        if not main_store.is_dir() or main_store.resolve() == root:
            return None
        return (
            f"worktree store {root} is separate from the main working tree store {main_store}; "
            f"memory and evidence stay split. Run 'agentdir roots register {main_store}' to search both, "
            f"or remove this store to share the main one."
        )
    except OSError:
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _index_unfolded_headers(index_path: Path) -> int:
    """Count indexed header values that still carry fold whitespace.

    Upgrading does not rewrite rows that were already indexed, so a store built
    by an affected 3.11 install keeps padded values and silently fails artifact
    lookups against them until the index is rebuilt.
    """
    if not index_path.is_file():
        return 0
    try:
        with contextlib.closing(sqlite3.connect(index_path)) as conn:
            if not _table_exists(conn, "headers"):
                return 0
            return sum(
                1
                for (value,) in conn.execute("select value from headers")
                if value is not None and value != header_value(value)
            )
    except sqlite3.DatabaseError:
        return 0


def _index_secret_findings(index_path: Path) -> list[str]:
    if not index_path.is_file():
        return []
    findings: list[str] = []
    tables = (
        ("messages", "id", ("body_text", "subject")),
        ("headers", "message_rowid", ("value",)),
        ("memory_documents", "id", ("body_text",)),
    )
    try:
        with contextlib.closing(sqlite3.connect(index_path)) as conn:
            for table, id_column, columns in tables:
                if not _table_exists(conn, table):
                    continue
                existing_columns = _table_columns(conn, table)
                scan_columns = [col for col in columns if col in existing_columns]
                if id_column not in existing_columns or not scan_columns:
                    continue
                column_sql = ", ".join(scan_columns)
                for row in conn.execute(f"select {id_column}, {column_sql} from {table}"):
                    row_id = row[0]
                    if any(looks_secret_bearing(value or "") for value in row[1:]):
                        findings.append(f"{table}.{id_column}={row_id}")
                        break
    except sqlite3.DatabaseError as exc:
        findings.append(f"index scan failed: {exc}")
    return findings


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("select 1 from sqlite_master where type = 'table' and name = ?", (table,)).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"pragma table_info({table})")}
