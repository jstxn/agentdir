from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .artifacts import artifact_path
from .envelope import parse_envelope, validate_required
from .mailbox import iter_records
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
                if _looks_secret_bearing(parsed.body_text):
                    report.add_warning(f"{record.path}: body contains secret-like text")
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
    return report


def _looks_secret_bearing(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)
SECRET_PATTERNS = [
    re.compile(r"gh[opsu]_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

