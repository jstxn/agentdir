# AgentDir V1 Demo

This demo is the narrow proof for AgentDir V1:

1. initialize a local-first root
2. emit a small coding-session trace as immutable envelopes
3. build the SQLite sidecar index
4. delete the index and rebuild it from raw envelopes
5. replay the recovered session
6. run `doctor` against the resulting store

The goal is not a polished user experience. The goal is to prove the product thesis from the PRD and technical brief: raw envelopes are the canonical evidence, and the index is useful but disposable.

## Scope

This demo stays inside the existing V1 command contract:

```text
agentdir init <root>
agentdir emit --root <root> --session <id> --type <type> --body <file>
agentdir index rebuild --root <root>
agentdir query --root <root> --session <id>
agentdir replay --root <root> --session <id>
agentdir doctor --root <root>
```

It intentionally exercises the event types called out in the planning docs:

- `session.started`
- `user.message`
- `tool.call`
- `tool.result`
- `file.diff`
- `agent.message`
- `session.ended`

The happy-path script does not require actor handoff or artifact storage. Those should be validated separately once the later V1 slices land.

## Prerequisites

- A complete V1 checkout that exposes the AgentDir CLI, either as `agentdir` or `python -m agentdir`
- A local shell with `bash`
- No secrets in the demo bodies or tool outputs

## Run The Demo

From the repository root:

```bash
bash examples/dogfood-session.sh
```

Optional controls:

```bash
KEEP_WORKDIR=1 bash examples/dogfood-session.sh
KEEP_WORKDIR=1 SESSION_ID=dogfood-smoke bash examples/dogfood-session.sh /tmp/agentdir-v1-demo
```

If `KEEP_WORKDIR=1` is set, the script prints the retained root so you can inspect the envelopes, rebuilt index, and replay output afterward.

## What The Script Proves

Expected flow:

1. `init` creates the `.agentdir`, `sessions`, `actors`, `artifacts`, and `indexes` layout.
2. A toy coding-session trace is written into the session mailbox through immutable envelopes.
3. `index rebuild` creates `indexes/agentdir.sqlite3`.
4. `query --session <id>` shows the emitted records.
5. Deleting `indexes/agentdir.sqlite3` does not lose the session.
6. A second `index rebuild` reconstructs the sidecar from raw envelopes.
7. `replay --session <id>` renders the recovered timeline.
8. `doctor` reports a healthy root on the happy path.

The script is intentionally small, but it should leave behind enough evidence for manual inspection with normal shell tools such as `find`, `less`, and `sqlite3`.

## Manual Verification Checklist

After a retained run, confirm:

- `sessions/<session-id>/Maildir/new/` contains the emitted envelopes.
- `tmp/` is empty unless you deliberately interrupted a writer.
- `indexes/agentdir.sqlite3` exists after rebuild.
- `replay` still works after deleting and rebuilding the index.
- `doctor` exits successfully on the clean demo root.

Helpful spot checks:

```bash
find /tmp/agentdir-v1-demo -type f | sort
sqlite3 /tmp/agentdir-v1-demo/indexes/agentdir.sqlite3 '.tables'
```

## Doctor Test Notes

The demo script covers the healthy path only. `doctor` still needs targeted negative coverage for the acceptance criteria in `tasks/BACKLOG.md` and `tasks/COMMIT_PLAN.md`.

Minimum follow-up cases:

1. Malformed root
   Create a root missing one of `tmp`, `new`, or `cur`, then confirm `doctor` reports the exact mailbox defect.
2. Duplicate `Message-ID`
   Place or emit two visible envelopes that share the same `Message-ID`, then confirm `doctor` reports the duplicate without hiding unrelated records.
3. Malformed envelope
   Drop a broken message into `new` and confirm `doctor` reports a parse failure with the file path.
4. Missing blob
   Once artifact references exist, remove a referenced blob under `artifacts/blobs/sha256/` and confirm `doctor` reports the missing object.

Those cases belong in automated tests for the final V1 implementation, but the checklist here keeps the expected coverage explicit before code lands.
