# Jev context-selection evaluation

Date: 2026-09-21. AgentDir source: `cb5a1b001f03bbb3b2e302d43acbbd74f95d65ee`.

This follows the [integration investigation](2026-09-21-jev-integration.md).
The experiment uses live Jev responses and AgentDir's actual local retrieval
and selection functions. It adds an evaluation script and fixture, without
enabling a remote backend in normal AgentDir commands.

Recommendation: proceed with an opt-in relevance-filtering prototype that scores
candidates before diversification. Jev substantially improved this synthetic
benchmark, especially when its relevance gate could omit weak material. A
stricter existing local gate recovered some of that improvement at no API cost,
but did not match Jev here. Real session history is the next validation target;
these results do not justify making a remote model the default.

## Method

- [Fixture](../../scripts/fixtures/jev_retrieval.json): 50 synthetic tasks and
  70 synthetic historical passages, with relevance labels fixed before live
  scoring. These are not human annotations or real recorded development tasks.
- Four topic groups supply 20 development queries; six different groups supply
  30 held-out queries. The held-out set contains 24 answerable tasks and six
  tasks with no labeled useful source.
- Each topic includes literal and paraphrased requests, an implementation
  constraint, a failure reproduction, and a no-context request. Distractors
  include keyword-heavy placeholders, decorative notes, and injected scoring
  instructions. Relevant warnings in superseded decisions receive partial credit.
- Grades are 2 for directly useful and 1 for supporting detail. Labels are never
  included in API requests. The same rubric and 0.7 cutoff are used throughout;
  neither was tuned on the resulting scores.
- The local baselines are AgentDir hybrid retrieval and semantic-hybrid using
  FastEmbed `0.8.0`, `BAAI/bge-small-en-v1.5`, and Python `3.12.12`.
- The model is pinned to `jev-1.13.0`. Requests use one Noul per candidate, with
  a shared task and explicitly addressed candidate passages. Only synthetic
  fixture text is sent. The API key is read in-process from the supplied dotenv
  file and is excluded from logs, result files, and command arguments.

The corrected pipeline retrieves up to 96 hits, excludes derived summaries
without independent labels, and takes the first 20 canonical passages in
retrieval order for Jev scoring. The existing source tiers, session-diversity
rules, eight-hit memory limit, and five-source briefing limit still apply after
scoring. A fresh isolated store is used; the real repository's memory settings
are unchanged. Fixed message IDs and insertion order make the local inputs
repeatable.

Five variants are recorded for each retrieval engine:

1. Current selection over all retrieved canonical passages.
2. Current selection over only the same 20-candidate shortlist.
3. Jev ordering over that shortlist, preserving existing quality gates.
4. Jev ordering plus relevance filtering: require probability at least 0.7 and
   treat surviving passages as eligible for the briefing. This replaces the
   experiment's similarity-based eligibility gate, not evidence verification.
5. A cheaper control that keeps only sources already rated `strong` by AgentDir.
   This uses existing local thresholds and makes no API call.

The fourth variant changes both ordering and eligibility; its improvement must
not be attributed solely to reranking. Probabilities are applied only to
in-memory copies inside the evaluation script. Production scores, raw events,
and context-review decisions are never modified.

## Results

Results below cover the 30 held-out tasks. nDCG@5 measures how well the first
five selected sources match graded relevance, with 1.0 ideal; it is averaged
over the 24 answerable tasks. Recall is the average fraction of labeled useful
sources selected on those tasks. Precision is useful selections divided by all
selections, including no-context tasks. Counts treat an unlabeled source as
irrelevant under the frozen rubric.

| Retrieval and selection | nDCG@5 | Direct answer first | Useful-source recall | Selection precision | Irrelevant selections | Correct empty briefing |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Hybrid, current | 0.679 | 75.0% | 61.1% | 32.4% | 69 | 0/6 |
| Hybrid, local strong-only | 0.000 | 0.0% | 0.0% | N/A | 0 | 6/6 |
| Hybrid, Jev ordering only | 0.754 | 95.8% | 62.5% | 34.3% | 65 | 0/6 |
| Hybrid, Jev ordering + filtering | 0.930 | 100.0% | 84.7% | 98.0% | 1 | 6/6 |
| FastEmbed hybrid, current | 0.747 | 79.2% | 75.0% | 28.7% | 107 | 0/6 |
| FastEmbed hybrid, local strong-only | 0.829 | 79.2% | 79.2% | 64.8% | 25 | 6/6 |
| FastEmbed hybrid, Jev ordering only | 0.778 | 87.5% | 75.0% | 28.7% | 107 | 0/6 |
| FastEmbed hybrid, Jev ordering + filtering | 0.969 | 100.0% | 98.6% | 85.7% | 10 | 5/6 |

The full-pool and 20-candidate local baselines produced identical selections
on this fixture. The hybrid shortlist contained an average 84.7% of labeled
useful sources on held-out answerable tasks; the FastEmbed shortlist contained
100%. Jev retained all labeled useful sources available to hybrid retrieval,
but could not recover its retrieval misses. The local strong-only hybrid
control returned nothing for every task, illustrating why local similarity
thresholds cannot be substituted for calibrated relevance probabilities.

Ordering alone helped place direct answers first but left most irrelevant
material in the briefing. Filtering accounts for much of the additional gain.
The existing selector intentionally permits weak exploratory context; counting
that context as irrelevant here evaluates a stricter selection policy and is
not evidence that the current behavior violates its contract.

The FastEmbed/Jev combination traded more complete coverage for more extras.
Its false positive on the supply-chain-attestation task selected three ordinary
release-management sources at probabilities 0.73-0.78, although none described
cryptographic attestations. Other extras concerned related secret-cleanup
procedures and release validation. One relevant historical warning scored 0.68
and was omitted. Hybrid/Jev's one unlabeled selection was subprocess timeout
advice returned for an output-capture task at 0.71. These are concrete reasons
to keep the scores advisory and validate the cutoff on real data.

No injected scoring-instruction passage was selected by either filtered Jev
variant across the 50 queries. On 50 batch/pair comparisons from the two fixed
development queries, mean absolute probability difference was 0.0582, with one
disagreement at the 0.7 cutoff. A relevant warning scored 0.70 in a batch and
0.63 alone. Batching is promising but not score-equivalent to isolated requests.

Observed API latency for the corrected 100 query/engine evaluations was
**283 ms median and 425 ms p95**. The final offline replay measured warm local
retrieval/selection at 4.9/11.2 ms median/p95 for hybrid and 31.1/36.1 ms for
FastEmbed hybrid. These are local-process measurements, not a service SLA.

The corrected comparison used 141 distinct request payloads, including pair
checks, with 303,562 reported input tokens: approximately **$0.01275** at the
published $0.042 per million input tokens. Of these, 108 requests were new and
33 reused prior identical responses. Across both live runs and the smoke test,
there were **250 API requests, zero observed API errors, 594,982 input tokens,
and about $0.02499 total estimated cost**. The final local-filter comparison
and replay made **zero new API requests**. Cost is calculated from reported
usage and published rates, not a billing statement.

## Limits and interpretation

This is a small, constructed benchmark. Its repeated scenario structure,
short passages, imperfect relevance labels, and lack of real session history
limit what the scores establish. Useful related material can be missing from
the labels; precision here means agreement with those labels. Testing ten
injected passages does not establish general prompt-injection resistance.

The fixture deliberately excludes unlabeled derived summaries and does not
exercise concurrent `work start`, production network integration, long or
truncated session bodies, or private/federated-root export policies. Warm local
latency comes from a persistent evaluation process, not fresh CLI startup.
Jev timing includes the stdlib HTTP request and response validation. Cached
responses retain their original measured timings; a cache hit is not counted
as a new API call.

The first exploratory run incorrectly applied diversification when constructing
the shortlist. That discarded useful same-session passages before Jev could
score them. The reported comparison uses the corrected order: retrieve,
shortlist, score, then diversify. The first run remains under
`.agentdir/evals/jev/results/`; corrected results are under
`.agentdir/evals/jev/results-v2/`. The final offline replay, including the added
local strong-only control, is under `.agentdir/evals/jev/results-v3/`.
Requests with identical content reuse their
saved responses. The rubric and fixture labels did not change during this
correction. Held-out queries were seen in that first run, so the corrected run
is not a pristine blind evaluation.

## Proof and reproduction

The dependency installation, live evaluation, and self-check ran through the
machine-wide queue. AgentDir recorded the evaluation output and verification.
The runner and fixture are the only executable additions; normal AgentDir
commands do not call Jev.

```bash
# Create the isolated environment if it does not already exist.
~/.config/agents-sync/bin/agent-queue bash -c 'uv venv --python 3.12 .agentdir/evals/jev/venv && uv pip install --python .agentdir/evals/jev/venv/bin/python "fastembed>=0.4" "platformdirs>=4.2" "rich>=13.7"'

# Local checks; no API calls.
~/.config/agents-sync/bin/agent-queue agentdir run --name test -- \
  .agentdir/evals/jev/venv/bin/python scripts/eval_jev.py --self-check

# Live calls only for requests absent from the content-addressed response cache.
~/.config/agents-sync/bin/agent-queue agentdir run -- \
  .agentdir/evals/jev/venv/bin/python scripts/eval_jev.py --live \
  --env-file /home/jstxn/Development/.env --output .agentdir/evals/jev/results-v2

# Offline replay and local strong-only control; uses already saved responses.
~/.config/agents-sync/bin/agent-queue bash -c 'agentdir run --name test -- .agentdir/evals/jev/venv/bin/python scripts/eval_jev.py --self-check && agentdir run -- .agentdir/evals/jev/venv/bin/python scripts/eval_jev.py --output .agentdir/evals/jev/results-v3'
```

The final output directory was seeded with the earlier content-addressed
response files before offline replay. To reproduce from a fresh checkout,
create the environment and use `--live` with an empty output directory instead.
The recorded run used FastEmbed `0.8.0`; pin that version when comparing future
runs against these numbers.

The self-check exercises invalid or missing answers, non-finite and out-of-range
probabilities, ranking arithmetic, selection thresholds, preserving original
scores, timeout/429 fallback, and counting cached requests only once. Observed:

```text
Self-check passed: response validation, ranking metrics, threshold, immutable inputs, timeout and 429 fallback, cached billing counted once.
```

Result files contain the protocol and hashes, per-query candidate IDs and
selections, individual responses and usage, aggregate metrics, and batch/pair
comparisons. They remain in the ignored AgentDir store. No commit or push was
performed. A full application test suite was not run because production code
was unchanged; the changed script was exercised directly and with its self-check.

Final fixture SHA-256:
`d4ee208fa45d9bc3cbe19a94b894d805e6f2053463155f2181ab4d6d4348d70a`.
Final evaluator SHA-256:
`229174eee65383e1b7afacc6fc16351f1c35e028ad9b2dc5e5632b4cec33a663`.

API shape and published pricing: [TypeSafe API](https://docs.typesafe.ai/api),
[models](https://docs.typesafe.ai/models).
