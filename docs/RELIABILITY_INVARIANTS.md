# Reliability Invariants — Intra AI

Adapted from the governing spec's §51 list to what is actually true in this codebase as of the
2026-09-11 hardening pass, with the file/mechanism that enforces each one. An invariant listed
here without a "Verified by" test reference is enforced by code inspection only, not by an
automated regression test — treat those as lower-confidence until a test is added.

1. **Existing Agora agents are reused; no accidental duplicate physical agent starts.**
   Enforced by `AgoraAgentService.start_interview_agent` starting only the active logical agent
   per interview round, and by the active-agent guard in
   `backend/app/custom_llm/adapter.py` (`requesting_agent_id != curr_agent_id` → silently drop
   the turn) which prevents an inactive agent's callback from producing competing output.
   The two Agora agents added by commit `b98f288` (`gd_mod`, `role_play`) are real, distinct
   pipelines — not duplicates of Alex/Jordan — but are currently registered without being wired
   into any live product flow (see Root Cause Report #4).

2. **Logical agent identity matches the physical speaking agent.**
   `context.current_agent_id` is the single source of truth read by `AgoraAgentService` when
   starting/handing off an agent; the active-agent guard above rejects any turn whose
   `x-agent-id` header doesn't match it.

3. **A transcript turn is not processed more than once.**
   Enforced by the turn-idempotency guard added in this pass
   (`backend/app/custom_llm/adapter.py`, checked before the M1/Orchestrator branch, marked
   "seen" only on successful completion). **Verified by:**
   `test_custom_llm_adapter.py::test_39_duplicate_final_turn_id_is_ignored`.
   Scope: covers the substantive answer path only, by design — deterministic fast-path replies
   (repeat-question, pause, audio-check, clarification, empty turn) are already side-effect-free
   and safe to reprocess; guarding them too was tried and reverted after it broke their own
   "context left unchanged" test invariants (see Root Cause Report #1 for the full story).

4. **Voice job creation creates a draft, never a published job.**
   `create_job_draft` (Morgan voice tool, added this pass) calls only
   `JobService.create_job`, which unconditionally sets `status=DRAFT`
   (`backend/app/services/job_service.py`). No `publish`/`approve` tool is exposed to Morgan at
   all — publishing is only reachable through the recruiter-authenticated REST route
   `POST /jobs/{id}/publish`, gated by `recruiter_actor` + `require_job` ownership check.
   **Verified by:** `test_voice_tools.py::test_create_job_draft_persists_real_draft_never_publishes`
   (asserts the created row's status is `"draft"` and explicitly asserts it is never
   `"published"`), plus a static assertion that no tool name containing `"publish"` is registered
   in Morgan's `TOOL_INPUTS` at all. `test_create_job_draft_concurrent_confirm_creates_only_one_job`
   additionally verifies the shared double-confirm/concurrent-confirm protection (a `_Pending`
   status machine plus a per-`confirmation_id` lock, shared by every write tool) also covers this
   tool specifically — two concurrent confirmations of the same proposal create exactly one job.

5. **Human approval controls publication.**
   `JobService.publish_job` requires the job to already be in `DRAFT` status and is invoked only
   through the explicit, separately authorized `POST /jobs/{id}/publish` route
   (`backend/app/routes/jobs.py`) — never as a side effect of creation, voice or otherwise.

6. **Eligibility-threshold crossing is sufficient authorization to invite; it does not require an
   additional unconditional manual click.**
   `EligibilityService.check_eligibility` now performs the `SHORTLISTED → INVITED` transition
   itself immediately after auto-shortlisting a candidate who crosses the threshold.
   **Verified by:** `test_application_quality.py::test_check_eligibility_auto_invites_on_threshold_crossing`,
   `test_check_eligibility_below_threshold_never_invites`. The manual "Send Invite" UI action
   remains available for any application that becomes `shortlisted` through a non-automatic path
   (e.g. HR override), and as a fallback if the automatic invite call itself fails (caught and
   logged, never raised into the eligibility-scoring result).
   **Known gap (pre-existing, not introduced by this invariant):** `ApplicationService.replace_resume`
   calls `check_eligibility` unconditionally at any application status with no guard, contradicting
   its own docstring's claim that shortlisted/invited/later states are retained. This invariant's
   auto-invite step now rides on top of that pre-existing rescoring behavior — see Root Cause
   Report §5 for the full analysis. Not fixed in this pass; flagged for a separate decision.

7. **Voice agents do not read entire generated documents aloud.**
   `create_job_draft`'s tool description explicitly instructs: "tell the recruiter it's ready to
   review; do not read the full job description aloud." Not independently enforced by a
   deterministic code check (LLM instruction-following, not validated application logic) — flag
   this as the one invariant in this list that depends on model compliance rather than code.

8. **M1 evaluates interview content; Meta-Orchestrator controls routing/next action; these
   responsibilities are not collapsed.**
   Unchanged by this pass — verified by inspection only (no code in `backend/app/interview_intelligence`
   or `backend/app/orchestrator` was modified). Role-play evaluation is likewise kept separate
   from the persona role in the actual `role_play` feature
   (`backend/app/role_play/response_generator.py` vs. `backend/app/intelligence/role_play/analyzer.py`),
   and this pass corrected an unused Agora agent profile
   (`backend/app/agents/role_play.py`) that had drifted from that separation in its prompt text
   (see Root Cause Report #4).

9. **Knowledge Graph stores persistent knowledge; it is read-only from the interview path.**
   `CandidateMemoryService` docstring: "Strictly read-only: Never mutates, creates, or deletes
   graph elements." The new per-turn memory cache added in this pass
   (`backend/app/agent_context/builder.py`) reads `context.accumulated_evidence` only to decide
   whether to reuse a cached read — it does not write to the graph, to `context`, or to any
   session state. **Verified by:** the existing `test_agent_turn_context.py` "Read-only
   guarantee" test continuing to pass unmodified against the new cache.

10. **InterviewAIContext owns live interview state; AgentTurnContext is a bounded per-turn
    snapshot.**
    Unchanged by this pass. The new memory cache attaches to the long-lived
    `AgentTurnContextBuilder` instance, not to `InterviewAIContext` or `AgentTurnContext`
    themselves — it does not blur this boundary.

11. **Candidate content cannot override system/company instructions.**
    Unchanged by this pass — not touched.

12. **Persistence failures should not unnecessarily kill a live turn or session.**
    Confirmed unchanged and correct by inspection during this pass's Role Play/GD trace: GD
    durably appends a contribution *before* attempting moderation/analysis
    (`backend/app/group_discussion/service.py`), and wraps the analysis step in
    `try/except AppError: pass` so a DB/AI failure there is logged and retried later via a
    lease-based drain, never dropping the message or crashing the session. Both RP and GD
    isolate evidence→Knowledge-Graph delivery failures per-event with backoff, never raising into
    the live turn.

13. **UI should not overflow its containers or the viewport.**
    Investigated: the Group Discussion feature had a more severe issue than overflow — several
    components used undefined Tailwind design tokens, so affected elements rendered with **no**
    background/text color at all (see Root Cause Report #7), not merely inconsistent styling.
    Fixed by substituting the app's real theme tokens. Fixed elsewhere: Company Knowledge and
    Role Play "action" buttons were plain underlined text links, not styled as actions
    (Root Cause Report #8); Candidate Portal had a horizontal nav plus a literally duplicate,
    mismatched-label navigation block (Root Cause Report #9).

14. **Auto-invite / eligibility scoring never bypasses tenant/workspace authorization.**
    `EligibilityService.check_eligibility` operates on an `application_id` already resolved
    within the caller's authorized context (`application_service.py`'s existing call sites); the
    new auto-invite step reuses the same `ApplicationRepo.update_status` call the manual
    `invite()` path uses, with no new authorization surface introduced.

15. **New Agora agent profiles must not conflate the simulated-persona role with the evaluator
    role, even when currently unused.**
    New invariant, added by this pass, reflecting the fix in Root Cause Report #4 — the spec's
    persona/evaluator separation rule (§23) is treated as unconditional, including for
    not-yet-wired-up agent profiles, since they are a landmine for future integration otherwise.
