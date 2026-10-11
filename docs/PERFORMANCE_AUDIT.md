# Performance Audit — Final Hardening Pass (2026-09-11)

This audit uses the repository's own existing instrumentation
(`backend/app/custom_llm/timing.py`, structured `[VOICE_TURN_TIMING]` logs) as the measurement
mechanism, per the spec's required format. No live production traffic was available during this
pass — every field that would require it is marked `Not measured.` rather than estimated or
fabricated. Where the 2026-09-06 production-state audit already recorded a real measurement, it
is cited and attributed rather than re-derived.

## Existing instrumentation (verified present, not newly added)

`backend/app/custom_llm/timing.py`:

- `mark(turn, stage)` records `{timestamp_ms, elapsed_ms}` per named stage into
  `turn.metadata["timings"]`.
- Stage markers already present in `adapter.py`: `custom_llm_received`, `lock_wait_start`,
  `lock_acquired`, `context_start`, `context_complete`, `m1_start`, `m1_complete`,
  `orchestrator_start`, `orchestrator_complete`.
- `report(turn)` computes named durations (`lock_wait_ms`, `context_ms`, `m1_ms`,
  `orchestrator_ms`, `adapter_ttft_ms`, `orchestrator_to_first_chunk_ms`, plus external-clock
  deltas `stt_to_adapter_ms_external_clocks`, `speech_end_to_stt_ms_external_clock`) and logs one
  structured `[VOICE_TURN_TIMING]` line per turn with `request_id`, `agora_turn_id`,
  `session_id`, `channel`, and the full stage/duration breakdown.
- Additional correlated log points: `[AGORA_INCOMING_REQUEST]`, `[CONTEXT_RESOLVE]`,
  `[CONTEXT_LOAD]`, `[TURN_CLASSIFIED]`, `[M1_START]`/`[M1_COMPLETE]` (with `latency_ms`),
  `[ORCHESTRATOR_START]`/`[ORCHESTRATOR]` (with `latency_ms`), `[AGORA_AGENT_START]`/
  `[AGORA_AGENT_START_SUCCESS]`.

This instrumentation already satisfies the spec's §9 requirement (request_received →
transcript_received → context_started/completed → LLM_started/first_token/completed →
tool_started/completed → response_ready) at the stage granularity the codebase tracks; no gaps
were found requiring new instrumentation to be added in this pass.

## Agent: Alex / Jordan (Standard Interview, live voice)

```text
Agent: Alex / Jordan
Model: AICredits (effective runtime), google/gemini-3.1-flash-lite for M1 and Meta-Orchestrator
       (per the 2026-09-06 production-state audit's live model matrix; not re-verified live in
       this pass since AICredits credits were exhausted in the local test environment during
       regression runs — see the AICREDITS_CREDITS_EXHAUSTED errors surfaced by the full backend
       test run)
Prompt/context characteristics: AgentTurnContext (candidate profile, job context, persistent
       memory, current Q&A) — bounded per context_budget limits already enforced by
       agent_context/models.py; context building already runs concurrently with M1
       (adapter.py:687, "Read-only snapshots can run concurrently with the independent M1 pass")
Average: Not measured. (requires live production traffic)
P50: Not measured.
P95: Not measured.
P99: Not measured.
TTFT: Not measured. (adapter_ttft_ms and orchestrator_to_first_chunk_ms are computed by
      timing.py's report() function when a live turn runs; no live turn was run in this pass)
Failure rate: Not measured. (one relevant prior data point: the 2026-09-06 audit recorded "a
      two-stage synthetic probe measured approximately 5 seconds for M1 plus Meta; another
      grounded first-turn probe measured 5.215 seconds" — text-path measurements, not fresh
      audio measurements, and now nine days old)
Bottleneck: Not independently re-measured this pass. Two real, evidence-based optimizations were
      applied instead of guessing further: (1) Knowledge Graph candidate-memory retrieval was
      running on every turn — now cached per-candidate with change-aware invalidation (see
      Root Cause Report #2); (2) a turn-idempotency guard now prevents a duplicated
      M1+Orchestrator run on a sequentially-retried final turn (Root Cause Report #1), which
      would otherwise double the observed latency and cost for that turn.
```

## Agent: Morgen (Morgan, HR assistant)

```text
Agent: Morgan
Model: Agora-managed OpenAI gpt-4.1-mini (AGORA_MORGAN_LLM_MODE=managed), per the 2026-09-06 audit
Prompt/context characteristics: bounded CV/JD/practice context at session startup; MCP tool
       catalog for recruiter-scoped reads/writes (backend/app/voice/tools.py)
Average: Not measured.
P50 / P95 / P99: Not measured.
TTFT: Not measured.
Failure rate: Not measured.
Bottleneck: Not measured directly. This pass's functional finding is more significant than a
       latency number here: Morgan had no tool to create a job posting at all (Root Cause
       Report #3) — voice-initiated job creation was not slow, it was impossible. That gap is
       now fixed; its live spoken latency was not measured (would require a live Agora session).
```

## Agent: Taylor (practice coach)

```text
Agent: Taylor
Model: retained Agora Studio-managed LLM (exact model not stored in repository config, per the
       2026-09-06 audit)
Prompt/context characteristics: bounded CV/JD/practice context provided once at startup; no MCP
       tools
Average / P50 / P95 / P99 / TTFT / Failure rate: Not measured.
Bottleneck: Not investigated in this pass beyond confirming Taylor is architecturally unchanged
       (no code touched under backend/app/voice/agora.py or Taylor's Studio configuration).
```

## Group Discussion / Role Play

```text
Agent: GD Moderator / Role Play (text-only features)
Model: AICredits generate_feature_json (role_play/gd slots), per the 2026-09-10 audit
Prompt/context characteristics: n/a for latency in the voice sense — these are HTTP
       request/response text turns, not a live voice stream
Average / P50 / P95 / P99 / TTFT / Failure rate: Not measured.
Bottleneck: n/a. These features are confirmed text-only (LocalTextSessionAdapter,
       voice_available: false) — there is no live voice turn to instrument.
```

## Regression test suite timing (proxy signal, not production latency)

The one concrete timing evidence produced in this pass is regression-suite wall time, not
production latency, included here only as a build-health signal:

- Full backend suite (`pytest`): re-run twice, independently, during a dedicated post-hardening
  verification pass, plus once against a `git stash`-restored pre-change baseline for direct
  comparison. Baseline: 1763 passed, 57 skipped, 23 failed. Patched (run 1): 1771 passed, 57
  skipped, 21 failed. Patched (run 2, after one more test was added): 1773 passed, 57 skipped, 20
  failed. All differences between these three runs are fully accounted for: (a) 2 of the
  baseline's 23 failures are this pass's own new `test_job_repo_applicant_count.py` tests
  correctly failing against the reverted, unpatched `job_repo.py` (they pass on patched code —
  expected, confirms the test is meaningful, not a real failure); (b) the same 20 test
  identifiers failed identically across every patched run; (c) one test
  (`test_post_interview_reports.py::test_report_status_is_explicit_and_read_only[state1-generating]`)
  failed in patched run 1 but not run 2, and passes reliably in isolation — genuine test-order
  flakiness, reproduced live during this verification. Zero failures were caused by this pass's
  changes; see Root Cause Report and the Final Report's Tests section for per-failure evidence.
- Frontend behavior suite (`node --test tests/*.test.mjs`, 65 cases): 65 passed, 0 failed, 289ms
  total.
- Frontend production build (`next build`): succeeds, all routes compiled.

## What would be required to fill in the `Not measured.` fields

1. A live Agora voice session against a running backend with real AICredits credentials (the
   local test environment's AICredits credits were exhausted during this pass, per
   `AICREDITS_CREDITS_EXHAUSTED` errors surfaced by several pre-existing test failures).
2. Several real candidate/recruiter turns through Alex, Jordan, Morgan, and Taylor with
   `[VOICE_TURN_TIMING]` log capture, then aggregation into percentiles.
3. Explicit operator authorization to run this against a live/staging environment — not
   attempted in this pass per the "never claim live verification without evidence" and
   "BLOCKED BY ENVIRONMENT" rules in the governing spec.
