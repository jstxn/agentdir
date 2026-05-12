# AgentDir RAG And Vector Memory Investigation

Date: 2026-05-12

Implementation status:

- Stage 1 hybrid passage retrieval is implemented in this branch with
  rebuildable `memory_passages` and `memory_terms` tables.
- Stage 2 context protocol V1 is implemented: `context build --emit`,
  `context consume`, `context cite`, and `audit context`.
- Stage 3 memory federation V1 is implemented with explicit root registration,
  root-qualified source IDs, federated memory search, and federated context
  packs.
- Stage 4 optional vector extras have a discoverable status surface through
  `memory backend list`; real external backends remain unconfigured and
  dependency-free by default.

## Executive Decision

AgentDir should not become a conventional RAG product with a required vector
database. The strongest path is more specific and more aligned with AgentDir:

1. Keep the default memory layer zero-dependency, deterministic, and rebuilt
   from raw envelopes.
2. Upgrade the current memory implementation into a hybrid passage retriever
   inside the SQLite sidecar.
3. Add a first-class context protocol that records retrieved, consumed, cited,
   and handed-off sources as append-only AgentDir events.
4. Add a federated root index for cross-repo and machine-level memory, but keep
   it derived and opt-in.
5. Offer real embeddings and vector databases only as optional accelerators,
   never as required install or source of truth.

This makes AgentDir more ambitious than "RAG-lite" without weakening the core
contract: raw envelopes are canonical, indexes are disposable, evidence is
separate from retrieval, and agents must re-verify before making claims.

## Method

This investigation used five isolated detached worktrees:

| Lane | Worktree | Focus |
| --- | --- | --- |
| `raglite-core` | `/Users/justen/Development/research/agentdir-investigation-worktrees/raglite-core` | Zero-dependency retrieval architecture |
| `vector-db` | `/Users/justen/Development/research/agentdir-investigation-worktrees/vector-db` | External and embedded vector DB options |
| `machine-memory` | `/Users/justen/Development/research/agentdir-investigation-worktrees/machine-memory` | Machine-level and cross-repo memory |
| `agent-protocol` | `/Users/justen/Development/research/agentdir-investigation-worktrees/agent-protocol` | Context consumption, citation, and handoff protocol |
| `regression-lab` | `/Users/justen/Development/research/agentdir-investigation-worktrees/regression-lab` | Regression and release gates |

Each worktree had an isolated Python virtual environment. The worktrees were
created from commit `5fd6127b714e53360e3a861803b905284fb5248d`.

The main checkout also ran scale probes against the current implementation:

| Corpus | Memory docs | Rebuild time | Unfiltered semantic search time |
| --- | ---: | ---: | ---: |
| 2,000 messages, 100 session summaries | 2,100 | 2.696s | 0.105s |
| 10,000 messages, 400 session summaries | 10,400 | 14.627s | 0.565s |

These numbers support the current simple path for small and medium local stores,
but they also expose the likely future bottleneck: `search_memory()` currently
scores every SQL-filtered memory row in Python.

## Current State

AgentDir already has a rebuildable local memory layer:

- `memory_documents` stores message and session-summary memory rows in SQLite.
- `vectorize()` creates deterministic sparse hashed vectors from tokens,
  trigrams, and adjacent-token bigrams.
- `search_memory()` filters in SQL, then deserializes vectors and computes
  cosine similarity in Python.
- `context build` combines memory hits, current-session evidence, and recent
  summaries.

The current design is valuable, but it is not semantic RAG in the embedding
model sense. It is deterministic lexical similarity with a vector-shaped sparse
representation. That distinction should remain explicit in docs and CLI output.

## Architecture Contract

Any RAG-like evolution must keep these rules:

- Raw Maildir-style envelopes remain canonical.
- SQLite and any vector index remain derived and rebuildable.
- Retrieval results are hints, not proof.
- Evidence rows, artifact hashes, and fresh verification support claims.
- Retention remains explicit only.
- Project scope stays the default daily workflow.
- User, global, machine, and federated memory are opt-in scopes.
- No required daemon, hosted service, embedding API, or vector database.
- No hard enforcement claim that an agent actually read retrieved context.

## Scenario Matrix

| Scenario | Verdict | Why |
| --- | --- | --- |
| Current SQLite deterministic vectors | Keep as baseline | Lowest install risk and fully rebuildable |
| Hybrid passage retriever in SQLite | Build next | Best default improvement without new deps |
| FTS5 lexical shortlist | Optional acceleration | Useful when available, but not guaranteed everywhere |
| `sqlite-vec` plus local embeddings | Optional extra | Strongest embedded vector path, but still pre-v1 and dependency-bearing |
| LanceDB | Optional experiment | Good embedded story, but a second store and broader stack |
| Chroma | Optional only | Useful local client/server UX, but more operational surface |
| Qdrant local/server | Optional large-store lane | Strong service-backed engine, too heavy for default |
| DuckDB `vss` | Avoid as default | Its docs flag HNSW persistence as experimental |
| `sqlite-vss` | Avoid | Its own repo points users toward `sqlite-vec` instead |
| FAISS standalone | Avoid as product store | It is an index library, not AgentDir-compatible storage by itself |
| Single machine-global canonical store | Reject | Violates scope ownership and widens privacy blast radius |
| Spotlight-first or OS-index truth | Reject | Useful for discovery hints only, not portable truth |
| Machine watcher daemon | Later opt-in | Good warm-index accelerator, risky if required |
| Federated root index | Build after core retrieval | Ambitious cross-repo memory while preserving per-root truth |
| Context consumption/citation protocol | Build early | Makes retrieval auditable without overclaiming enforcement |

## Recommended Product Shape

Use these product terms:

- `Context Retrieval`: the default zero-dependency retrieval layer.
- `Context Pack`: a bounded task briefing with source manifest.
- `Context Audit`: retrieved, consumed, cited, and evidence-backed lineage.
- `Memory Federation`: opt-in cross-root retrieval.
- `Vector Extras`: optional embeddings and vector stores.

Avoid leading with `RAG` in the core product surface. It invites expectations
about generation, embeddings, and vector DBs that AgentDir should not make true
by default.

## Stage 1: Hybrid Passage Retrieval

Add derived retrieval tables during `index rebuild`:

```sql
memory_passages(
  id integer primary key,
  source_kind text not null,
  source_id text not null,
  message_rowid integer,
  session_id text,
  event_type text,
  tool text,
  git_head text,
  workspace text,
  date_utc text,
  ordinal integer not null,
  body_text text not null,
  token_count integer not null,
  vector_json text not null,
  text_sha256 text not null
);

memory_terms(
  term text not null,
  passage_id integer not null,
  tf integer not null,
  field_mask integer not null,
  primary key(term, passage_id)
);
```

Query flow:

1. Apply existing filters first: session, event type, actor, task, tool, git
   head, workspace, and time.
2. Shortlist candidate passages through `memory_terms` or FTS5 when available.
3. Rerank the shortlist with the existing hashed-vector cosine.
4. Collapse passages back to source-level results.
5. Diversify context packs by session and source class.
6. Fall back to the current full-scan path when the shortlist is too small.

Why this is the default path:

- It fixes long-message retrieval without changing canonical storage.
- It avoids a new dependency.
- It uses SQLite tables that can be deleted and rebuilt.
- It improves explainability by showing lexical score, vector score, and chosen
  passage.
- It gives AgentDir room to add optional real vector backends later behind the
  same retrieval interface.

## Stage 2: Context Protocol

`context build` should be able to emit an auditable protocol object.

New commands:

```text
agentdir context build <task> --emit --json
agentdir context consume --pack <pack-id> --source <source-id>... --purpose plan|tool|answer|handoff
agentdir context cite --pack <pack-id> [--source <source-id>] --format json|md
agentdir audit context --pack <pack-id>
```

New event types:

- `context.pack.created`
- `context.pack.consumed`
- `context.sources.cited`
- `handoff.prepared`
- `handoff.accepted`

Key headers:

```text
X-AgentDir-Protocol: agentdir.context-pack.v1
X-AgentDir-Pack-Id: <pack-id>
X-AgentDir-Context-Query: <query>
X-AgentDir-Context-Scope: project|user|global|machine|federated
X-AgentDir-Source-Id: <source-id>
X-AgentDir-Consumption-Purpose: plan|tool|answer|handoff
X-AgentDir-Enforcement-Mode: advisory
```

`X-AgentDir-Enforcement-Mode: advisory` is important. AgentDir can record that a
cooperative agent requested, consumed, and cited sources. It cannot prove that a
hostile or non-integrated model read them.

## Stage 3: Federated Memory

The machine-level opportunity is not one global canonical store. It is a
federated derived index over registered roots.

New commands:

```text
agentdir roots register [--root <root>] [--name <name>] [--visibility private|team|machine]
agentdir roots list
agentdir roots remove <root-id>
agentdir index federated rebuild
agentdir memory search --across registered <query>
agentdir context build --across registered <task>
```

Federated index rows should include:

- `source_root_id`
- `source_root_path`
- `source_id`
- `source_file_path`
- `source_kind`
- `event_type`
- `session_id`
- `workspace`
- `git_head`
- `date_utc`
- `text_sha256`
- short excerpt
- derived vector or passage reference

Defaults:

- Register roots explicitly.
- Exclude archives.
- Copy metadata and excerpts, not full bodies.
- Require opt-in for full-body replication.
- Keep child roots canonical.

This is the clearest "revolutionary" AgentDir direction: a local, inspectable,
cross-repo memory plane for agents that still behaves like Maildir plus
rebuildable indexes.

## Stage 4: Optional Vector Extras

Optional extras should sit behind the same retrieval interface:

```text
agentdir memory backend list
agentdir memory backend configure sqlite-vec
agentdir memory backend configure qdrant --url <url>
agentdir memory embeddings configure fastembed --model <model>
```

Recommended order:

1. `sqlite-vec` for embedded vector search.
2. `fastembed` for local embedding generation.
3. Qdrant only for larger stores or shared deployments.

Do not make any of these required. Do not let them replace envelopes, AgentDir
SQLite metadata, or evidence records.

## Regression Risks

| Risk | Control |
| --- | --- |
| Memory becomes treated as truth | Keep explicit source classes: `evidence`, `retrieval_hint`, `summary` |
| Archived sessions leak into active retrieval | Add archive/prune semantic tests |
| Ranking drift hides relevant evidence | Add adversarial ranking fixtures and score explanations |
| Session summaries dominate real messages | Penalize summaries and diversify by source class |
| Filters leak after passage collapse | Apply filters before candidate generation |
| Search slows on larger stores | Add benchmark gates at 1k, 10k, and 50k envelopes |
| Optional vector backend corrupts trust model | Store backend outputs only as derived cache rows |
| Context protocol overclaims enforcement | Keep `advisory` mode explicit |
| Machine federation leaks private repo data | Require explicit root registration and visibility flags |

## Required Tests Before Promotion

- `test_memory_search_excludes_archived_sessions_after_apply`
- `test_context_build_excludes_pruned_or_archived_memory_hits`
- `test_recent_session_summaries_drop_archived_sessions_after_rebuild`
- `test_query_semantic_honors_git_head_workspace_actor_and_time_filters`
- `test_memory_search_prefers_message_hit_over_weaker_session_summary_when_exact_evidence_exists`
- `test_memory_explain_on_summary_hit_marks_it_as_derived`
- `test_memory_search_no_rebuild_can_be_stale_and_default_path_refreshes_it`
- `test_concurrent_emit_and_memory_search_do_not_corrupt_index`
- `test_semantic_search_with_conflicting_old_and_new_fixes_can_be_constrained_by_git_head_or_since`
- `test_context_pack_emit_round_trips_manifest_artifact`
- `test_context_consume_records_source_subset_and_purpose`
- `test_context_cite_distinguishes_evidence_from_retrieval_hints`
- `test_handoff_create_links_actor_message_to_pack_id`
- `test_federated_search_excludes_archives_and_unregistered_roots`

## Release Gates

Existing gates remain required:

```bash
python3 -m compileall src tests
uv run --with pytest pytest -q
bash -n examples/dogfood-session.sh
KEEP_WORKDIR=1 bash examples/dogfood-session.sh
```

Add semantic gates before any promotion:

- Retrieval regression suite.
- Context protocol round-trip suite.
- Archive/prune semantic exclusion suite.
- 1k, 10k, and 50k envelope benchmark suite.
- Disposable install smoke that runs `memory search`, `context build`,
  `context build --emit`, archive apply, and semantic search after archive.
- `doctor` or `memory doctor` validation for stale memory tables, orphaned
  memory rows, vector dimension consistency, and archive leakage.

## External Reference Points

These references informed the optional-backend and hybrid-retrieval tradeoffs:

- [`sqlite-vec`](https://github.com/asg017/sqlite-vec) is a small SQLite vector
  extension, but it is still pre-v1.
- [`sqlite-vss`](https://github.com/asg017/sqlite-vss) is not the right default;
  its own README points users toward `sqlite-vec`.
- [SQLite FTS5](https://www.sqlite.org/fts5.html) has built-in BM25 ranking
  behavior and can be used as an optional candidate generator when available.
- [DuckDB VSS](https://duckdb.org/docs/current/core_extensions/vss.html) is
  powerful, but its HNSW persistence is still flagged as experimental.
- [LanceDB embedded OSS](https://www.lancedb.com/lp/embedded-oss) offers an
  in-process vector database, but would add another store and dependency stack.
- [Chroma persistent client](https://docs.trychroma.com/reference/python/client)
  supports local persistence, but production guidance points toward server
  backed operation.
- [Qdrant local mode](https://qdrant.tech/documentation/frameworks/langchain/#local-mode)
  can run without a server for small vector sets, while Qdrant server remains a
  heavier optional deployment.
- The 2026 `vstash` paper proposes local-first hybrid retrieval with SQLite,
  FTS5, `sqlite-vec`, and adaptive fusion, reinforcing the direction of hybrid
  local retrieval rather than vector-only search:
  <https://arxiv.org/abs/2604.15484>.

## Recommendation

Proceed, but not as "add RAG." The next AgentDir feature should be:

```text
Context Retrieval v1:
  hybrid passages + score explanations + context protocol manifests
```

Then:

```text
Memory Federation v1:
  registered roots + federated derived index + read-only context surface
```

Then:

```text
Vector Extras v1:
  optional sqlite-vec and local embeddings behind the same interface
```

This is a genuine improvement path because it makes AgentDir more useful to
agents while strengthening, not weakening, evidence discipline.
