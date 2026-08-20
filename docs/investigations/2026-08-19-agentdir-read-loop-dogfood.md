# AgentDir Read-and-Decide Loop Dogfood

Date: 2026-08-19

## Executive Summary

The new read-and-decide loop is a meaningful improvement: a normal
`agentdir work start` automatically presented a bounded numbered briefing,
the agent recorded one source as used and the other three as dismissed, and
the final report exposed the complete retrieval-to-citation funnel.

The dogfood also exposed an important remaining gap: AgentDir now makes
**source selection** obvious, but not yet **deep source reading**. The useful
source's terminal excerpt ended before most of its actual contract. Reopening
the briefing printed the same truncated text. The full 500-character source
excerpt was available through `memory explain`, but the main workflow did not
advertise that path or expose the source ID needed to run it.

The strongest next improvement is therefore a first-class expansion command,
such as `agentdir work context --read 1`, printed beside every briefing. That
would let an agent move naturally from "this looks relevant" to "I read the
actual prior evidence" before recording a disposition.

No screenshots were necessary for this terminal-centered test. Exact copied
output is included below so labels, counters, excerpts, commands, errors, and
stream behavior remain searchable.

## Scope and Environment

- AgentDir source checkout: `/Users/jstxn/Development/agentdir`
- Source Git HEAD: `a77a5e3230a31b1395ef1c22c1d5e5489c6c50f7`
- Source CLI under test: AgentDir `0.8.0`
- Python: `3.14.5`
- Isolated test repository: `/tmp/agentdir-read-loop-dogfood.bvmn7p`
  (resolved by macOS as `/private/tmp/agentdir-read-loop-dogfood.bvmn7p`)
- Ambient globally installed CLI: `/Users/jstxn/.local/bin/agentdir`, AgentDir
  `0.7.8`
- Durable repository change from this investigation: this report only
- AgentDir source-code changes: none
- Commits or pushes in the AgentDir source repository: none

The source checkout already contained the uncommitted read-loop implementation
under review. This investigation exercised that working tree through:

```text
/Users/jstxn/Development/agentdir/.venv/bin/python -m agentdir
```

This matters because the printed copy-and-paste commands use the bare
`agentdir` executable, which resolved to a different installed version during
the experiment.

## Method

The test used one fresh Git repository and two intentional work sessions:

1. Initialize and adopt the temporary repository.
2. Seed a prior session named `Document tag slug normalization contract`.
3. Record a decision containing a concrete Unicode slug-normalization contract.
4. Finish the seed session and commit its contract document.
5. Start a separate implementation session named
   `Implement normalize_tag_slug from the documented normalization contract`.
6. Read the automatically printed briefing and reopen it from the persisted
   pack.
7. Record source 1 as used with a reason; let the high-level decision dismiss
   the other three presented sources.
8. Implement `normalize_tag_slug` and four `unittest` cases in the temporary
   repository.
9. Run the tests through `agentdir run`, record a structured test claim, cite
   the used context, inspect status, audit the session and claim, preview the
   final report, and finish the work session.
10. Re-audit the ended session by explicit ID and run AgentDir doctor.

This exercises the high-level path defined by the default-on context parser,
numbered briefing, review command, and finish path rather than only calling
low-level context primitives. See
[`cli.py`](../../src/agentdir/cli.py#L2070),
[`control.py`](../../src/agentdir/control.py#L394), and
[`control.py`](../../src/agentdir/control.py#L485).

## Exact Terminal Transcript Excerpts

### 1. Adoption was not isolated by default

The documented short command tried to update the user-level Codex skill even
though the current repository was a temporary fixture:

```console
$ /Users/jstxn/Development/agentdir/.venv/bin/python -m agentdir adopt --gitignore user
Traceback (most recent call last):
  ...
  File "/Users/jstxn/Development/agentdir/src/agentdir/skills.py", line 191, in install_codex_skill
    backup_path.write_text(existing, encoding="utf-8")
  ...
PermissionError: [Errno 1] Operation not permitted: '/Users/jstxn/.codex/skills/agentdir/SKILL.md.bak'
```

The failure occurred after `.agentdir` had already been created. Adoption then
succeeded with every non-store installation surface explicitly disabled:

```console
$ /Users/jstxn/Development/agentdir/.venv/bin/python -m agentdir adopt --gitignore user --install-skill none --install-generic none --install-integrations none
root=/private/tmp/agentdir-read-loop-dogfood.bvmn7p/.agentdir
doctor_ok=true
hooks=5
gitignore=user:exists:unchanged:/Users/jstxn/.gitignore_global
next=agentdir work start "<task>"
```

This follows directly from `adopt` defaulting to a user skill, project generic
guidance, and all integrations, then creating/resolving the root before those
writes complete. See [`cli.py`](../../src/agentdir/cli.py#L2027) and
[`cli.py`](../../src/agentdir/cli.py#L1072).

### 2. An empty store had an honest no-context state

The seed session required no artificial review decision:

```console
$ .../.venv/bin/python -m agentdir work start 'Document tag slug normalization contract'
session=repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204326Z-349379000
task=Document tag slug normalization contract
memory_hits=0
evidence=0
recent_session_summaries=0
federated=false
context_pack=ctx-20260819T204326Z-746b3257d983
context_match=no_context_available
context_presented=0
context_omitted=0
context_review=not_applicable
```

That behavior matches the briefing contract: a review is required only when
the briefing actually presents sources. See
[`context.py`](../../src/agentdir/context.py#L1128).

### 3. `emit --body` was ambiguous

The first attempt treated inline prose as a body, but `--body` means a file
path:

```console
$ .../.venv/bin/python -m agentdir emit --type decision --subject 'Tag slug normalization contract' --body 'Implement normalize_tag_slug with Unicode NFKD normalization, ...'
Traceback (most recent call last):
  ...
FileNotFoundError: [Errno 2] No such file or directory: 'Implement normalize_tag_slug with Unicode NFKD normalization, ...'
```

The successful command used the contract file:

```console
$ .../.venv/bin/python -m agentdir emit --type decision --subject 'Tag slug normalization contract' --body docs/tag-slug-contract.md
/private/tmp/agentdir-read-loop-dogfood.bvmn7p/.agentdir/sessions/repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204326Z-349379000/Maildir/new/1787172219751897000.P58923Q1R8e774d2015301c4c.jstxn-mbp.local
```

`read_body()` explicitly treats a non-`-` value as a path, but the command help
only displays `--body BODY`. See [`cli.py`](../../src/agentdir/cli.py#L129) and
[`cli.py`](../../src/agentdir/cli.py#L1740).

### 4. The main session received a visible briefing

This is the most important user-facing output from the run:

```console
$ .../.venv/bin/python -m agentdir work start 'Implement normalize_tag_slug from the documented normalization contract'
session=repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204408Z-270413000
task=Implement normalize_tag_slug from the documented normalization contract
memory_hits=8
evidence=0
recent_session_summaries=3
federated=false
context_pack=ctx-20260819T204408Z-ee400c100fe3
context_match=no_strong_prior_context
context_presented=4
context_omitted=6
[1] possible retrieval_hint: Tag slug normalization contract
    message: <178717221975.58923.11947399259501434809@agentdir.local> session: repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204326Z-349379000 event: decision subject: Tag slug normalization contract # Tag slug normaliz...
[2] possible retrieval_hint: work started: Document tag slug normalization contract
    message: <178717220637.58833.13984132119354456684@agentdir.local> session: repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204326Z-349379000 event: work.started subject: work started: Document tag slug normalization c...
[3] weak summary: session summary: repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204405Z-187686000
    Session summary: repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204405Z-187686000 Events: 3 Event counts: git.hook.post-commit=1, session.ended=1, session.started=1 Key records: - session.started: session_id=repo-age...
[4] weak summary: session summary: repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204404Z-797445000
    Session summary: repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204404Z-797445000 Events: 3 Event counts: git.hook.pre-commit=1, session.ended=1, session.started=1 Key records: - session.started: session_id=repo-agen...

context_use=agentdir work context --pack ctx-20260819T204408Z-ee400c100fe3 --use <number> --reason "<how it helps>"
context_none=agentdir work context --pack ctx-20260819T204408Z-ee400c100fe3 --none-relevant --reason "<why none help>"
context_skip=agentdir work context --pack ctx-20260819T204408Z-ee400c100fe3 --skip --reason "<why review was not possible>"
```

The presentation is bounded to five sources, labels match quality and source
class, truncates terminal excerpts to 220 characters, and prints the three
possible dispositions. See [`context.py`](../../src/agentdir/context.py#L34),
[`context.py`](../../src/agentdir/context.py#L1196), and
[`control.py`](../../src/agentdir/control.py#L394).

### 5. The printed command failed under version skew

The literal copy-and-paste command used the ambient installed CLI, not the
source CLI that created the pack:

```console
$ command -v agentdir
/Users/jstxn/.local/bin/agentdir
$ agentdir --version
agentdir 0.7.8
$ agentdir work context --pack ctx-20260819T204408Z-ee400c100fe3 --use 1 --reason 'Defines the exact Unicode, separator, and empty-result behavior required by the implementation.'
usage: agentdir work [-h] {start,finish} ...
agentdir work: error: argument work_command: invalid choice: 'context' (choose from 'start', 'finish')
```

This is primarily a source-development and staged-rollout problem, not evidence
that a fully upgraded installation will fail. It is still a serious dogfood
problem because the source formatter hard-codes the bare executable in every
next command. See [`control.py`](../../src/agentdir/control.py#L427).

### 6. The source-bound review command was concise

```console
$ .../.venv/bin/python -m agentdir work context --pack ctx-20260819T204408Z-ee400c100fe3 --use 1 --reason 'Defines the exact Unicode, separator, and empty-result behavior required by the implementation.'
context_reviewed=ctx-20260819T204408Z-ee400c100fe3
disposition=used
reviewed=4
used=1
dismissed=3
recorded=true
reason=Defines the exact Unicode, separator, and empty-result behavior required by the implementation.
```

Selecting source 1 is a terminal declaration over all four presented sources:
the selected source is used, every presented source is marked reviewed, and the
other three are dismissed. That is enforced by the context decision validator,
not merely inferred for display. See
[`context.py`](../../src/agentdir/context.py#L1657).

The stored event also says that the declaration is cooperative and cannot prove
model attention, but the concise terminal result above does not repeat that
boundary. See [`context.py`](../../src/agentdir/context.py#L1784).

### 7. Full source reading required a lower-level command

The plain briefing and `work context --show` both ended source 1 at
`# Tag slug normaliz...`. After the work session, this lower-level command
showed that AgentDir had retained a much more useful excerpt:

```console
$ .../.venv/bin/python -m agentdir memory explain 'Implement normalize_tag_slug from the documented normalization contract' --source 'message:sessions/.../1787172219751897000.P58923Q1R8e774d2015301c4c.jstxn-mbp.local'
query=Implement normalize_tag_slug from the documented normalization contract
source_id=message:sessions/repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204326Z-349379000/Maildir/new/1787172219751897000.P58923Q1R8e774d2015301c4c.jstxn-mbp.local
source_kind=message
score=0.254067
session=repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204326Z-349379000
overlap=contract, normalization, normalize_tag_slug
passage_id=6
passage_score=0.254067
lexical_score=
passage_excerpt:
message: <178717221975.58923.11947399259501434809@agentdir.local> session: ... # Tag slug normalization contract The `normalize_tag_slug` helper converts a human tag to a stable URL slug. - Normalize Unicode text to NFKD and drop combining marks. - Lowercase ASCII letters. - Replace each run of non-alphanumeric characters with one hyphen. - Strip leading and trailing hyphens....
excerpt:
# Tag slug normalization contract The `normalize_tag_slug` helper converts a human tag to a stable URL slug. - Normalize Unicode text to NFKD and drop combining marks. - Lowercase ASCII letters. - Replace each run of non-alphanumeric characters with one hyphen. - Strip leading and trailing hyphens. - Raise `ValueError` when normalization produces an empty slug. Examples: - `"Café Launch"` becomes `"cafe-launch"`. - `" Release / Notes "` becomes `"release-notes"`.
```

`memory explain` supports a source-specific read and formats two
500-character excerpts. See [`memory.py`](../../src/agentdir/memory.py#L878).
The high-level briefing, however, exposes only ordinals in plain output. An
agent must request JSON to recover the opaque source ID, and that JSON was 1,145
lines for this four-item briefing because it also included the complete pack.

During the actual implementation I opened the current workspace contract file
directly. I therefore did use the prior decision to identify the governing
contract, but the deepest read happened through the repository rather than an
obvious AgentDir expansion interaction. This is exactly the distinction the
original product concern was trying to expose.

### 8. Evidence capture streamed normal test output

```console
$ .../.venv/bin/python -m agentdir run -- .../.venv/bin/python -m unittest -v
test_collapses_separator_runs (test_tag_toolkit.NormalizeTagSlugTests.test_collapses_separator_runs) ... ok
test_preserves_digits (test_tag_toolkit.NormalizeTagSlugTests.test_preserves_digits) ... ok
test_rejects_empty_result (test_tag_toolkit.NormalizeTagSlugTests.test_rejects_empty_result) ... ok
test_removes_combining_marks (test_tag_toolkit.NormalizeTagSlugTests.test_removes_combining_marks) ... ok

----------------------------------------------------------------------
Ran 4 tests in 0.001s

OK
```

Python's `unittest -v` wrote this output to stderr. AgentDir streamed it to the
terminal and preserved it under the stderr section of the immutable
`tool.result`. The wrapper tees stdout and stderr separately while capturing
both. See [`capture.py`](../../src/agentdir/capture.py#L109).

The structured claim was then recognized as supported:

```console
$ .../.venv/bin/python -m agentdir claim test --passed --note '4 unittest cases passed for normalize_tag_slug'
claim=test outcome=passed session=repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204408Z-270413000

$ .../.venv/bin/python -m agentdir evidence --brief
Latest evidence by family:
test: Wed, 19 Aug 2026 20:44:43 +0000 tool.result python exit=0 tool.result python exit 0
```

```json
{
  "family": "test",
  "message": "latest test evidence succeeded",
  "outcome": "passed",
  "status": "supported"
}
```

The evidence classifier recognizes `unittest`; the claim audit compares the
latest classified tool result and its exit code to the structured claim. See
[`review.py`](../../src/agentdir/review.py#L11) and
[`audit.py`](../../src/agentdir/audit.py#L185).

The seed session also demonstrated the guardrail in the opposite direction. I
incorrectly called `git diff --check` a lint check, and AgentDir reported:

```json
{
  "family": "lint",
  "message": "claimed lint passed but no lint evidence was recorded",
  "outcome": "passed",
  "status": "unsupported"
}
```

I retracted that claim rather than overstating the evidence.

### 9. Citation was explicit and source-bound

```console
$ .../.venv/bin/python -m agentdir context cite --pack ctx-20260819T204408Z-ee400c100fe3
# AgentDir Context Citations

Pack: ctx-20260819T204408Z-ee400c100fe3
Enforcement: advisory

- `retrieval_hint` `message:sessions/repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204326Z-349379000/Maildir/new/1787172219751897000.P58923Q1R8e774d2015301c4c.jstxn-mbp.local` decision Tag slug normalization contract
  file: `sessions/repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204326Z-349379000/Maildir/new/1787172219751897000.P58923Q1R8e774d2015301c4c.jstxn-mbp.local`
  excerpt: message: <178717221975.58923.11947399259501434809@agentdir.local> session: ...
```

When a review is required, default citation selects only consumed/used sources;
it refuses to cite an unused source set. See
[`context.py`](../../src/agentdir/context.py#L1582).

### 10. Status exposed the funnel clearly

The Rich status table contained this compact context section:

```text
context  latest_pack          ctx-20260819T204408Z-ee400c100fe3
context  pack_count           1
context  blocking_packs
context  attention_packs
context  retrieved            10
context  presented            4
context  reviewed             4
context  used                 1
context  consumed             1
context  additional_consumed  0
context  dismissed            3
context  pending              0
context  cited                1
context  cited_without_use    0
context  review_status        complete
context  decision_complete    true
evidence count                2
health   doctor_ok            true
```

This is the strongest human inspection surface in the current implementation.
The table deliberately exposes each stage rather than collapsing everything to
"context was used." See [`rendering.py`](../../src/agentdir/rendering.py#L7).

After `work finish`, status showed `session active false` and `evidence count
0` because evidence is scoped to an active session, while it continued to show
the latest completed pack's context funnel. That asymmetry is understandable
from the data model but may surprise a user reading status after completion.

### 11. The final report and handoff were useful

The human-readable preview reduced the run to the important facts:

```text
## Context

- pack: `ctx-20260819T204408Z-ee400c100fe3`
- blocking_packs: none
- attention_packs: none
- retrieved: 10
- presented: 4
- reviewed: 4
- used: 1
- consumed: 1
- additional_consumed: 0
- dismissed: 3
- pending: 0
- cited: 1
- cited_without_use: 0
- review_status: complete
- decision_complete: true
- review_reason: Defines the exact Unicode, separator, and empty-result behavior required by the implementation.
- evidence_backed: 0

## Claim Support

- test: supported - latest test evidence succeeded

## Agent Handoff

- status: ok
- action: none
```

The JSON returned by `work finish --json` agreed:

```json
{
  "agent_handoff": {
    "context_lineage": {
      "cited": 1,
      "decision": "used",
      "decision_complete": true,
      "dismissed": 3,
      "pending": 0,
      "presented": 4,
      "retrieved": 10,
      "review_status": "complete",
      "reviewed": 4,
      "used": 1
    },
    "failed_evidence": [],
    "known_gaps": [],
    "recommended_agent_actions": [],
    "status": "ok"
  }
}
```

The final formatter intentionally prints the full funnel, session checks,
claim support, handoff, health, and known gaps. See
[`control.py`](../../src/agentdir/control.py#L750). `work finish` also refuses a
pack whose review has not reached an allowed terminal state. See
[`control.py`](../../src/agentdir/control.py#L548) and
[`context.py`](../../src/agentdir/context.py#L809).

## Observations from the Agent Perspective

### What worked

1. **Context appeared without a special retrieval command.** This is the most
   important behavior change. The normal start command made prior context part
   of the hot path.
2. **The decision was easy to record.** Choosing ordinal `1` was much easier
   than copying a Maildir source ID.
3. **The review was inspectable.** The output explicitly said four reviewed,
   one used, and three dismissed. Status, audit, citation, and handoff all
   agreed.
4. **Evidence instrumentation stayed out of the test's way.** Test output
   streamed normally, while the result became immutable test evidence.
5. **Claim auditing caught a real operator mistake.** The unsupported lint
   claim was rejected even though its underlying diagnostic command exited 0.
6. **The final handoff was decision-ready.** `status: ok`, supported test
   evidence, no pending context, and the exact use reason were visible without
   reading the raw store.

### Friction and ambiguity

1. **The briefing enabled triage more than reading.** The relevant source's
   clean contract was mostly pushed out of the 220-character terminal excerpt
   by envelope metadata. A high-level `--read 1` path was missing.
2. **Reopening did not deepen the read.** `work context --show` reproduced the
   same truncated briefing. JSON exposed source IDs, but produced over one
   thousand lines because it included the whole pack.
3. **The best source looked weaker than it felt.** It shared the exact
   `normalize_tag_slug` identifier plus `normalization` and `contract`, yet its
   score of `0.409` remained `possible` because hybrid strong quality requires
   `0.55`. The behavior follows the current policy, but a decision event with
   an exact identifier match may deserve an authority/type boost. See
   [`context.py`](../../src/agentdir/context.py#L42) and
   [`context.py`](../../src/agentdir/context.py#L1252).
4. **Git-hook sessions occupied half the briefing.** Committing the seed
   contract generated standalone pre-commit and post-commit sessions. Their
   weak summaries became sources 3 and 4 even though they had no task-term
   overlap. This is expected from the hook implementation, which creates a
   short session when no work session is active, but those summaries are poor
   default reading material. See [`hooks.py`](../../src/agentdir/hooks.py#L267).
5. **A selected source implicitly closes every presented source.** The command
   records all four as reviewed and automatically dismisses the unselected
   three. That is efficient, but `reviewed=4` can look stronger than the user
   action of typing `--use 1`. The terminal should call the other three
   `auto_dismissed` or restate that this is an advisory attestation.
6. **Counts required interpretation.** Start showed eight memory hits and
   three recent summaries, while the pack reported ten retrieved sources after
   deduplication. That is internally reasonable but not obvious from the text.
7. **The copied command depended on ambient installation state.** This broke
   the source dogfood path completely until the full Python-module invocation
   was substituted.
8. **Adoption was not transactionally isolated.** The first failure left a
   partial store and required three explicit `none` flags to avoid global and
   project integration writes.
9. **`emit --body` looked literal but was path-only.** The traceback was the
   first indication of that contract.

## Observations from the User Perspective

### What the user can now trust

The final surfaces answer substantially better questions than the previous
write-heavy behavior:

- What was retrieved?
- What was actually presented to the agent?
- What did the agent declare it reviewed?
- Which source did it use?
- Which sources did it dismiss?
- Is anything still pending?
- Did it cite the used source?
- Does fresh test evidence support the verification claim?

The status table and final report make these answers auditable. This is a real
improvement over merely observing that a context pack was written.

### What the user still cannot see automatically

In this Codex run, AgentDir's briefing and status lived inside command output,
not in a dedicated user-facing AgentDir panel. A user would need to expand the
tool activity or depend on the agent to summarize the result. The app does not
automatically promote "source 1 was used for this reason" into the final answer.

More importantly, `reviewed=4` is a cooperative declaration. AgentDir cannot
prove model attention, and the stored review body says so. The user can trust
that the integrated agent made a signed, internally consistent disposition;
they cannot infer four deep reads from that number alone.

My honest description of this run is:

> AgentDir successfully put relevant prior work in front of me, I selected it
> as governing context, and the lifecycle recorded that decision. The default
> excerpt was not sufficient for a deep read, so I opened the current contract
> file directly; `memory explain` later confirmed AgentDir retained the fuller
> source content.

That distinction should remain visible in the product language.

## Corroborating Red-to-Green Run

A second isolated run at `/tmp/agentdir-ux-primary.5FFe14` used a smaller
stdlib-only task: extend an `Atlas` slug normalizer to collapse mixed separator
runs and reject separator-only input. This fixture disabled hooks and every
integration surface, which kept its briefing free of hook-only sessions.

The normal start command found the seeded decision and made selection obvious:

```text
memory_hits=8
context_match=no_strong_prior_context
context_presented=2
context_omitted=6
[1] possible retrieval_hint: Atlas slug separator contract
[2] possible summary: session summary: repo-agentdir-ux-primary...

context_use=agentdir work context --pack ctx-20260819T204311Z-71b15c7cd5be --use <number> --reason "<how it helps>"
context_none=agentdir work context --pack ctx-20260819T204311Z-71b15c7cd5be --none-relevant --reason "<why none help>"
context_skip=agentdir work context --pack ctx-20260819T204311Z-71b15c7cd5be --skip --reason "<why review was not possible>"
```

The recorded decision was equally clear:

```text
context_reviewed=ctx-20260819T204311Z-71b15c7cd5be
disposition=used
reviewed=2
used=1
dismissed=1
recorded=true
reason=The prior decision gives the exact separator-collapse and empty-input contract to implement
```

This run intentionally followed a normal red-green test cycle. AgentDir kept
both facts visible rather than replacing history:

```text
Failed evidence:
- Wed, 19 Aug 2026 20:43:34 +0000 tool.result python exit=1 failed=true tool.result python exit 1

Latest evidence by family:
test: Wed, 19 Aug 2026 20:43:42 +0000 tool.result python exit=0 tool.result python exit 0
```

The final report simultaneously showed a supported test claim and a
`needs_attention` handoff:

```text
## Claim Support

- test: supported - latest test evidence succeeded

## Agent Handoff

- status: needs_attention
- action: Inspect failed evidence and rerun the relevant check after fixing it.

## Known Gaps

- failed_tool_results: 1 failed tool result(s)
```

Keeping the red run is valuable evidence. The recommended action is stale,
however, because the exact requested rerun already succeeded and supports the
claim. A human can reconcile this from the report, but the headline makes an
ordinary TDD cycle look unresolved.

The recovery surface also lagged behind the status surface. After the decision
and even after finish, `work context --show --pack ...` reproduced the original
briefing and all three decision commands without displaying the recorded
disposition, reason, or `review_status=complete`. By contrast, `status` showed:

```text
context  retrieved          8
context  presented          2
context  reviewed           2
context  used               1
context  dismissed          1
context  pending            0
context  cited              1
context  review_status      complete
context  decision_complete  true
```

This confirms that the audit model has the right state, but the persisted
briefing renderer is not decision-aware.

## Recommendations

### P0: Make deep reading the next obvious action

Add one of these high-level forms:

```text
agentdir work context --read 1
agentdir work context --show 1
agentdir work context --expand 1
```

It should print:

- a clean source body or substantially longer excerpt;
- subject, event type, prior session, timestamp, and Git head;
- a concise provenance path;
- match reasons and score;
- the exact `--use 1` / `--none-relevant` next command.

The default briefing should advertise this command beside each item or once
under the list. This directly addresses whether agents are reading context
rather than only recording dispositions.

### P0: Preserve the executable used to generate next commands

The formatter should either:

- render the current invocation prefix safely;
- expose a configurable command prefix for source/dev installs; or
- detect and warn when the bare installed `agentdir` version cannot satisfy the
  printed workflow.

At minimum, include `agentdir_version=0.8.0` in the briefing so version-skew
failures are immediately legible.

### P1: Exclude hook-only summaries from the default reading briefing

Keep Git-hook events as diagnostic evidence, but do not spend scarce briefing
slots on sessions whose only substantive events are
`git.hook.pre-commit`/`git.hook.post-commit` and session boundaries. They can
remain retrievable through explicit evidence or recent-history inspection.

### P1: Make reopened briefings decision-aware

`work context --show` should join the persisted briefing to its current audit.
Once terminal, it should display the disposition, used/dismissed counts, reason,
and citation state. It should stop printing commands that imply a new decision
is still required, while retaining source-expansion and citation paths.

### P1: Distinguish repaired failures from unresolved failures

Keep historical red evidence visible, but if the latest evidence for the same
family passes and the structured claim is supported, do not recommend another
rerun. A handoff state such as `ok_with_history`, or a `resolved_failures`
section separate from `known_gaps`, would preserve provenance without making a
completed red-green cycle look blocked.

### P1: Make isolated adoption first-class and transactional

Add an explicit mode such as:

```text
agentdir adopt --isolated
```

It should mean store plus optional local hooks, no user skill, no generated
project guidance, no integrations, and no global ignore mutation unless asked.
Adoption should preflight all selected destinations before mutating any of them,
or roll back the new store on failure.

### P1: Improve authority-aware quality

Consider a bounded boost when all of these are true:

- exact specific identifier overlap exists;
- at least two other task terms overlap;
- the source is a prior `decision`, failure, or final handoff rather than a
  generic lifecycle event.

This should not make every lexical overlap strong. It should help the quality
label reflect an exact governing decision without lowering the global score
threshold indiscriminately.

### P2: Make disposition semantics more candid in terminal output

Prefer output such as:

```text
presented=4
selected_used=1
auto_dismissed=3
attestation=advisory
```

This is more faithful to the single command the agent actually issued than
`reviewed=4` alone.

### P2: Reconcile candidate counts in the briefing

Name each layer explicitly:

```text
memory_candidates=8
recent_candidates=3
retrieved_unique=10
presented=4
omitted=6
```

This removes the need for the user to infer deduplication.

### P2: Clarify text/file arguments

Replace ambiguous `--body` with explicit `--body-file` and `--body-text`, or
apply the existing read-file-if-present/otherwise-literal behavior consistently.

### P2: Surface the handoff in the agent's final answer

Generated guidance should require a one-sentence user-facing context summary
when sources were presented, for example:

> AgentDir presented four prior sources; I used the prior slug contract,
> dismissed three unrelated lifecycle summaries, and finished with no pending
> context.

This gives the user the benefit of the audit without making them inspect CLI
output.

## Validation

The isolated implementation session ended successfully as:

```text
session=repo-agentdir-read-loop-dogfood.bvmn7p-20260819T204408Z-270413000
pack=ctx-20260819T204408Z-ee400c100fe3
agent_handoff.status=ok
context.review_status=complete
context.retrieved=10
context.presented=4
context.reviewed=4
context.used=1
context.dismissed=3
context.pending=0
context.cited=1
test_claim.status=supported
doctor.ok=true
```

Final explicit-session validation reported:

- session start: pass
- session finish: pass
- two evidence records: pass
- failed tool results: none
- truncated evidence: none
- context pack created: pass
- four presented sources reviewed: pass
- one source used: pass
- one used source cited: pass
- doctor: pass
- worktree dirty: warning, expected because the isolated implementation files
  were intentionally left uncommitted
- tests: 4 passed

The temporary repository remains available at
`/tmp/agentdir-read-loop-dogfood.bvmn7p` for inspection. It contains the
isolated `.agentdir` store, the seed contract, `tag_toolkit.py`, and
`test_tag_toolkit.py`. Nothing from that fixture was copied into the AgentDir
source tree.

## Conclusion

The read-loop implementation succeeds at making context presentation and
disposition inspectable. It materially changes AgentDir from "a pack was
written" to "these sources were shown, this one was used for this reason, the
others were dismissed, and nothing is pending."

The remaining product gap is exactly where the original concern points: an
agent can now record a credible context decision more easily than it can deeply
read the source behind that decision. A first-class numbered source expansion
path, cleaner excerpts, and removal of hook-session noise would make reading
the most obvious and most defensible AgentDir interaction.

## Follow-up Design Investigation: First-Class Context Expansion

This section is a separate design lane. It does not describe code implemented
by this investigation. It asks how AgentDir can make a deep read a first-class
interaction without weakening the existing context-review lifecycle or forcing
callers to understand every source kind, storage location, presentation depth,
and receipt rule.

The recommendation is a flexible but deep `ContextExpansion` module. Its
external seam has one operation. Source resolution, content budgeting,
redaction, delivery, and the optional audit receipt remain hidden in its
implementation behind internal seams with real adapters.

### Decision

Use this public CLI shape:

```text
agentdir work context --pack <pack-id> --expand <number> [--expand <number>] [--depth relevant|source]
```

Do not overload `--show` with a value and do not call the operation `--read`.

- `--show` is already a boolean member of the required mutually exclusive
  disposition group. Changing it to `--show [selector]` changes argparse value
  shape and creates ambiguity for existing callers
  ([`src/agentdir/cli.py:2070-2107`](../../src/agentdir/cli.py#L2070-L2107)).
- `read` implies model attention that AgentDir cannot prove. The current review
  body already says that review is a cooperative declaration, not proof of
  attention
  ([`src/agentdir/context.py:1784-1813`](../../src/agentdir/context.py#L1784-L1813)).
- `expand` makes a narrower, testable claim: AgentDir resolved a presented
  source and delivered a larger representation to the caller.
- If compatibility with an experimental `--read` spelling becomes necessary,
  it can be a hidden alias to `--expand`; persisted events, JSON, status, and
  audit fields should still use `expanded`, never `read`.

`--expand` should be added to the existing mutually exclusive action group.
It is nonterminal and takes no `--reason`. Repeating it should select several
presented sources in one bounded delivery and produce at most one metadata-only
receipt. Batching keeps a source-reading interaction from looking like several
knowledge writes.

### Why a module rather than another command branch

The complexity is not argument parsing. One request may refer to a local
message, a derived session summary, evidence, or a federated child root. The
requested representation can be a relevant passage or a bounded source body.
The pack may be active, terminal-but-active, historical, legacy, or malformed.
The delivery may be plain terminal output, JSON, or eventually MCP/UI content.
Receipt policy may be automatic, required, or disabled.

Putting those decisions in the CLI handler would create a shallow module: the
caller would need to know nearly as much as the implementation. A deep
`ContextExpansion` module gives CLI, MCP, and future UI callers leverage from
one small interface and keeps resolution, privacy, and audit changes local.
Deleting that module should cause the complexity to reappear across all of
those callers; that is the deletion test that shows the module earns its seam.

### External interface

An illustrative Python interface is:

```python
@dataclass(frozen=True)
class ExpansionRequest:
    target: ContextTarget              # pack, session-latest, or active-latest
    selectors: tuple[str, ...]          # "1", "s1", or a presented source id
    depth: ExpansionDepth = RELEVANT    # RELEVANT or SOURCE; both bounded

@dataclass(frozen=True)
class ExpansionOutcome:
    pack_id: str
    session_id: str
    items: tuple[ExpandedSource, ...]
    delivered: bool
    receipt: ExpansionReceipt           # recorded, not_recorded, or failed

class ContextExpansion:
    def expand(self, request: ExpansionRequest, delivery: ExpansionDelivery) -> ExpansionOutcome: ...
```

This is one operation at the external seam. `ExpansionDelivery` is an injected
adapter because at least terminal, JSON, and in-memory test deliveries are real
variants. Callers do not select a source resolver, byte budget, redactor,
federated transport, or receipt writer.

The interface includes these invariants:

1. Resolve one exact pack before resolving ordinals. Printed commands always
   include `--pack`, so a later pack cannot retarget an earlier briefing.
2. Resolve selectors only against the sources actually displayed by
   `brief_context_manifest()`, not every retrieved source.
3. Validate and deduplicate the whole selector batch before retrieving or
   delivering anything. An invalid selector produces no partial output and no
   receipt.
4. Every delivered item reports `content_state` as `full`, `bounded`,
   `stored_excerpt`, or `unavailable`, plus byte count, truncation, and
   redaction metadata. A stored excerpt must never be presented as a full read.
5. Both depths are bounded. `source` means “the source representation up to the
   safety cap,” not an unbounded terminal dump.
6. Expansion never changes reviewed, used, dismissed, pending, disposition,
   decision ID, `finish_allowed`, or `lineage_valid`.
7. A receipt means the delivery adapter accepted the rendered result. It does
   not mean a model or human attended to it.
8. Expanded content is never copied into the receipt event. Only identifiers,
   hashes, depth, size, truncation, redaction count, and content state persist.

The error interface should expose stable codes with plain and JSON renderings:

| Code | Meaning | Receipt |
| --- | --- | --- |
| `context_pack_not_found` | Target did not resolve to an immutable pack | none |
| `context_pack_invalid` | Manifest identity, digest, or briefing is malformed | none |
| `context_selector_invalid` | Ordinal/ID is unknown or was not presented | none |
| `context_source_unavailable` | No resolver and no stored excerpt can satisfy a source | none |
| `context_source_unsafe` | Path/root/privacy validation rejected resolution | none |
| `context_delivery_failed` | Output adapter did not accept the result | none |
| `context_receipt_failed` | Content was delivered but a required receipt could not be written | failed |

In automatic receipt mode, a receipt failure should return
`delivered=true, receipt=failed` with a warning rather than pretending delivery
did not happen. In required mode it may be a nonzero command result. That
distinction prevents an audit-side failure from falsifying the read-side
outcome.

### Proposed usage

The start/show briefing should add one pack-bound instruction alongside the
existing terminal decisions:

```text
[2] high retrieval_hint: Preserve explicit user slugs
    Canonical slugs are normalized to lowercase and ...

context_expand=agentdir work context --pack ctx-123 --expand <number>
context_use=agentdir work context --pack ctx-123 --use <number> --reason "<how it helps>"
context_none=agentdir work context --pack ctx-123 --none-relevant --reason "<why none help>"
```

A proposed expansion response is:

```text
pack=ctx-123
source_ref=2
source_id=message:abc
depth=relevant
content_state=bounded
truncated=false
receipt=recorded

Canonical slugs are normalized to lowercase. Explicit caller-provided slugs
must be preserved after validation; only inferred slugs are regenerated.

next=agentdir work context --pack ctx-123 --use 2 --reason "<how it helps>"
```

The agent can then make the same existing terminal decision. Expansion is
useful evidence about delivery, not a new disposition and not a prerequisite
for `--use`.

### Hidden implementation and internal seams

`ContextExpansion.expand()` should own the sequence:

1. Resolve `ContextTarget` to an immutable pack.
2. Read and validate the manifest.
3. Reconstruct the exact displayed briefing and map selectors.
4. Ask the resolver chain for each source representation.
5. Apply the requested depth, byte budget, redaction, and output hash.
6. Hand the structured batch to the delivery adapter.
7. Apply receipt policy after successful delivery.
8. Return one structured outcome used by both plain and JSON callers.

The internal resolver seam is justified by multiple concrete adapters:

- `LocalMessageResolver` reads canonical local message/evidence bodies.
- `DerivedSummaryResolver` reads the derived memory document represented by a
  session-summary source.
- `FederatedRootResolver` validates the registered child-root identity and
  resolves the original source there.
- `StoredExcerptResolver` is the final fallback and labels its result
  `stored_excerpt`.

Resolvers accept the manifest source descriptor and return a common
`ResolvedContent`; they do not render, persist receipts, or decide review
state. This gives locality: adding a future artifact, remote root, or encrypted
store changes one adapter without changing the CLI or audit interface.

Depth is a policy inside the module, not a resolver concern:

- `relevant` is the default bounded passage with enough adjacent context to
  evaluate the source.
- `source` requests the bounded source body. It can still return
  `truncated=true`; there is deliberately no unsafe “unlimited” mode.

Delivery is another real internal seam:

- `PlainTerminalDelivery` renders compact metadata plus content.
- `JsonDelivery` returns the same structured fields without embedding the
  entire manifest.
- `MemoryDelivery` is the local-substitutable test adapter.
- A later `McpDelivery` can carry structured content without changing source
  resolution or receipt semantics.

Receipt policy has three adapters, chosen by command/integration configuration
rather than exposed as routine CLI flags:

- `AutoReceipt`: record only when the pack-owning work session is still active.
- `RequiredReceipt`: fail visibly if the durable receipt cannot be written.
- `NoReceipt`: deliver historical/read-only inspection without mutating an
  ended session.

Current dependencies are in-process or local-substitutable. Selector mapping,
budgeting, redaction, and metric folding are in-process. Maildir, SQLite,
artifact blobs, registered roots, stdout, and receipt envelopes can all be
exercised in temporary repositories. No true external dependency is needed for
the first implementation. A remote federated store would justify a future
resolver port and network adapter; it should not be introduced preemptively.

### Current seams and constraints from primary source

The present command has three separate surfaces:

| Surface | Current behavior | Constraint for expansion |
| --- | --- | --- |
| `work start` | Prints up to five numbered sources and collapses each excerpt to 220 characters | Add one pack-bound expansion instruction; keep the compact briefing bounded ([`control.py:394-439`](../../src/agentdir/control.py#L394-L439)) |
| `work context --show` | Reloads the manifest and calls the same formatter | Expansion must use the identical ordinal mapping and explicit old-pack targeting ([`control.py:442-482`](../../src/agentdir/control.py#L442-L482)) |
| `memory explain` | Recomputes relevance for a query/source and renders 500-character document and passage excerpts | Do not route expansion through this public behavior; reuse lower-level content lookup while preserving the pack's stored match reasons and score ([`memory.py:878-944`](../../src/agentdir/memory.py#L878-L944)) |

The manifest is not itself a deep-read artifact. `_source_entry()` stores rich
provenance but only a 320-character excerpt
([`context.py:1038-1089`](../../src/agentdir/context.py#L1038-L1089)).
`brief_context_manifest()` supplies the ordinal refs from persisted briefing
IDs
([`context.py:1092-1125`](../../src/agentdir/context.py#L1092-L1125)).
Expansion therefore has to resolve canonical source content, not merely print
more of the manifest JSON.

Federated sources need explicit fallback semantics. The federated row already
truncates its copied body to 500 characters while retaining child-root and
original-source provenance
([`federation.py:447-462`](../../src/agentdir/federation.py#L447-L462)). If the
child root is present and still registered, the federated adapter can resolve
the canonical body. Otherwise it may return only the manifest excerpt, clearly
marked `stored_excerpt`; it must not silently call that a full expansion.

### Selectors, packs, and terminal decisions

The current selector helper accepts `1`, `s1`, or a source ID and rejects a
retrieved source that was omitted from the briefing
([`context.py:1544-1571`](../../src/agentdir/context.py#L1544-L1571)). Reuse that
contract, but not the exact legacy helper behavior blindly.

For manifests without a persisted `briefing`, `brief_context_manifest()` builds
a bounded display, while `_briefing_source_ids()` falls back to every manifest
source
([`context.py:1092-1125`](../../src/agentdir/context.py#L1092-L1125),
[`context.py:1574-1579`](../../src/agentdir/context.py#L1574-L1579)). Expansion
must map ordinals from the actual displayed `sources` array. Changing
`_briefing_source_ids()` globally would alter existing legacy review semantics,
so the expansion module should keep its own exact-display mapping.

Expansion is allowed:

- before a `used`, `no_relevant`, or `skipped` decision;
- after a terminal decision while the session remains active;
- against an ended/historical pack for inspection, with `receipt=not_recorded`.

It must not add sources to the used set or alter the terminal decision. Current
decision IDs include disposition, reason, reviewed/used/dismissed IDs, and
purpose
([`context.py:512-655`](../../src/agentdir/context.py#L512-L655)). Keeping
expansion out of that signature allows repeatable reads and preserves existing
idempotency and conflict behavior. A post-decision expansion is visible as
late inspection; it cannot retroactively imply that the source informed the
decision.

### Persisted receipt and audit metrics

`expanded` does belong in persisted audit metrics, with a narrow meaning:
“AgentDir successfully delivered an expanded representation.” It must be
presented to the user as a read receipt, not as an authored knowledge write.
Otherwise the new feature would make the original write-heavy interaction
ratio look worse even though reading improved.

Persist one append-only `context.sources.expanded` envelope per successful
command, not per source, containing:

```text
pack_id
source_ids
depth
content_states
displayed_bytes
content_sha256
truncated
redactions
receipt_policy
```

Do not persist the expanded content. This keeps the receipt small, avoids a
second copy of private tool output, and separates reading from knowledge
creation.

Fold receipts into a nested audit object so future depth/resolver fields do not
sprawl across the top-level audit interface:

```json
{
  "expansion": {
    "expanded_count": 2,
    "expanded_source_ids": ["message:abc", "session-summary:def"],
    "event_count": 1,
    "before_decision_count": 2,
    "after_decision_count": 0,
    "used_without_prior_expansion_count": 0,
    "depth_counts": {"relevant": 2, "source": 0}
  }
}
```

`expanded_count` is the number of unique presented sources expanded at least
once; `event_count` preserves repeat/batch activity. Chronology matters:
`before_decision_count` is the defensible signal that an expansion could have
informed the decision. An expansion recorded after `--use` must not erase
`used_without_prior_expansion_count`.

Status, Rich rendering, final report, and `agent_handoff.context_lineage` should
all project the same counters. Those current projections are separate call
sites
([`control.py:175-223`](../../src/agentdir/control.py#L175-L223),
[`rendering.py:7-57`](../../src/agentdir/rendering.py#L7-L57),
[`control.py:750-848`](../../src/agentdir/control.py#L750-L848),
[`control.py:914-978`](../../src/agentdir/control.py#L914-L978)); deriving one
expansion object in the audit module keeps the meaning local.

Expansion remains optional in the first release. It must not gate `--use`,
`work finish`, or lineage validity. Existing agents and low-level callers can
still complete review without an expansion receipt. This is essential because
the current audit deliberately supports complete and partial compatibility
flows from low-level consume events
([`context.py:717-898`](../../src/agentdir/context.py#L717-L898)).

Malformed expansion events should produce separate
`expansion_validation_errors` and an `expansion_receipts_valid` flag. They may
put the final handoff into `needs_attention`, but should not change the terminal
review decision or block finish in the first release. Letting optional
telemetry invalidate a valid terminal decision would make the receipt side
effect a denial-of-finish vector.

### Backward-compatibility matrix

| Existing artifact/caller | Expected behavior after expansion ships |
| --- | --- |
| Current `work context --show` caller | Unchanged boolean flag and output, plus one additive `context_expand` line when sources exist |
| Current `--use`, `--none-relevant`, `--skip` caller | Unchanged; no expansion is required |
| Low-level `context consume`/`context cite` caller | Existing compatibility and citation rules remain unchanged |
| New CLI reading an old manifest without `briefing` | Expand only the exact sources shown by the reconstructed briefing; fall back to stored excerpt when canonical content is unavailable |
| Old CLI reading a store containing expansion events | Ignores the unknown event type; review folding remains based on consumed/reviewed/cited events |
| New CLI targeting an ended pack | Delivers content but reports `receipt=not_recorded` under automatic policy |
| New CLI targeting a stale federated root | Uses an explicitly labeled stored excerpt or returns `context_source_unavailable` |
| Strict JSON consumer | Receives additive `expansion` object; document it as optional and avoid changing existing field meanings |

Unknown event compatibility is practical because `_context_events()` indexes
any pack-tagged event, while the current audit fold changes state only for the
known consumed and cited event types
([`context.py:1420-1483`](../../src/agentdir/context.py#L1420-L1483),
[`context.py:717-744`](../../src/agentdir/context.py#L717-L744)). New code should
also add the expansion event type to `context_packs()` orphan/wrong-session
discovery; that scan currently lists only consumed, reviewed, and cited actions
([`control.py:1085-1215`](../../src/agentdir/control.py#L1085-L1215)).

Legacy behavior is covered explicitly today: v1 manifests without `briefing`
may cite without strict use enforcement, and immutable cross-session v1
actions remain compatible
([`tests/test_vector_memory.py:608-686`](../../tests/test_vector_memory.py#L608-L686),
[`tests/test_vector_memory.py:689-750`](../../tests/test_vector_memory.py#L689-L750)).
Expansion should add no required manifest field and no new terminal-decision
field, so those fixtures remain valid.

### Regression matrix

The module interface should be the primary test surface. Tests should use real
temporary AgentDir roots and an in-memory delivery adapter, then keep a small
number of CLI rendering tests. This verifies behavior without coupling every
test to resolver implementation details.

| Area | Required regression cases |
| --- | --- |
| CLI grammar | `--expand` is mutually exclusive with show/use/none/skip; repeated selectors batch; current arguments and exit codes remain unchanged |
| Briefing output | Start/show prints a pack-bound expansion command; no command for zero-source packs; existing compact-output limit remains enforced |
| Targeting | Default active latest, explicit session latest, explicit pack, and reopened older pack all select the intended immutable pack even when a newer one exists |
| Selectors | `1`, `s1`, and presented source ID resolve identically; duplicate refs dedupe; zero/out-of-range/omitted IDs fail atomically with no delivery or receipt |
| Local resolution | Message, evidence, and session-summary sources return the canonical body at both depths with correct metadata |
| Federated resolution | Available child root resolves full canonical content; stale/unregistered child falls back visibly; unsafe/root-escaping paths are rejected |
| Bounds/privacy | Relevant/source byte caps, Unicode byte accounting, truncation flag, secret redaction, and no body text in receipt event |
| Delivery | Plain, JSON, and in-memory adapters receive the same structured item fields; JSON excludes the full pack manifest; delivery failure emits no receipt |
| Receipts | One batch event, unique-source count versus event count, repeated expansion, required/auto/none policy, and receipt failure after successful delivery |
| Chronology | Expansion before decision, after used, after no-relevant, and after skipped; only the first contributes to before-decision metrics |
| Terminal state | Expansion never changes decision ID, disposition, reviewed/used/dismissed/pending, finish allowance, or lineage validity |
| Concurrency | Expansion and terminal decision serialize on the pack lock; repeated concurrent expansion has deterministic event/count semantics |
| Historical | Explicit ended pack remains readable; automatic policy does not append into its ended session |
| Malformed events | Unknown pack/source, wrong session, missing headers, and invalid depth surface separate expansion validation without corrupting review state |
| Legacy | No-briefing manifest uses displayed ordinal mapping; old consume/cite and cross-session v1 behavior stays green; expansion remains optional |
| Projections | Audit JSON, plain status, Rich status, final report, and handoff agree on expansion counts and receipt validity |
| Guidance | README, PRD, technical brief, generated AgentDir instructions, packaged skill, and Pi integration describe expansion as optional delivery evidence rather than proof of attention |

Existing tests already protect the most important adjacent contracts:

- persisted numbered show and old-pack command binding
  ([`tests/test_agent_first.py:1474-1551`](../../tests/test_agent_first.py#L1474-L1551));
- low-level complete/partial compatibility and terminal-decision conflicts
  ([`tests/test_agent_first.py:1626-1735`](../../tests/test_agent_first.py#L1626-L1735));
- decision idempotency and selector canonicalization
  ([`tests/test_agent_first.py:1738-1810`](../../tests/test_agent_first.py#L1738-L1810));
- rejection of retrieved-but-not-presented sources
  ([`tests/test_agent_first.py:1813-1881`](../../tests/test_agent_first.py#L1813-L1881));
- federated root-qualified provenance
  ([`tests/test_vector_memory.py:1316-1346`](../../tests/test_vector_memory.py#L1316-L1346)).

### Tradeoffs

This design has more internal structure than a direct `if args.expand` branch.
The cost is a resolver chain, delivery adapters, and receipt policy. The payoff
is depth: one interface can support local, derived, federated, terminal, JSON,
and future MCP/UI reads without multiplying source/privacy/audit logic.

Batching makes partial failure semantics stricter, but it keeps receipts
coherent and reduces audit-write noise. Bounded `source` depth is less powerful
than an unlimited dump, but safer for secrets, terminal usability, and agent
context windows. Optional receipts are weaker than a mandatory read gate, but
they preserve old agents and honestly distinguish “delivered” from “attended.”

Most importantly for the original user concern, product analytics should count
`work context --expand` as a read interaction. Its metadata receipt is audit
instrumentation, not evidence authored into AgentDir. This makes the UX metric
match what the agent actually did.

### Commands and source snapshot

This design lane inspected repository HEAD
`a77a5e3230a31b1395ef1c22c1d5e5489c6c50f7` plus the concurrent investigation
working tree. It made no product-source edits. Representative commands were:

```bash
rg -n "def brief_context_manifest|def _briefing_source_ids|def _context_events|def context_packs|def explain_memory_match" src tests docs
nl -ba src/agentdir/cli.py | sed -n '2060,2120p'
nl -ba src/agentdir/control.py | sed -n '380,610p'
nl -ba src/agentdir/context.py | sed -n '500,900p'
nl -ba src/agentdir/context.py | sed -n '1020,1125p'
nl -ba src/agentdir/context.py | sed -n '1400,1595p'
nl -ba src/agentdir/federation.py | sed -n '430,470p'
nl -ba src/agentdir/memory.py | sed -n '860,955p'
nl -ba tests/test_agent_first.py | sed -n '1440,2025p'
nl -ba tests/test_vector_memory.py | sed -n '580,750p'
/Users/jstxn/Development/agentdir/.venv/bin/python -m agentdir work context --help
```

The help output confirmed that the current command accepts exactly one of
`--show`, `--use`, `--none-relevant`, or `--skip`; there is no first-class
expansion action yet. A replay against the preserved temporary dogfood pack was
also attempted while the source working tree was changing and returned
`Unknown context pack`; because the immutable envelopes remain present and the
earlier dogfood transcript had already completed successfully, no design or
compatibility conclusion in this section relies on that unstable WIP replay.

## Primary Synthesis: Regression-Safe First Release

The follow-up investigation compared three independent interface designs and
ran a second controlled source-CLI experiment. The strongest first release is
a hybrid:

- one additive `work context --expand <number>` action;
- the common-caller design's clean, bounded, pack-bound output;
- the minimal design's explicit integrity and drift states;
- the flexible design's optional, nonterminal, metadata-only receipts;
- no read-before-use finish gate and no larger federated source copy in the
  first release.

This is deliberately narrower than the most ambitious design. It closes the
trust gap while keeping all current decision, compatibility, privacy, and
finish behavior intact.

### Additional controlled findings

The second fixture lives at `/tmp/agentdir-deep-read.dueaXE`. It used the source
CLI at `.venv/bin/agentdir` version `0.8.0`.

#### The current content ladder truncates at every layer

The seeded contract body was 616 characters. The emitted manifest retained a
320-character `excerpt`; `work context --show` rendered 220 characters; and
`memory explain --source` returned 500 characters. The explain result did not
include a truncation flag or the original character count.

This means a wrapper around `memory explain` would improve discoverability but
would not yet establish a complete or candid deep-read interface. Expansion
needs its own `returned_chars`, `source_chars`, `truncated`, and page/range
metadata. The current limits are visible in
[`context.py`](../../src/agentdir/context.py#L1022),
[`control.py`](../../src/agentdir/control.py#L394), and
[`memory.py`](../../src/agentdir/memory.py#L878).

#### A federated source needs a real resolver

The federated pack stored this root-qualified source:

```text
root-b120ba2b7612:message:sessions/prior-contract/Maildir/new/...
```

Running `memory explain --source` for that ID against the controller root
failed with:

```text
agentdir: Unknown memory source: root-b120ba2b7612:message:sessions/...
```

The same request succeeded against the still-registered child root when it
used `source_root_path` plus `source_id_original`. A first-class expansion
module therefore must validate the child against the current root registry and
delegate to it. It must not pass a root-qualified ID to the controller's local
memory lookup or blindly trust an arbitrary path captured in a manifest.

#### Live lookup can drift from the captured pack

An explicit low-level context build included
`session:active-investigation:summary` with captured text hash:

```text
ca48c433c0070b500e934eafc5a31f2ff211306103b48bf88b51c47903a36dff
```

After the pack creation event was indexed, the live derived summary had hash:

```text
9eaaa2c56d7075d661bf1afb8ec5955e83b0830b82ce8304d8dfb0586360c8b7
```

Normal `work start` excludes the current session from prior-memory retrieval,
so this exact immediate drift does not describe its default path. It does prove
the general invariant: an exact source ID is not sufficient to certify that
live derived content is the content represented by the pack. Expansion must
compare the manifest's captured hash with the resolved content and must never
silently substitute changed content.

#### The adjacent compatibility baseline is green

Fourteen focused tests passed across three runs. They covered:

- memory explanation and federated pack provenance;
- used-source decisions, reopening, and exact old-pack targeting;
- legacy citation, legacy cross-session actions, and low-level compatibility;
- malformed manifest handling;
- compact start output, no-context markers, selector canonicalization, and
  rejection of retrieved-but-unpresented sources.

These are the minimum adjacent contracts the expansion implementation must
keep green before adding its own matrix.

### Recommended caller interface

Add exactly one new action in the existing mutually exclusive group:

```text
agentdir work context --pack <pack-id> --expand <number> [--page <number>]
```

Use `expand`, rather than `read`, in persisted and audit language. AgentDir can
prove that it returned a larger representation; it cannot prove model
attention. `--page` is valid only when a returned source is truncated. There
is no unbounded `--all` mode.

The compact briefing adds one generic, pack-bound line rather than one command
per source:

```text
context_expand=agentdir work context --pack ctx-123 --expand <number>
```

The actual executable prefix should come from the current invocation and be
shell-quoted. This prevents the source-versus-installed version skew reproduced
by the first dogfood run.

Suggested plain expansion output:

```text
pack=ctx-123
source=[1] possible decision: Tag slug normalization contract
integrity=verified
extent=full
page=1/1
chars=612/612
truncated=false
receipt=recorded
semantics=content-returned-not-model-attention

Normalize Unicode text to NFKD and drop combining marks.
...

next_use=<current executable> work context --pack ctx-123 --use 1 --reason "<how it helps>"
```

The structured result should separate two dimensions that the flexible design
combined:

- `integrity`: `verified`, `legacy_unverified`, `changed`, or `unavailable`;
- `extent`: `full`, `bounded`, or `stored_excerpt`.

If an immutable message hash changes, fail rather than show the live body as
the captured source. If a derived summary changes, show the stored excerpt and
an explicit drift warning; do not count it as a verified expansion. If a
canonical source is unavailable, a stored excerpt may still be shown, but it
must remain labeled `stored_excerpt`.

### Recommended module and seam

Keep one deep `ContextExpansion` module at the existing context/work seam:

```python
@dataclass(frozen=True)
class ExpansionRequest:
    target: ContextTarget
    selector: str
    page: int = 1

class ContextExpansion:
    def expand(self, request: ExpansionRequest) -> ExpansionOutcome: ...
```

The module returns one structured outcome consumed by the existing plain and
JSON renderers. Do not make renderers or output delivery part of the external
interface in the first release; they already have a shared structured-result
pattern, and a future MCP/UI adapter does not yet justify more caller surface.

The implementation hides:

1. exact pack/session target resolution;
2. ordinal mapping from the actual displayed briefing;
3. local message/evidence lookup by stable identity and hash;
4. derived-summary drift classification;
5. registered federated-root lookup and original-source delegation;
6. clean body extraction, UTF-8-safe paging, redaction, and bounds;
7. decision-state annotation;
8. idempotent expansion-receipt emission.

The source resolvers are real internal adapters because local messages,
derived summaries, federated roots, and the stored-excerpt fallback already
vary. Filesystem, SQLite, Maildir, and registered roots remain
local-substitutable and should be tested in temporary stores.

For legacy manifests without a persisted briefing, expansion ordinals must
come from `brief_context_manifest(...)["sources"]`, the list the caller actually
saw. Do not globally change `_briefing_source_ids()`, because current legacy
review compatibility intentionally falls back to every manifest source.

### Receipt semantics without write inflation

Expansion should emit a small append-only receipt automatically only when the
pack-owning session is still active. Historical expansion remains readable but
reports `receipt=not_recorded`; it does not append into an ended session.

Use an orthogonal event and pack-claim header, for example:

```text
event_type=context.sources.expanded
X-AgentDir-Context-View-Pack-Id: ctx-123
X-AgentDir-Source-Id: message:...
X-AgentDir-Context-View-Id: view-...
```

Do not reuse the decision event's pack header. This lets old audit code ignore
the receipt completely and prevents an optional observation from entering the
current transition-conflict fold.

The receipt contains identifiers, expected/actual hashes, page/range, extent,
truncation, and redaction counts, never expanded content. Its ID is
deterministic over pack, source, content hash, page/range, and representation
version. Repeating the same expansion returns `recorded=false` instead of
adding another physical write.

Receipt events must be excluded from direct memory documents, and their bodies
must be omitted from session-summary key excerpts. Otherwise a feature meant
to improve reading would create self-retrieving telemetry noise. Raw receipts
remain available to audit and replay.

Audit, status, final report, and handoff should project one consistent nested
object:

```json
{
  "expansion": {
    "expanded_source_count": 1,
    "expanded_before_decision_count": 1,
    "expanded_after_decision_count": 0,
    "used_without_prior_expansion_count": 0,
    "receipt_event_count": 1
  }
}
```

The physical event is a read receipt, not authored knowledge. Product metrics
must classify it as a read interaction. Malformed optional receipts may surface
`expansion_validation_errors`, but they must not change the terminal decision,
`finish_allowed`, or `lineage_valid` in the first release.

### What should not change in the first release

- Do not require expansion before `--use` or `work finish`.
- Do not require expansion before a reasoned `--none-relevant` decision.
- Do not copy larger federated source bodies into controller manifests; the
  current contract intentionally keeps the child root canonical.
- Do not change context-pack or briefing protocol versions for additive fields
  and an orthogonal receipt event.
- Do not rename or remove existing JSON audit fields.
- Do not make a late expansion retroactively look as if it informed an earlier
  terminal decision.

This preserves old agents and low-level callers while making the new behavior
observable. If real usage later shows that agents routinely declare a
truncated source used without expanding it, AgentDir can add a configurable
policy or warning in a later release. It should not begin with a mandatory gate
before there is field evidence about false positives and workflow cost.

### Recommended implementation order

1. Add the `ContextExpansion` module, exact resolver/integrity states, bounded
   output, and focused module-interface tests.
2. Add the CLI action, current-invocation command rendering, and decision-aware
   `--show` output.
3. Add idempotent metadata receipts plus audit/status/handoff projections and
   memory-noise exclusions.
4. Update README, PRD, technical brief, generated guidance, and packaged skill
   in the same release as the CLI action.
5. Dogfood the release across local, legacy, and federated fixtures before
   considering any enforcement policy.

The flexible section's regression matrix remains the comprehensive acceptance
list. The implementation should additionally assert the exact experimental
failures above: 616/320/220/500 truncation honesty, root-qualified resolution,
derived-summary hash drift, idempotent receipt writes, and zero receipt-memory
self-retrieval.

## Implementation dogfood: the read-before-use loop

After implementing the expansion seam, I repeated the experiment in an
isolated Git repository at
`/private/tmp/agentdir-context-expansion-dogfood.sTHTUz/repo`. The commands used
the current source checkout through
`/Users/jstxn/Development/agentdir/.venv/bin/python -m agentdir`, not the older
globally installed executable.

The fixture contained a deliberately shallow buggy `slugify()` function, a
failing table-driven test, and an ended prior session whose useful instruction
was beyond the compact briefing excerpt. The new task was:

```text
fix repeated separator slug normalization using prior regression guidance
```

### What the agent saw at start

The start response showed two numbered sources and copyable commands bound to
both the source invocation and the immutable pack:

```text
context_match=no_strong_prior_context
context_presented=2
[1] possible retrieval_hint: slug normalization incident resolution
    preview: ... Historical slug normalization incident. The production...
[2] possible retrieval_hint: session completed
    preview: ... session_id=prior-slug-incident status=completed ...

context_expand=/Users/jstxn/Development/agentdir/.venv/bin/python -m agentdir work context --root /private/tmp/agentdir-context-expansion-dogfood.sTHTUz/repo/.agentdir --pack ctx-20260819T221654Z-200c2dd20ad6 --expand <number>
context_use=/Users/jstxn/Development/agentdir/.venv/bin/python -m agentdir work context --root /private/tmp/agentdir-context-expansion-dogfood.sTHTUz/repo/.agentdir --pack ctx-20260819T221654Z-200c2dd20ad6 --use <number> --reason "<how it helps>"
```

I copied the expansion command, substituted `1`, changed the working directory
to `/private/tmp`, and ran it unchanged otherwise. The absolute `--root` kept
the command correctly scoped.

### What the deep read returned

The response clearly separated provenance and semantics from the clean source
body:

```text
context_source=1
source_class=retrieval_hint
match_quality=possible
subject=slug normalization incident resolution
integrity=verified
basis=canonical_envelope
extent=full
page=1/1
bytes=0:877/877
chars=877/877
truncated=false
decision_phase=before_decision
semantics=content-returned-not-model-attention
source_event=agent.message
source_session=prior-slug-incident

--- source content ---
...
DEEP_SENTINEL: use `re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")`
and keep the tests table-driven.
--- end source content ---

receipt=recorded
receipt_reason=active_session_receipt
```

This was the key behavioral change: the 877-character canonical source was
available through the obvious next action, while the start response remained
compact. `integrity=verified`, the exact byte/character coverage, and explicit
attention caveat made the output honest about what AgentDir could prove.

Only after that delivery did I record `--use 1`, apply the prior regex pattern,
and run the isolated test through AgentDir. It passed:

```text
.                                                                        [100%]
1 passed in 0.00s
```

### What the user saw afterward

The default Rich status now makes the read funnel visible without opening raw
events:

```text
context  retrieved                    3
context  presented                    2
context  reviewed                     2
context  used                         1
context  dismissed                    1
context  pending                      0
context  expanded                     1
context  expanded_before_decision     1
context  expanded_after_decision      0
context  used_without_prior_expansion 0
context  expansion_receipts           1
context  expansion_receipts_valid     true
health   doctor_ok                    true
```

The strict session audit also reported
`1 context expansion receipt(s) validated`, and the final handoff kept the same
counters with `context_lineage.ok=true` and overall `status=ok`. The receipt
was metadata-only and did not enter searchable memory.

### User-perspective assessment

The read path is now obvious enough to follow without knowing AgentDir's
lower-level memory commands. It tells the agent what can be opened, prints one
copyable exact-target command, returns a bounded clean representation, and
then shows the user whether the read happened before or after the decision.
Crucially, it says that content delivery is not proof of model attention.

One retrieval-quality issue remains outside this expansion seam: this highly
relevant fixture was still labelled `possible` and the terminal
`session completed` record occupied the second of two briefing slots. Expansion
fixes the missing deep-read affordance, but it does not by itself improve
ranking calibration or briefing diversity. Those should remain a separate,
evidence-driven follow-up rather than being hidden inside the delivery module.
