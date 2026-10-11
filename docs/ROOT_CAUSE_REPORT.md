# Root Cause Report — Final Hardening Pass (2026-09-11)

Scope note: two very recent internal audits already cover architecture, security, deployment
and Agora/AICredits configuration in depth —
[`INTRA_AI_PRODUCTION_STATE_AUDIT_2026-09-06.md`](INTRA_AI_PRODUCTION_STATE_AUDIT_2026-09-06.md)
and [`FEATURE_IMPLEMENTATION_AUDIT.md`](FEATURE_IMPLEMENTATION_AUDIT.md) (checkpoint 2026-09-10).
This report does not repeat their findings; it covers what changed or was newly investigated in
this pass, evaluated against `ui+technical_prompt (1).md`.

---

## 1. Turn idempotency gap (voice reliability)

**Issue:** A single final voice turn could be processed more than once.

**Observed behaviour:** No code checked Agora's per-turn identity before running M1 +
Meta-Orchestrator. A sequential retry of the same final transcript (e.g. Agora retrying a POST)
would be fully reprocessed: a second M1 call, a second orchestrator decision, and a second
mutation of accumulated evidence, all for content already answered.

**Root cause:** `backend/app/custom_llm/adapter.py`'s `process_turn_async` had a per-session
`asyncio.Lock` (`interview_context/store.py`) that serializes *concurrent* turns, but no
turn-id-based dedup for *sequential* repeats. `turn.metadata["agora_turn_id"]` was already
captured (`adapter.py`, `parse_turn`) but never read again.

**Evidence:** `backend/app/custom_llm/adapter.py:400` sets `agora_turn_id`; before this fix,
nothing else in the file referenced it.

**Fix:** Added a bounded, per-session `recent_turn_ids` list on `InterviewAIContext.metadata`.
Checked once, right before the M1/Meta-Orchestrator ("deep reasoning") branch begins — after
every deterministic fast path (repeat-question, pause, audio-check, clarification, empty turn,
opening) has already returned, so those remain exactly as inert as before. Marked "seen" only at
the turn's successful completion, not at entry, so a turn that failed or paused mid-way (and
never completed) is not silently dropped if Agora legitimately retries it.

**Files:** `backend/app/custom_llm/adapter.py`.

**Tests:** `backend/tests/test_custom_llm_adapter.py::test_39_duplicate_final_turn_id_is_ignored`
— asserts a repeated POST with the same `turn_id` invokes M1/orchestrator exactly once and
accumulated evidence reflects only one processed turn.

**Validation:** New test passes. Full `test_custom_llm_adapter.py` suite (38 tests) passes,
including the deterministic-fast-path invariant tests
(`test_empty_candidate_turn.py`, `test_live_asr_repairs.py`, `test_voice_clarification.py`,
`test_voice_service_pause.py`, `test_voice_turn_cancellation.py`,
`test_observed_conversation_progress.py`) that assert those paths leave context byte-for-byte
unchanged — an earlier version of this fix (checked at the top of the lock, unconditionally)
broke exactly those tests by writing to `context.metadata` on every turn; relocating the check
past the fast paths fixed it. That failure-then-fix is recorded here for traceability.

**Remaining risk:** The dedup window is bounded (last 20 turn ids per session, in-memory). A
restart clears it, matching this codebase's existing documented limitation that live interview
state is process-local (see the 2026-09-06 audit, deployment blocker #6).

---

## 2. Repeated Knowledge Graph read per voice turn (latency)

**Issue:** `CandidateMemoryService.get_candidate_memory_async` ran a full Neo4j-backed query on
every single voice turn, not once per session.

**Observed behaviour:** `backend/app/agent_context/builder.py`'s `build_turn_context_async`
called the memory service unconditionally inside `asyncio.gather(...)` alongside candidate/job
snapshot loading, which is otherwise correctly cached via session metadata
(`_candidate_from_metadata`/`_job_from_metadata`). Persistent memory had no equivalent cache.

**Root cause:** No caching existed for this specific read; every turn paid a full graph
traversal even when nothing about the candidate's persistent memory had changed since the last
turn.

**Fix:** Added an in-process, per-candidate cache on `AgentTurnContextBuilder`
(`_get_candidate_memory_cached`), invalidated whenever the live in-session
`accumulated_evidence` count grows (a signal this turn may have recorded evidence that will be
persisted to the graph and should be visible next read) or after a 30-second TTL (covers
cross-session/background-written updates). Bounded to 500 candidate entries with simple
insertion-order eviction, since the builder is a long-lived process singleton.

**Files:** `backend/app/agent_context/builder.py`.

**Tests:** Existing `backend/tests/test_agent_turn_context.py` (23 cases, including "Read-only
guarantee: context building never mutates graph or session state") passes unchanged — the new
cache mutates only the builder's own instance state, never `context` itself, so the read-only
guarantee holds.

**Validation:** `test_agent_turn_context.py` — 22/23 pass; the one failure
(`test_23_gated_live_or_mock_nemotron_test`) is pre-existing (confirmed identical on the
unmodified baseline via `git stash`) — **correction from an earlier version of this report,
made during independent post-hardening verification:** this test fails deterministically, in
isolation, every time, not only in the full suite. It is not test-order flakiness. Root cause:
the test mocks `call_groq` and asserts it was called, but the effective runtime provider is
`aicredits` (not `groq`), and the configured AICredits credits are exhausted in this local
environment (`AICREDITS_CREDITS_EXHAUSTED` in its own captured log), so the orchestrator falls
back to a deterministic decision without ever reaching Groq. This is an
ENVIRONMENT/EXTERNAL-DEPENDENCY failure (needs live, unexhausted AICredits credits, or a
Groq-selected runtime, to pass) — unrelated to this change either way.

**Remaining risk:** Cache correctness depends on `accumulated_evidence` growing whenever new
graph-relevant evidence is recorded — true for the current single-process interview flow. If
interview state ever moves to a distributed/multi-process store (a known separate limitation,
see 2026-09-06 audit blocker #6), this in-process cache would need to move with it or be
revalidated.

---

## 3. Morgan cannot create a job posting by voice

**Issue:** Spec's example dialogue ("Create a job posting." → Morgan asks role, comp, skills →
persists a draft) had no corresponding implementation.

**Observed behaviour:** `backend/app/voice/tools.py`'s `TOOL_INPUTS`/`WRITE_TOOLS` had 23 tools
covering candidates, scheduling, email, calendar, and Slack — but only one job-related tool,
`list_jobs`, which is read-only. There was no `create_job`/`draft_job` entry at all, so a
recruiter asking Morgan to create a job had literally nothing for the assistant to call — not a
case of Morgan "just" instructing the user to use the UI instead.

**Root cause:** The voice-job-creation feature was never built; the underlying REST job-creation
flow (`backend/app/routes/jobs.py`, `JobService.create_job`/`publish_job`) already existed and
already correctly separates draft creation from an explicit, separately authorized publish
action — nothing there needed fixing.

**Fix:** Added `create_job_draft` as a new confirmation-gated write tool, following the exact
same propose → recruiter-confirms → execute pattern already used by every other write tool in
this file (`_review`/`_authorize_action_resources`/`_execute` branches). It asks only for
genuinely required fields (title, must-have skills); anything unstated by voice takes the same
starting defaults the manual job-creation wizard uses (`frontend/.../admin/jobs/new/page.tsx`'s
`DEFAULT_ROUNDS`, department "Engineering", location "Remote", job type "remote", experience
2–6 years) — mirrored in `_DEFAULT_JOB_DRAFT_ROUNDS` and `_draft_job_review` — so the created row
is a valid, schema-complete `DRAFT`, not a second, differently-shaped job model. It calls the
existing `JobService.create_job` directly; there is no `publish` tool exposed to Morgan at all,
so voice can never publish.

**Files:** `backend/app/voice/tools.py`.

**Tests:** `backend/tests/test_voice_tools.py::test_create_job_draft_persists_real_draft_never_publishes`
(asserts a real `DRAFT` row is persisted with the stated title/skills and the wizard's defaults,
requires one confirmation, and is never `published`),
`test_create_job_draft_requires_at_least_one_skill`,
`test_create_job_draft_concurrent_confirm_creates_only_one_job` (added during independent
post-hardening verification, after noting the existing generic double-confirm/concurrent-confirm
protection tests are hardcoded to `schedule_interview` — this proves the same `_Pending`
status-machine protection also covers `create_job_draft` specifically, rather than assuming it
by analogy to another tool). Updated `test_schema_allowlist_is_exact_and_closed` to include the
new tool in the enumerated allowlist.

**Validation:** All 113 tests in `test_voice_tools.py` pass.

**Remaining risk:** Voice confirmation copy ("I've created the draft. Please review it and
approve it when you're ready.") is specified in the tool description for the LLM to use, but
LLM phrasing is inherently non-deterministic — the actual spoken confirmation text from a live
Morgan session was not observed (requires a live Agora session). Marked
`NOT VERIFIED — requires manual/external validation` below.

---

## 4. Latent persona/evaluator conflation in an unused Agora agent profile

**Issue:** Spec §23 requires "Role-Play Agent = simulated character; must not become the
evaluator" as an unconditional rule.

**Observed behaviour:** `backend/app/agents/role_play.py` (added by the prior commit `b98f288`,
which registered `gd_mod`/`role_play` as new Agora Agent Studio agents) had `role="AI Role Play
Evaluator"` and instructions including "Evaluate the candidate's communication, empathy, and
problem-solving through the interaction" — directly asking the persona to also score the
candidate.

**Root cause:** This new agent profile was registered into the generic interview `AgentRegistry`
(used by the separate M1/"interviews" product) but is not referenced by any `agent_id` call site
anywhere in the codebase, and is not imported by or wired into the actual `backend/app/role_play/`
feature module at all (confirmed by exhaustive grep). It is currently inert — not a live risk —
but the wording is a landmine for whoever eventually wires it up, and the actual `role_play`
feature already implements the correct separation (`role_play/response_generator.py`'s system
prompt explicitly says "Do not evaluate the candidate, reveal scoring rules...").

**Fix:** Rewrote the profile's `role` to "AI Role Play Persona" and its instructions to match the
separation already correctly implemented in `response_generator.py` — persona-only, evaluation
explicitly out of scope, matching the working feature's own prompt.

**Files:** `backend/app/agents/role_play.py`.

**Validation:** No test referenced the old string (`AI Role Play Evaluator`) or asserted on the
evaluator-flavored instructions text — grep-confirmed before changing it. Field is unused by any
live path, so this is a zero-risk correctness fix.

**Remaining risk:** None from this change. Separately (not fixed, logged only): `gd_mod` and
`role_play` are real, distinct Agora pipelines (not duplicates of Alex/Jordan — verified RUNNING
on Agora cloud per the `b98f288` commit message) but are registered without being connected to
any actual product flow. Wiring live Agora voice into the `role_play`/`group_discussion` feature
modules is a substantial, multi-session feature build (real-time session/turn protocol, speaker
attribution, moderator turn-taking) — explicitly out of scope for this hardening pass per the
spec's own "not a new product-design phase" principle. Logged as a known gap, not fixed here.

---

## 5. Eligibility-crossing required an unconditional manual invite click (business logic)

**Issue:** Spec §4: "If eligibility threshold is crossed, avoid unnecessary HR approval unless
the existing business rules genuinely require it." (User-approved change for this pass: implement
real auto-invite.)

**Observed behaviour:** `EligibilityService.check_eligibility` already auto-shortlists on
threshold crossing (`backend/app/services/eligibility_service.py`), but actually inviting the
candidate to schedule (`ApplicationService.invite`, a separate `SHORTLISTED → INVITED`
transition) was a manual, always-required action — the frontend unconditionally rendered a "Send
Invite" button for every shortlisted row (`frontend/.../admin/jobs/[id]/page.tsx`).

**Root cause:** Auto-invite was never implemented; only auto-shortlist was.

**Fix:** `EligibilityService.check_eligibility` now performs the `SHORTLISTED → INVITED`
transition itself, immediately after auto-shortlisting, reusing the same repository call
`ApplicationService.invite()` uses internally (`eligibility_service.py` cannot import
`ApplicationService` directly — it already constructs `EligibilityService`, so that would be
circular — the repository call is reused directly instead, matching `invite()`'s exact
semantics). Wrapped in try/except so a failure to auto-invite never breaks the eligibility
scoring/shortlisting that already succeeded; the recruiter's manual "Send Invite" button remains
available as a fallback for that case and for any application that reaches `shortlisted` through
a non-automatic path (e.g. HR override).

**Files:** `backend/app/services/eligibility_service.py`.

**Tests:** `backend/tests/test_application_quality.py::test_check_eligibility_auto_invites_on_threshold_crossing`,
`test_check_eligibility_below_threshold_never_invites`.

**Validation:** Both new tests pass; existing `test_recruiter_can_reconsider_an_automatically_rejected_application`
unaffected.

**Remaining risk:** "Invite" in this codebase is a pure status transition (`SHORTLISTED →
INVITED`); it does not itself send an email — no email-sending code path was found attached to
`invite()`. The candidate presumably discovers the "invited" state on their portal / gets
scheduling access from it. If a future email-notification-on-invite feature is added, it will
now fire automatically on auto-shortlist too — worth an explicit product decision at that time.

**Additional finding from independent post-hardening verification (not fixed — flagged for a
separate decision, out of this fix's scope):** `ApplicationService.replace_resume` (used when a
recruiter re-uploads/corrects a candidate's resume, `POST /applications/{id}/resume`) calls
`check_eligibility` unconditionally, at **any** application status, with **no** status guard
beforehand — despite its own docstring's explicit claim that "existing recruiter decisions
(shortlisted/invited and later interview states) are retained." `check_eligibility` always
overwrites `status` to `SHORTLISTED` or `REJECTED` based on the new score; this contradiction
between the docstring and the actual code **pre-dates this pass** (confirmed: the unconditional
`update_status(...)` call this relies on was already there before my change). My auto-invite
addition makes the practical impact of that pre-existing gap slightly worse: if a recruiter
replaces the resume of an already-`INVITED`/`scheduled`/`completed` candidate and the new score
happens to cross the threshold, the application is now not only silently downgraded back to
`shortlisted` (pre-existing behavior) but immediately auto-invited again on top of that
(new behavior) — potentially resetting a candidate who already interviewed back into an
"invited, please schedule" state. This is a real, evidenced business-logic risk, but fixing the
underlying status-downgrade-on-rescoring behavior is a separate, broader change than the
approved auto-invite fix and was not made here — flagging it for an explicit decision rather
than silently expanding this fix's scope.

---

## 6. Job applicant count is a permanently stale value

**Issue:** Spec: "Trace: Application → Database → Backend → Query/cache → Jobs UI. Use real
persisted data. Do not hardcode."

**Observed behaviour:** `jobs.applications_count` is a real DB column, read by
`frontend/.../admin/jobs/page.tsx`'s list view — but `application_service.py` (where
applications are actually created/shortlisted/invited/rejected) never references or increments
it anywhere. It permanently reflects its seeded default. Separately, the job *detail* page
already derives a correct live count from `useJobApplications`, so the list and detail pages
could show two different numbers for the same job.

**Root cause:** The column was likely intended to be denormalized/cached but the write side was
never implemented.

**Fix:** `JobRepo.get_by_id`/`JobRepo.list` now compute a live applicant count from the
`applications` table (one extra `count="exact"` query for `get_by_id`; one extra grouped query
for `list`, counting a page of jobs at once rather than one query per job) and overwrite
`applications_count` on the returned row before it reaches `JobService`/`JobResponse` — mirroring
exactly what the detail page already derives correctly, rather than adding increment/decrement
logic at every application-state-change call site (safer: single source of truth, no drift risk).

**Files:** `backend/app/repositories/job_repo.py`.

**Tests:** `backend/tests/test_job_repo_applicant_count.py` (3 cases): live count overrides a
stale stored value on `get_by_id`; `list()` populates correct per-job counts in one extra query;
an empty job list makes no extra query.

**Validation:** New tests pass (3/3). Reran `test_voice_tools.py` (112 tests, includes a job
creation path that calls `get_by_id`) and `test_recruiter_workspace.py`/`test_interview_templates.py`
(78 tests) — all pass, confirming the extra query doesn't break existing job-repo callers.

**Remaining risk:** Adds one extra DB round trip per `get_by_id`/`list` call. Bounded and
inexpensive (COUNT / narrow-column SELECT, not a full row fetch), consistent with "targeted,
evidence-based" scope; not measured under production load (`Not measured.` in the performance
doc).

---

## 7. UI: undefined design tokens in Group Discussion (functional bug, not just inconsistency)

**Issue:** Spec: "GD UI: Use the same HR design language... loading/empty/error states."

**Observed behaviour:** `frontend/src/features/group-discussion/components/*.tsx` used shadcn/ui
convention classes — `bg-primary`, `text-primary-foreground`, `text-muted-foreground`, `bg-card`,
`text-destructive`, `bg-muted`, `bg-background`, `border-primary` — none of which are defined in
this app's actual Tailwind v4 `@theme` block (`frontend/src/app/globals.css`, which defines
`brand`, `bg`, `surface`, `border`, `text-primary`, `text-muted`, `success`/`warning`/`error`,
etc. — no `primary`, `card`, `destructive`, `muted`, or `background` keys at all). This is more
severe than a style mismatch: these utility classes resolve to nothing in this app's build, so
the affected buttons/cards/text render with no color styling at all, not merely a different
design language.

**Root cause:** The GD feature was built against a different (shadcn default) token vocabulary
than the rest of the app ever adopted.

**Fix:** Mechanical token substitution across `discussions-page.tsx`, `session-page.tsx`,
`report-card.tsx`, `invitation-page.tsx` to the app's real tokens (`bg-brand`, `text-text-inverse`,
`text-text-muted`, `bg-surface`, `text-error`, `bg-border`, `bg-bg`, `border-brand`), plus
adopting the shared `Button`, `Card`/`CardHeader`/`CardTitle`/`CardContent`, `StatusBadge`, and
`EmptyState` components in `discussions-page.tsx` in place of local `inputClass`/`buttonClass`
constants and a hand-rolled empty state, matching the pattern used across `admin/jobs` etc.

**Files:** `frontend/src/features/group-discussion/components/{discussions-page,session-page,report-card,invitation-page}.tsx`.

**Validation:** `npx tsc --noEmit` clean; grep-confirmed zero remaining undefined-token classes
across the feature directory; production build (`npm run build`) succeeds.

**Remaining risk:** `session-page.tsx`'s remaining raw `<button>` elements (join/leave/raise-hand,
reply, etc.) were token-corrected but not converted to the shared `Button` component in this
pass — a further, lower-priority polish item, not a functional bug (colors now resolve
correctly).

---

## 8. UI: Company Knowledge and Role Play actions rendered as plain inline links

**Issue:** Spec explicitly: "Document Version and Policy must look like intentional
navigation/actions, not ordinary inline links" and "Create Persona and Create Scenario must be
clear buttons/actions/tabs."

**Observed behaviour:** Both `frontend/src/features/company-knowledge/components/knowledge-page.tsx`
and `frontend/src/features/role-play/components/role-play-page.tsx` used raw
`<button className="text-brand underline">` and a native `<details>/<summary>` disclosure,
importing no shared UI components.

**Fix:** Swapped in the existing `Button`/`Card`/`Badge` components (same ones used across
`admin/jobs`). Version history is now a `Button`-triggered toggle instead of a native `<details>`
element. No new components were created — pure reuse.

**Files:** `frontend/src/features/company-knowledge/components/knowledge-page.tsx`,
`frontend/src/features/role-play/components/role-play-page.tsx`.

**Validation:** `npx tsc --noEmit` clean; production build succeeds.

---

## 9. UI: Candidate portal used a horizontal nav plus a genuinely duplicate nav block

**Issue:** Spec: "Use the vertical sidebar with candidate-specific navigation: Dashboard,
Applications, My Profile, other existing candidate features. Remove redundant old horizontal
navigation."

**Observed behaviour:** `frontend/src/app/(candidate)/layout.tsx` rendered `CandidateNav` (a
horizontal top bar) for every candidate page, plus a second, separate inline `<nav>` block
directly in the page body with different labels ("Applications"/"My profile") than the header's
own links, and a "My profile" link that didn't exist in `CandidateNav` at all.

**Root cause:** `CandidateNav` is shared between authenticated candidate pages and unauthenticated
public pages (job browsing, login) — a legitimate horizontal-nav use case for visitors with no
account context yet. The redundant inline `<nav>` appears to have been added later without
removing anything.

**Fix:** Added `CandidateSidebar` (`frontend/src/components/layout/candidate-sidebar.tsx`),
visually mirroring the existing HR `Sidebar` component, with candidate-specific destinations
(Dashboard → `/portal`, Browse Jobs, My Profile, Group Discussions, Role Play, Training). Wired
into `(candidate)/layout.tsx` only for the authenticated, non-HR branch (mirroring
`admin-layout.tsx`'s mobile-overlay + collapsible-sidebar pattern); unauthenticated/public pages
(job browsing, login) keep `CandidateNav`, since a sidebar navigating between "my applications"
and "my profile" doesn't make sense for a visitor with no account yet. The duplicate inline
`<nav>` block is removed entirely.

**Files:** `frontend/src/app/(candidate)/layout.tsx` (new),
`frontend/src/components/layout/candidate-sidebar.tsx` (new).

**Validation:** `npx tsc --noEmit` clean; production build succeeds (all `/portal`, `/profile`,
`/group-discussions`, `/role-play`, `/training`, `/schedule/[token]` routes still compile and are
listed in the build output).

**Remaining risk:** Live navigation/responsive behavior (mobile overlay open/close, active-link
highlighting) was verified by code inspection and successful compilation only —
`NOT VERIFIED — requires manual/external validation` (no headless browser run was performed for
this specific change).

**Correction from independent post-hardening verification:** the inline code comment in
`(candidate)/layout.tsx` says the horizontal-nav fallback branch is kept for "job browsing,
login" — this is inaccurate. `/jobs` is served by a completely separate `(public)` route group
with its own layout and was never part of this layout to begin with; the actual, verified reason
the fallback branch matters is unauthenticated token-based access to
`/interview/:token/report` and `/interview/:token/done` (an invited candidate viewing their
interview outcome without ever creating an account, per the pre-existing
`isPublicInterview`/opaque-token design already documented elsewhere in this codebase). The
runtime behavior is correct; only the comment's stated rationale was wrong. Not corrected in
code during this verification pass per the instruction not to make additional changes without
flagging first — noted here as a one-line documentation fix to make later.

---

## 10. UI: Settings page had zero backend-connected Save actions

**Issue:** Spec: "Existing settings must be genuinely functional... No fake save behaviour."

**Observed behaviour:** All four Settings tabs (General, AI Configuration, Templates, Team) were
pure local `useState`, seeded from hardcoded constants. All four "Save" buttons had no `onClick`
handler at all.

**Root cause investigation:** Checked for backend support before deciding a fix. Found: (a) a
real `interview_templates` backend (`backend/app/routes/interview_templates.py`,
`frontend/src/lib/api/interview-templates.ts`, and a dedicated working editor at
`/admin/interviews/templates`) already exists and could genuinely back the Templates tab; (b) no
backend exists at all for workspace/company settings, AI-scoring configuration, or team-member
invitations — no `workspace` model, no settings table, no team-invite routes (confirmed via
`grep` across `backend/app/routes/` and `backend/app/models/`).

**Fix (user-approved scope):** Templates tab now genuinely lists real templates via
`interviewTemplatesApi.list()`, with a working Archive action, and links out to the existing full
editor at `/admin/interviews/templates` for create/edit rather than rebuilding a second,
differently-shaped editor inside Settings (the old mock modeled "question lists per round type";
the real backend models "reusable multi-round interview templates" — these are different
concepts, and duplicating the editor would violate "do not create duplicate infrastructure").
General/AI Configuration/Team tabs keep their illustrative UI but every Save/Upload/Invite/Remove
action is now `disabled` with an explicit "Not yet connected to a backend" label/tooltip, instead
of silently doing nothing — honest about current capability without fabricating new backend
infrastructure in a hardening pass.

**Files:** `frontend/src/app/admin/settings/page.tsx`.

**Validation:** `npx tsc --noEmit` clean; production build succeeds.

**Remaining risk:** General/AI Configuration/Team settings remain non-functional by design (per
user decision) — this is a known, explicitly surfaced gap, not a hidden one. Building real
workspace-settings and team-invitation backends is scoped as future work, not attempted here.

---

## 11. Minor: redundant manual ellipsis alongside CSS truncation (Round Architecture)

**Issue:** `frontend/src/components/jobs/round-sequencer.tsx`'s collapsed round summary applied
CSS `truncate` (which already ellipsizes on overflow) *and* manually appended a literal `"..."`
string whenever more than two focus areas existed — redundant, and capable of producing a
doubled/oddly-cut ellipsis for long focus-area lists.

**Fix:** Replaced the manual `"..."` with an explicit `, +N more` count, letting CSS `truncate`
remain the sole overflow-safety net. More informative than either bare ellipsis and removes the
redundancy. One-line change.

**Files:** `frontend/src/components/jobs/round-sequencer.tsx`.

**Validation:** `npx tsc --noEmit` clean; visually reviewed by reading the surrounding JSX (no
screenshot/browser run performed — `NOT VERIFIED — requires manual/external validation` for the
rendered appearance).

---

## 12. Candidate default view: table vs. the spec's stated Card/Grid default

**Issue:** Spec explicitly (twice) states "Default: Card/Grid View, Secondary: List View."

**Observed behaviour:** `frontend/src/app/admin/candidates/page.tsx` defaulted to `"table"`, with
a working toggle to `"kanban"` — and "kanban" here is a genuine status-grouped Kanban board
(columns per pipeline stage, e.g. Applied/Shortlisted/Interviewed), not a flat card gallery. No
third, plain "grid of cards" view exists in this codebase.

**Fix:** Flipped the default `useState` to `"kanban"` (one line) — the closest existing
implementation to "Card/Grid" (individual candidate cards are the unit; they're grouped by
status rather than laid out in a flat grid). Table remains available as the secondary view via
the existing, unchanged toggle.

**Files:** `frontend/src/app/admin/candidates/page.tsx`.

**Honesty note:** This is a reasonable interpretation, not a proven match to the spec's literal
"grid of cards" wording — the app has no flat card-grid view distinct from the pipeline-grouped
Kanban board. Building a genuinely new flat-grid view would be new UI work, not a hardening fix,
so it was not attempted. Logged explicitly here rather than silently equating the two.

---

## Findings investigated, no code change (with reasoning)

- **Round Architecture / Job Description / "Role" page layout** — spec expected three parallel
  labeled sections; the actual detail page (`frontend/.../admin/jobs/[id]/page.tsx`) groups the
  same information differently (Job Description + Details + Required Skills + Configured Rounds).
  Verified no overflow, misalignment, or broken buttons. This is a naming/grouping difference,
  not a functional bug — changing it would be discretionary redesign, out of scope.
- **Application success page CTA alignment** — both buttons are equal-width flex children; no
  layout break found. One had a harmless duplicate className not present on the other — cosmetic,
  not a bug, not changed.
- **HR-side scheduling UX** — both candidate- and HR-facing scheduling flows inspected and found
  functionally solid (timezone handling, slot conflict handling, clear next actions); the only
  real gap found was the auto-invite item (finding #5 above), now fixed.
