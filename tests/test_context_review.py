from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import sys
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from agentdir.context_expansion import (
    HEADER_VIEW_ID,
    HEADER_VIEW_SOURCE_SHA256,
    _expansion_events,
    _receipt_body,
    _resolve_session_summary,
    _validate_receipt,
    _view_id,
    _view_payload,
    expand_context_source,
)


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("could not locate project root")


PROJECT_ROOT = find_project_root()
SRC_ROOT = PROJECT_ROOT / "src"
CLI_INVOCATION = shlex.join((sys.executable, "-m", "agentdir"))


def scoped_work_context_invocation(root: Path) -> str:
    return f"{CLI_INVOCATION} work context --root {shlex.quote(str(root.resolve()))}"


def run_cli(
    *args: str,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    expected_returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [sys.executable, "-m", "agentdir", *args],
        cwd=cwd or PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == expected_returncode, (
        f"expected exit code {expected_returncode}, got {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return result


def init_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, text=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "agentdir@example.test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "AgentDir Test"], cwd=path, check=True)
    return path


def query_rows(root: Path, event_type: str | None = None) -> list[dict[str, object]]:
    db = root / "indexes" / "agentdir.sqlite3"
    sql = "select * from messages"
    params: tuple[object, ...] = ()
    if event_type:
        sql += " where event_type = ?"
        params = (event_type,)
    sql += " order by id"
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def displayed_source_ref(payload: dict[str, object], *, event_type: str = "agent.message") -> str:
    manifest = payload["context_pack"]
    briefing = payload["context_briefing"]
    assert isinstance(manifest, dict)
    assert isinstance(briefing, dict)
    source_by_id = {
        source["source_id"]: source
        for source in manifest["sources"]
        if isinstance(source, dict)
    }
    for index, source_id in enumerate(briefing["source_ids"], start=1):
        if source_by_id[source_id].get("event_type") == event_type:
            return str(index)
    raise AssertionError(f"no displayed {event_type} source in context pack")


def test_work_context_show_reopens_the_persisted_numbered_briefing(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect persisted briefing marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "checkout redirect persisted briefing marker", "--json", cwd=repo).stdout
    )

    shown = run_cli(
        "work",
        "context",
        "--show",
        "--pack",
        started["context_pack"]["pack_id"],
        cwd=repo,
    )

    assert "[1]" in shown.stdout
    assert "persisted briefing marker" in shown.stdout
    pack_id = started["context_pack"]["pack_id"]
    assert (
        f"context_use={scoped_work_context_invocation(repo / '.agentdir')} "
        f"--pack {pack_id} --use <number>"
        in shown.stdout
    )


def test_reopened_briefing_commands_remain_bound_to_the_displayed_pack(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect recovered pack target marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    first = json.loads(
        run_cli(
            "work",
            "start",
            "checkout redirect recovered pack target marker",
            "--json",
            cwd=repo,
        ).stdout
    )
    first_pack_id = first["context_pack"]["pack_id"]
    second = json.loads(
        run_cli(
            "context",
            "build",
            "checkout redirect newer pack target marker",
            "--emit",
            "--json",
            cwd=repo,
        ).stdout
    )
    second_pack_id = second["manifest"]["pack_id"]

    shown = run_cli("work", "context", "--show", "--pack", first_pack_id, cwd=repo)
    assert (
        f"context_use={scoped_work_context_invocation(repo / '.agentdir')} "
        f"--pack {first_pack_id} --use <number>"
        in shown.stdout
    )
    run_cli(
        "work",
        "context",
        "--pack",
        first_pack_id,
        "--use",
        "1",
        "--reason",
        "the recovered source constrains the active repair",
        cwd=repo,
    )

    first_audit = json.loads(
        run_cli("audit", "context", "--pack", first_pack_id, "--json", cwd=repo).stdout
    )
    second_audit = json.loads(
        run_cli("audit", "context", "--pack", second_pack_id, "--json", cwd=repo).stdout
    )
    assert first_audit["review_status"] == "complete"
    assert first_audit["used_count"] == 1
    assert second_audit["review_status"] == "pending"


def test_finish_cannot_hide_an_older_pending_pack_behind_a_newer_pack(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect hidden pending marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    first = json.loads(
        run_cli("work", "start", "checkout redirect hidden pending marker", "--json", cwd=repo).stdout
    )
    second = json.loads(
        run_cli(
            "context",
            "build",
            "checkout redirect replacement pack",
            "--emit",
            "--json",
            cwd=repo,
        ).stdout
    )
    run_cli(
        "work",
        "context",
        "--pack",
        second["manifest"]["pack_id"],
        "--none-relevant",
        "--reason",
        "the replacement briefing does not constrain this task",
        cwd=repo,
    )

    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
    strict_audit = run_cli("audit", "session", "--strict", "--json", cwd=repo, expected_returncode=1)
    blocked = run_cli("work", "finish", "--json", cwd=repo, expected_returncode=3)

    assert first["context_pack"]["pack_id"] in status["context"]["blocking_packs"]
    assert first["context_pack"]["pack_id"] in strict_audit.stdout
    assert first["context_pack"]["pack_id"] in blocked.stderr


def test_no_context_records_a_visible_marker_for_the_latest_task(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect stale attribution marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    run_cli("work", "start", "first attributed task", cwd=repo)
    run_cli(
        "work",
        "context",
        "--none-relevant",
        "--reason",
        "the prior record does not constrain the next task",
        cwd=repo,
    )

    second = json.loads(
        run_cli("work", "start", "second explicit opt-out task", "--no-context", "--json", cwd=repo).stdout
    )
    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
    finished = json.loads(run_cli("work", "finish", "--json", cwd=repo).stdout)

    assert second["context_pack"] is not None
    assert second["context_briefing"]["match_state"] == "context_disabled"
    assert second["context_pack"]["selection_policy"]["context_enabled"] is False
    assert status["session"]["current"]["title"] == "second explicit opt-out task"
    assert status["context"]["audit"]["task"] == "second explicit opt-out task"
    assert status["context"]["audit"]["retrieved_count"] == 0
    assert finished["report"]["task"] == "second explicit opt-out task"


def test_low_level_consume_of_all_presented_sources_completes_compatibility_review(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect compatibility consume marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "checkout redirect compatibility consume marker", "--json", cwd=repo).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_ids = started["context_briefing"]["source_ids"]
    source_args = [part for source_id in source_ids for part in ("--source", source_id)]

    run_cli(
        "context",
        "consume",
        "--pack",
        pack_id,
        *source_args,
        "--purpose",
        "plan",
        cwd=repo,
    )
    audit = json.loads(run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout)

    assert audit["review_status"] == "complete"
    assert audit["decision"] == "legacy_used"
    assert audit["reviewed_count"] == audit["presented_count"]
    run_cli("work", "finish", "--json", cwd=repo)


def test_partial_low_level_consume_can_finish_with_a_visible_compatibility_warning(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    for index in range(2):
        prior = tmp_path / f"prior-{index}.txt"
        prior.write_text(
            f"checkout redirect partial compatibility marker {index}",
            encoding="utf-8",
        )
        run_cli("session", "start", "--id", f"prior-context-{index}", cwd=repo)
        run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
        run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "checkout redirect partial compatibility marker", "--json", cwd=repo).stdout
    )
    assert started["context_briefing"]["presented_count"] >= 2
    pack_id = started["context_pack"]["pack_id"]
    source_id = started["context_briefing"]["source_ids"][0]

    run_cli(
        "context",
        "consume",
        "--pack",
        pack_id,
        "--source",
        source_id,
        "--purpose",
        "plan",
        cwd=repo,
    )
    audit = json.loads(run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout)
    finished = json.loads(run_cli("work", "finish", "--json", cwd=repo).stdout)

    assert audit["review_status"] == "compatibility_partial"
    assert audit["decision"] == "legacy_partial"
    assert audit["finish_allowed"] is True
    assert audit["lineage_valid"] is False
    assert finished["report"]["agent_handoff"]["status"] == "needs_attention"


def test_terminal_review_rejects_later_low_level_consumption(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect terminal review marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "checkout redirect terminal review marker", "--json", cwd=repo).stdout
    )
    pack_id = started["context_pack"]["pack_id"]
    source_id = started["context_briefing"]["source_ids"][0]
    run_cli(
        "work",
        "context",
        "--none-relevant",
        "--reason",
        "the prior marker does not constrain this task",
        cwd=repo,
    )

    blocked = run_cli(
        "context",
        "consume",
        "--pack",
        pack_id,
        "--source",
        source_id,
        "--purpose",
        "plan",
        cwd=repo,
        expected_returncode=3,
    )
    audit = json.loads(run_cli("audit", "context", "--pack", pack_id, "--json", cwd=repo).stdout)

    assert "already terminal" in blocked.stderr
    assert audit["decision"] == "no_relevant"
    assert audit["used_count"] == 0
    assert audit["transition_conflict"] is False


def test_concurrent_identical_context_decisions_are_idempotent(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect concurrent decision marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    run_cli("work", "start", "checkout redirect concurrent decision marker", cwd=repo)

    def decide() -> dict[str, object]:
        result = run_cli(
            "work",
            "context",
            "--use",
            "1",
            "--reason",
            "the prior redirect marker constrains the plan",
            "--json",
            cwd=repo,
        )
        return json.loads(result.stdout)

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(lambda _: decide(), range(2)))

    assert sorted(decision["recorded"] for decision in decisions) == [False, True]
    assert len({decision["decision_id"] for decision in decisions}) == 1


def test_reordered_context_selectors_resolve_to_the_same_terminal_decision(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect selector order marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "checkout redirect selector order marker", "--json", cwd=repo).stdout
    )
    assert started["context_briefing"]["presented_count"] >= 2

    first = json.loads(
        run_cli(
            "work",
            "context",
            "--use",
            "2",
            "--use",
            "1",
            "--reason",
            "both prior records constrain the plan",
            "--json",
            cwd=repo,
        ).stdout
    )
    repeated = json.loads(
        run_cli(
            "work",
            "context",
            "--use",
            "1",
            "--use",
            "2",
            "--reason",
            "both prior records constrain the plan",
            "--json",
            cwd=repo,
        ).stdout
    )

    assert first["recorded"] is True
    assert repeated["recorded"] is False
    assert repeated["decision_id"] == first["decision_id"]


def test_work_context_rejects_a_retrieved_source_omitted_from_the_briefing(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    for index in range(6):
        body = tmp_path / f"prior-{index}.txt"
        body.write_text(
            f"checkout redirect omitted source marker variant {index}",
            encoding="utf-8",
        )
        run_cli("session", "start", "--id", f"prior-{index}", cwd=repo)
        run_cli("emit", "--type", "agent.message", "--body", str(body), cwd=repo)
        run_cli("session", "end", "--summary", str(body), cwd=repo)
    started = json.loads(
        run_cli(
            "work",
            "start",
            "checkout redirect omitted source marker",
            "--memory-limit",
            "8",
            "--json",
            cwd=repo,
        ).stdout
    )
    presented = set(started["context_briefing"]["source_ids"])
    omitted = next(
        source["source_id"]
        for source in started["context_pack"]["sources"]
        if source["source_id"] not in presented
    )

    blocked = run_cli(
        "work",
        "context",
        "--use",
        omitted,
        "--reason",
        "attempt to bypass the bounded briefing",
        cwd=repo,
        expected_returncode=2,
    )

    assert "not presented" in blocked.stderr

    all_source_ids = [source["source_id"] for source in started["context_pack"]["sources"]]
    source_args = [part for source_id in all_source_ids for part in ("--source", source_id)]
    run_cli(
        "context",
        "consume",
        "--pack",
        started["context_pack"]["pack_id"],
        *source_args,
        "--purpose",
        "plan",
        cwd=repo,
    )
    audit = json.loads(
        run_cli(
            "audit",
            "context",
            "--pack",
            started["context_pack"]["pack_id"],
            "--json",
            cwd=repo,
        ).stdout
    )

    assert audit["used_count"] == audit["presented_count"]
    assert audit["consumed_count"] == audit["retrieved_count"]
    assert audit["additional_consumed_count"] == audit["retrieved_count"] - audit["presented_count"]
    assert audit["lineage_valid"] is True


def test_context_review_can_target_an_older_session_without_ending_the_current_one(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect older session marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    started = json.loads(
        run_cli("work", "start", "checkout redirect older session marker", "--json", cwd=repo).stdout
    )
    older_session = started["session"]["session_id"]
    run_cli("session", "start", "--id", "newer-active", cwd=repo)

    run_cli(
        "work",
        "context",
        "--session",
        older_session,
        "--none-relevant",
        "--reason",
        "the older marker does not constrain the current implementation",
        cwd=repo,
    )
    blocked = run_cli(
        "work",
        "finish",
        "--session",
        older_session,
        "--json",
        cwd=repo,
        expected_returncode=3,
    )
    current = json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)
    finished = json.loads(
        run_cli(
            "work",
            "finish",
            "--session",
            older_session,
            "--keep-session",
            "--json",
            cwd=repo,
        ).stdout
    )

    assert "--keep-session" in blocked.stderr
    assert current["session_id"] == "newer-active"
    assert finished["ended_session"] is None


def test_status_does_not_attribute_an_older_pack_to_a_new_active_session(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    started = json.loads(run_cli("work", "start", "first context pack", "--json", cwd=repo).stdout)
    assert started["context_pack"] is not None
    run_cli("work", "finish", "--json", cwd=repo)
    run_cli("session", "start", "--id", "new-session-without-pack", cwd=repo)

    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)

    assert status["session"]["current"]["session_id"] == "new-session-without-pack"
    assert status["context"]["latest_pack"] is None
    assert status["context"]["audit"] is None


def test_work_finish_blocks_pending_context_but_allows_visible_skip(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect review gate marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    run_cli("work", "start", "checkout redirect review gate marker", "--emit-context", cwd=repo)

    blocked = run_cli("work", "finish", "--json", cwd=repo, expected_returncode=3)
    assert "Context review is pending" in blocked.stderr
    assert json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)["status"] == "active"

    run_cli(
        "work",
        "context",
        "--skip",
        "--reason",
        "the context artifact could not be reviewed",
        cwd=repo,
    )
    finished = json.loads(run_cli("work", "finish", "--json", cwd=repo).stdout)
    assert finished["report"]["agent_handoff"]["status"] == "needs_attention"
    assert finished["report"]["agent_handoff"]["context_lineage"]["review_status"] == "skipped"


def test_later_clean_pack_does_not_hide_an_earlier_context_warning(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    prior = tmp_path / "prior.txt"
    prior.write_text("checkout redirect durable skip marker", encoding="utf-8")
    run_cli("session", "start", "--id", "prior-context", cwd=repo)
    run_cli("emit", "--type", "agent.message", "--body", str(prior), cwd=repo)
    run_cli("session", "end", "--summary", str(prior), cwd=repo)
    first = json.loads(
        run_cli("work", "start", "checkout redirect durable skip marker", "--json", cwd=repo).stdout
    )
    first_pack_id = first["context_pack"]["pack_id"]
    run_cli(
        "work",
        "context",
        "--pack",
        first_pack_id,
        "--skip",
        "--reason",
        "the briefing could not be reviewed in this environment",
        cwd=repo,
    )
    run_cli("work", "start", "later explicit context opt out", "--no-context", cwd=repo)

    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
    strict = run_cli("audit", "session", "--strict", "--json", cwd=repo, expected_returncode=1)
    report = json.loads(
        run_cli("report", "final", "--format", "json", "--no-doctor", cwd=repo).stdout
    )
    finished = json.loads(run_cli("work", "finish", "--no-doctor", "--json", cwd=repo).stdout)

    assert first_pack_id in status["context"]["attention_packs"]
    assert first_pack_id in strict.stdout
    assert report["agent_handoff"]["status"] == "needs_attention"
    assert report["agent_handoff"]["context_lineage"]["pack_id"] == first_pack_id
    assert report["agent_handoff"]["context_lineage"]["review_status"] == "skipped"
    assert finished["report"]["agent_handoff"]["status"] == "needs_attention"


def test_tampered_context_manifest_is_visible_and_blocks_finish(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    run_cli("work", "start", "corrupt context manifest", cwd=repo)
    blobs = list((repo / ".agentdir" / "artifacts" / "blobs" / "sha256").glob("*/*/*"))
    assert len(blobs) == 1
    manifest = json.loads(blobs[0].read_text(encoding="utf-8"))
    manifest["briefing"]["review_required"] = False
    blobs[0].write_text(json.dumps(manifest), encoding="utf-8")

    status = run_cli("status", cwd=repo, expected_returncode=1)
    report = json.loads(
        run_cli("report", "final", "--format", "json", "--no-doctor", cwd=repo).stdout
    )
    blocked = run_cli("work", "finish", "--no-doctor", "--json", cwd=repo, expected_returncode=3)

    assert "review_status" in status.stdout
    assert "audit_error" in status.stdout
    assert "error" in status.stdout
    assert report["agent_handoff"]["status"] == "needs_attention"
    assert report["agent_handoff"]["context_lineage"]["review_status"] == "error"
    assert "digest does not match" in report["agent_handoff"]["context_lineage"]["error"]
    assert "cannot be certified" in blocked.stderr


def test_malformed_context_manifests_use_the_typed_audit_error_path(tmp_path: Path) -> None:
    from agentdir.events import emit_event

    variants: tuple[tuple[str, str, str], ...] = (
        ("invalid-json", "{not json", "invalid JSON"),
        ("non-object", "[]", "must be a JSON object"),
        (
            "missing-sources",
            json.dumps(
                {
                    "protocol": "agentdir.context-pack.v1",
                    "pack_id": "ctx-missing-sources",
                    "task": "missing sources",
                    "session_id": "malformed-missing-sources",
                }
            ),
            "sources must be a list",
        ),
        (
            "disabled-review",
            json.dumps(
                {
                    "protocol": "agentdir.context-pack.v1",
                    "pack_id": "ctx-disabled-review",
                    "task": "disabled review",
                    "session_id": "malformed-disabled-review",
                    "sources": [{"source_id": "src-presented"}],
                    "briefing": {
                        "protocol": "agentdir.context-briefing.v1",
                        "source_ids": ["src-presented"],
                        "presented_count": 1,
                        "omitted_count": 0,
                        "review_required": False,
                    },
                }
            ),
            "review requirement is inconsistent",
        ),
        (
            "session-mismatch",
            json.dumps(
                {
                    "protocol": "agentdir.context-pack.v1",
                    "pack_id": "ctx-session-mismatch",
                    "task": "session mismatch",
                    "session_id": "claimed-session",
                    "sources": [],
                    "briefing": {
                        "protocol": "agentdir.context-briefing.v1",
                        "source_ids": [],
                        "presented_count": 0,
                        "omitted_count": 0,
                        "review_required": False,
                    },
                }
            ),
            "session does not match",
        ),
    )
    for name, content, expected_error in variants:
        repo = init_repo(tmp_path / name)
        session_id = f"malformed-{name}"
        pack_id = "ctx-missing-sources" if name == "missing-sources" else f"ctx-{name}"
        run_cli("session", "start", "--id", session_id, cwd=repo)
        artifact = tmp_path / f"{name}.json"
        artifact.write_text(content, encoding="utf-8")
        emit_event(
            repo / ".agentdir",
            session_id=session_id,
            event_type="context.pack.created",
            subject=f"malformed manifest {name}",
            body=f"pack_id={pack_id}\nprotocol=agentdir.context-pack.v1",
            artifact=artifact,
            extra_headers={
                "X-AgentDir-Protocol": "agentdir.context-pack.v1",
                "X-AgentDir-Pack-Id": pack_id,
            },
        )

        status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
        strict = run_cli(
            "audit",
            "session",
            "--strict",
            "--json",
            cwd=repo,
            expected_returncode=1,
        )
        report = json.loads(
            run_cli("report", "final", "--format", "json", "--no-doctor", cwd=repo).stdout
        )
        blocked = run_cli(
            "work",
            "finish",
            "--no-doctor",
            "--json",
            cwd=repo,
            expected_returncode=3,
        )

        assert expected_error in status["context"]["audit"]["error"]
        assert pack_id in status["context"]["blocking_packs"]
        assert expected_error in strict.stdout
        assert report["agent_handoff"]["status"] == "needs_attention"
        assert report["agent_handoff"]["context_lineage"]["review_status"] == "error"
        assert "cannot be certified" in blocked.stderr
        assert json.loads(run_cli("session", "current", "--json", cwd=repo).stdout)[
            "session_id"
        ] == session_id


def test_malformed_context_creation_identities_remain_visible_blockers(tmp_path: Path) -> None:
    from agentdir.events import emit_event

    variants = (
        ("header-only", ["ctx-header-only"], "no pack id in this body", "body field, found 0"),
        ("body-only", [], "pack_id=ctx-body-only", "header, found 0"),
        (
            "mismatch",
            ["ctx-mismatch-header"],
            "pack_id=ctx-mismatch-body",
            "does not match body pack id",
        ),
        (
            "duplicate-header",
            ["ctx-duplicate-header", "ctx-duplicate-header"],
            "pack_id=ctx-duplicate-header",
            "header, found 2",
        ),
        (
            "duplicate-body",
            ["ctx-duplicate-body"],
            "pack_id=ctx-duplicate-body\npack_id=ctx-duplicate-body",
            "body field, found 2",
        ),
    )
    for name, header_ids, body, expected_error in variants:
        repo = init_repo(tmp_path / name)
        session_id = f"identity-{name}"
        run_cli("session", "start", "--id", session_id, cwd=repo)
        extra_headers: dict[str, str | list[str]] = {
            "X-AgentDir-Protocol": "agentdir.context-pack.v1"
        }
        if header_ids:
            extra_headers["X-AgentDir-Pack-Id"] = header_ids
        emit_event(
            repo / ".agentdir",
            session_id=session_id,
            event_type="context.pack.created",
            subject=f"malformed identity {name}",
            body=body,
            extra_headers=extra_headers,
        )
        pack_id = header_ids[0] if header_ids else f"ctx-{name}"

        status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
        strict = run_cli(
            "audit",
            "session",
            "--strict",
            "--json",
            cwd=repo,
            expected_returncode=1,
        )
        report = json.loads(
            run_cli("report", "final", "--format", "json", "--no-doctor", cwd=repo).stdout
        )
        blocked = run_cli(
            "work",
            "finish",
            "--no-doctor",
            "--json",
            cwd=repo,
            expected_returncode=3,
        )

        assert pack_id in status["context"]["blocking_packs"]
        assert expected_error in status["context"]["audit"]["error"]
        assert expected_error in strict.stdout
        assert report["agent_handoff"]["status"] == "needs_attention"
        assert "cannot be certified" in blocked.stderr
        direct = run_cli(
            "audit",
            "context",
            "--pack",
            pack_id,
            "--json",
            cwd=repo,
            expected_returncode=2,
        )
        assert expected_error in direct.stdout + direct.stderr

    repo = init_repo(tmp_path / "duplicate-creation")
    run_cli("session", "start", "--id", "identity-duplicate-creation", cwd=repo)
    for index in range(2):
        headers: dict[str, str] = {
            "X-AgentDir-Protocol": "agentdir.context-pack.v1",
        }
        if index == 0:
            headers["X-AgentDir-Pack-Id"] = "ctx-duplicate-creation"
        emit_event(
            repo / ".agentdir",
            session_id="identity-duplicate-creation",
            event_type="context.pack.created",
            subject=f"duplicate creation {index}",
            body="pack_id=ctx-duplicate-creation",
            extra_headers=headers,
        )
    status = json.loads(run_cli("status", "--json", cwd=repo).stdout)
    blocked = run_cli(
        "work",
        "finish",
        "--no-doctor",
        "--json",
        cwd=repo,
        expected_returncode=3,
    )
    assert "ctx-duplicate-creation" in status["context"]["blocking_packs"]
    assert "multiple creation events" in status["context"]["audit"]["error"]
    assert "cannot be certified" in blocked.stderr
    direct = run_cli(
        "audit",
        "context",
        "--pack",
        "ctx-duplicate-creation",
        "--json",
        cwd=repo,
        expected_returncode=2,
    )
    assert "multiple creation events" in direct.stdout + direct.stderr
