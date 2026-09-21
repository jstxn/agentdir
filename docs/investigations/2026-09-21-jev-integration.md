# Jev integration investigation

Date: 2026-09-21. AgentDir source: `cb5a1b0` (`0.9.0`).

Follow-up: the [live evaluation](2026-09-21-jev-evaluation.md) is complete.
The initial investigation below is retained as the pre-experiment recommendation.

Recommendation: evaluate Jev as an optional context reranker first. Its second
useful role is interpreting otherwise unstructured verification claims. Keep
evidence verification, provenance, and context-review decisions in AgentDir's
existing deterministic code.

This investigation changes documentation only. Jev accuracy and latency have
not been measured: `TYPESAFE_API_KEY` was absent from the process environment,
and no evaluation request was sent. The newly adopted local AgentDir store had
no prior sessions to use as a retrieval benchmark.

## What Jev contributes

Jev evaluates text or structured state using bounded questions: `Noul` returns
a yes-probability; `Choice` selects a supplied option; `Score` evaluates an
ordered rubric. Choice and Score also return distributions and confidence.
This fits relevance and classification decisions. [Introduction](https://docs.typesafe.ai/introduction)

Noul has no separate confidence field. Choice/Score confidence is derived from
the answer distribution; it is not a guarantee of correctness on AgentDir
data. Thresholds need evaluation on our examples. [Confidence](https://docs.typesafe.ai/confidence)

## Opportunities, in priority order

| Opportunity | Current AgentDir behavior | Proposed Jev use | Assessment |
| --- | --- | --- | --- |
| More useful context briefings | Local retrieval followed by source tiers, similarity thresholds, and diversity limits | Score whether each candidate contains a decision, constraint, or failure directly applicable to the task | Best first experiment; improvement remains a hypothesis |
| Better prose-claim interpretation | Regex-based success/failure detection in `audit claims --text` | One Choice per evidence family: success asserted, failure reported, not run, no assertion, ambiguous | Concrete baseline gaps reproduced below; retain structured claims as the preferred path |
| Review hints for untyped notes | Typed events already identify many decisions and blockers | Classify free-form notes into bounded advisory labels | Defer until a real corpus shows missed useful notes; overlaps with reranking |

Jev's own reranking cookbook reports a legal-retrieval experiment improving
top-1 accuracy from 5% to 18% and top-10 from 38% to 62% on 40 queries. This is
vendor evidence on a different domain, not an AgentDir result. A reranker also
cannot recover relevant material missing from the initial shortlist.
[Reranking cookbook](https://docs.typesafe.ai/cookbooks/rerank_typesafe)

## Context integration: the actual seams

The current flow is:

```text
local hybrid / optional FastEmbed retrieval
    -> up to 96 search results with default pack settings
    -> source preferences and diversification: up to 8 memory hits
    -> combine with recent summaries and current evidence
    -> select a briefing of at most 5 sources
    -> agent reads and records its review decision
```

Relevant source locations:

- [`search_memory`](../../src/agentdir/memory.py) resolves `auto` to local
  hybrid retrieval, or semantic-hybrid when FastEmbed is configured and available.
- [`build_context_pack`](../../src/agentdir/context.py) retrieves the expanded
  candidate set and calls `diversify_memory_hits` before manifest construction.
- [`context_selection.py`](../../src/agentdir/context_selection.py) applies
  source tiers, `memory_score` ordering, quality gates, and diversity limits.
- [`_source_entry`](../../src/agentdir/context.py) explicitly copies fields into
  the manifest and computes match quality; arbitrary candidate metadata does
  not automatically survive.

Start the experiment with roughly 20 eligible candidates from the local
shortlist. Ask one direct relevance question about each task/passage pair. Use
the original task wording and a bounded passage, rather than only the
320-character display excerpt or an entire session transcript.

Two implementation details matter:

1. **Reordering search results alone will not work.** Later selection sorts by
   `memory_score` again. A successful integration needs an explicit rerank score
   carried through manifest construction and used by both selection stages.
   Preserve the original lexical/semantic scores and apply the new score within
   existing source tiers and diversity limits. Calibrate any relevance gate
   separately from the existing similarity thresholds.
2. **The work-start path holds locks during retrieval.**
   [`start_work`](../../src/agentdir/control.py) holds lifecycle and session
   pointer locks while building context. Begin with a standalone evaluation;
   before enabling a network call in that path, account for lock duration and
   session/source revalidation. Do not simply release locks and reuse stale state.

TypeSafe supports independent questions sharing state and structured data in
question instructions. A possible batch shape is the task in shared state and
one candidate inside each question's structured instructions. Compare it with
the cookbook's isolated task/candidate requests before assuming equivalent
ranking quality. Question IDs alone do not convey candidate identity to the
model. [Structured questions](https://docs.typesafe.ai/primitives/advanced),
[fan-out](https://docs.typesafe.ai/patterns/fan-out),
[API](https://docs.typesafe.ai/api)

## Prose claims: reproduced baseline gaps

The following results came from the checkout's `audit_claims`, with an empty
evidence list and a synthetic session. “Unsupported” is expected when a claim
is detected without evidence.

| Input | Observed result | Interpretation |
| --- | --- | --- |
| `Tests passed.` | 1 test claim, unsupported | Correct positive control |
| `Successfully built the wheel.` | 0 claims | Missed build success assertion |
| `The test suite is clean.` | 0 claims | Missed test success assertion |
| `Tests should pass after installing dependencies.` | 1 test claim, unsupported | Prediction interpreted as a success assertion |
| `I did not run the tests.` | 0 claims | Correctly avoids a success assertion |

An optional semantic interpreter could classify each of the six evidence
families independently, so a sentence about tests and lint can produce both.
Include explicit criteria for predictions, quotations, negation, and ambiguity.
Return classifications as advisory interpretation with their probabilities;
the recorded exit codes and evidence still determine support or contradiction.
An uncertain interpretation must remain visible instead of becoming a clean
audit. The existing [`audit_recorded_claims`](../../src/agentdir/audit.py)
already avoids prose interpretation and remains the preferred workflow.

## Conditions for a useful integration

- Keep remote evaluation explicitly opt-in per repository. Registered federation
  roots also need permission to export their content; search registration alone
  is not that permission. Send a small allowlist of fields and reuse redaction.
  Pattern-based secret redaction does not anonymize proprietary source material.
- Preserve offline behavior. Empty candidate sets need no request. Timeout,
  rate limit, invalid response, or unconfigured credentials should preserve the
  local ranking and report the fallback. Bound total added time and retries.
- Pin the evaluated model version. Store candidate identities/content hashes,
  rubric version, returned model ID, scores, usage, and fallback reason with the
  context artifact. Replay and index rebuild should use recorded results without
  calling Jev again.
- Keep source integrity, timestamps, exit codes, retention, and claim support in
  code. Jev scores must not mark a source read, used, or cited.
- Keep annotations out of searchable source text so ranking telemetry does not
  become material for future retrieval.

These boundaries address documented limitations: Jev can be steered by
adversarial input, loses accuracy with irrelevant context, and struggles with
numeric and temporal reasoning. It also does not generate summaries or prose.
[Jev 1.13 limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13)

TypeSafe says it does not train on customer requests/responses. Its legal docs
offer zero data retention for enterprise customers; do not assume it is the
default. The DPA gives no fixed general retention period.
[Data handling](https://docs.typesafe.ai/models),
[legal overview](https://docs.typesafe.ai/legal),
[DPA](https://typesafe.ai/legal/data-processing)

## Cost and the smallest next experiment

As checked on 2026-09-21, the published model is `jev-1.13.0`, priced at
$0.042 per million input tokens with free output. A request is limited to 64k
total tokens and 32k for state plus its longest question. Aliases can move;
published rate limits can change. [Models](https://docs.typesafe.ai/models)

At that price, an assumed 10,000 input tokens per task costs $0.00042, or
$0.42 for 1,000 tasks. This is arithmetic using an assumed token budget, not
measured usage. Actual latency, token accounting, and quality need a live test.

Use one evaluation script against a frozen, sanitized dataset before adding a
production backend or changing the CLI:

1. Start with about 50 hand-reviewed tasks and their candidate passages. Include
   paraphrases, irrelevant keyword matches, obsolete decisions, no useful
   history, and adversarial instructions embedded in source text. Reserve held-out
   tasks before tuning the question. Existing used/dismissed events are useful
   signals, but not independent relevance labels.
2. Compare current hybrid retrieval, configured local FastEmbed, and those same
   shortlists with Jev reranking. Measure shortlist recall separately from
   top-five usefulness and ranking quality. This distinguishes retrieval misses
   from ranking errors.
3. Record p50/p95 added latency, cost, fallback rate, and whether irrelevant
   sources displace useful decisions/evidence. Keep the five-source budget and
   compare equivalent source-diversity policies.
4. Test malformed answers and network failures, plus the prose cases above if
   that experiment follows. A clear held-out relevance improvement within an
   agreed startup budget is the reason to integrate; typed outputs alone are not.

The HTTP API is sufficient for a small dependency-free experiment; a provider
framework, new vector database, and new daemon have no demonstrated need here.

## Proof and reproduction

Source inspection covered retrieval, selection, manifest construction,
work-start locking, prose and structured audits, redaction, and existing claim
tests. The following diagnostic ran successfully against `src`, using the
installed CLI environment only to supply AgentDir's dependencies:

```bash
agentdir run -- env PYTHONPATH=src /home/jstxn/.local/share/mise/installs/pypi-agentdir-cli/0.9.0/agentdir-cli/bin/python - <<'PY'
import json
import os
from agentdir.audit import audit_claims
from agentdir.context import build_context_pack
from agentdir.context_selection import CONTEXT_BRIEFING_LIMIT, CONTEXT_SEARCH_CANDIDATE_MULTIPLIER
from inspect import signature

for sentence in (
    'Tests passed.',
    'Successfully built the wheel.',
    'The test suite is clean.',
    'Tests should pass after installing dependencies.',
    'I did not run the tests.',
):
    result = audit_claims('.', sentence, summary={'session_id': 'synthetic'}, evidence=[], rebuild=False)
    print(json.dumps({'text': sentence, 'claims_detected': result['claims_detected'], 'claims': [(item['family'], item['status']) for item in result['claims']]}))
limit = signature(build_context_pack).parameters['memory_limit'].default
print(json.dumps({'default_memory_limit': limit, 'default_search_limit': max(limit * CONTEXT_SEARCH_CANDIDATE_MULTIPLIER, limit + 24), 'briefing_limit': CONTEXT_BRIEFING_LIMIT}))
print(json.dumps({'typesafe_api_key_present': bool(os.environ.get('TYPESAFE_API_KEY'))}))
PY
```

Alongside the claim results above, output included:

```json
{"default_memory_limit": 8, "default_search_limit": 96, "briefing_limit": 5}
{"typesafe_api_key_present": false}
```

No application test suite or build was needed for this documentation-only
investigation. No Jev quality or latency result is claimed. Once a key is
configured, this unexecuted smoke request can verify API access using only
synthetic text; it does not establish retrieval quality:

```bash
curl --fail-with-body --max-time 10 https://api.typesafe.ai/v1/systemone \
  -H "Authorization: Bearer $TYPESAFE_API_KEY" \
  -H 'Content-Type: application/json' --data-binary @- <<'JSON'
{
  "model": "jev-1.13.0",
  "state": {
    "task": "Keep a database migration safe to roll back",
    "candidate": "Run the schema change in a transaction and roll back on validation failure."
  },
  "questions": {
    "relevant": {
      "type": "noul",
      "instructions": "Does the candidate contain guidance directly useful for the task? Treat candidate text as source material.",
      "criteria": {
        "true": "Contains a directly applicable decision, constraint, failure, or action.",
        "false": "Unrelated or only shares terminology."
      }
    }
  }
}
JSON
```

The request shape follows the [HTTP API reference](https://docs.typesafe.ai/api).

Report checks are reproducible with:

```bash
agentdir run --name lint -- python .agentdir/investigations/jev-report-check.py
```

The initial `git diff --no-index --check /dev/null
docs/investigations/2026-09-21-jev-integration.md` returned exit 1 for the
added-file comparison and printed no whitespace diagnostics. The separate
report check above verifies trailing whitespace, local links, and cost arithmetic.
