# Root Cause Report — Group Discussion Voice & Role Play Agent Dropdown

Date: 2026-09-12. Scope: the two issues named in the task —
(1) Group Discussion was text-only, (2) the Role Play agent-selection dropdown had no
agents available. Written in the same evidence-first style as the prior
`docs/ROOT_CAUSE_REPORT.md`, which this document does not repeat.

---

## Issue 1 — Group Discussion was text-only

**Current (prior) behavior.** The GD feature (`app/group_discussion/`) had a
complete, correct, well-tested text pilot: event-sourced sessions with real
idempotency (`request_id`/`request_hash`), a deterministic+selective-LLM moderator
state machine, evidence-backed individual evaluation, and correctly-pinned
company-policy snapshots. Its `realtime` field always reported
`{"mode": "local_text", "voice_available": false}` via a stub adapter
(`app.integrations.assessment_realtime.LocalTextSessionAdapter`) that did nothing —
no Agora channel, no token, no agent.

**Root cause.** Two already-registered Agora agents existed
(`app.agents.gd_mod.GD_MOD_PROFILE`, mapped to a real Agora Agent Studio pipeline) but
were never called from anywhere — confirmed by exhaustive grep before this pass
(`app/group_discussion/*.py` had zero references to `AgoraAgentService` or Agora
token generation). The prior hardening pass (`docs/ROOT_CAUSE_REPORT.md`, finding #4)
explicitly logged this as a known, deliberately out-of-scope gap: "Wiring live Agora
voice into the role_play/group_discussion feature modules is a substantial,
multi-session feature build... explicitly out of scope for this hardening pass."
This task is that build.

**Missing integration, specifically:**
1. No per-participant Agora RTC UID/token issuance (GD is multi-participant; the
   existing candidate-token pattern in `app/sessions/service.py` is single-UID,
   single-candidate).
2. No moderator dispatch call from `GDService.control()`.
3. No path for Agora's own ASR to attribute speech to a specific human participant
   and commit it as a discussion contribution (the existing `append_gd_message` RPC
   requires an authenticated candidate JWT, which a voice webhook doesn't have).
4. UI copy explicitly disclaimed voice ("Text-only pilot... Agora voice... are not
   connected for GD").

**Implementation** (see `docs/GROUP_DISCUSSION.md` for the full architecture):
`app/group_discussion/identity.py` (deterministic RTC UID mapping),
`app/group_discussion/realtime.py` (Agora-aware `describe()`),
`app/group_discussion/webhook.py` (Agora→participant turn normalization + HMAC
verification), new `GDService` methods (`rtc_credentials`, `ingest_voice_turn`,
`_start_moderator`/`_stop_moderator`/`_stop_moderator_if_ended`), a new migration
(`migrations/20260912_01_gd_voice.sql`: `append_gd_voice_message`,
`set_gd_moderator_agent`), new routes (`POST .../rtc-credentials`, `POST
.../agora-webhook`), and a frontend RTC hook (`src/lib/multi-party-rtc.ts`) wired into
`session-page.tsx` with join/mute/leave controls and per-participant speaking
indicators.

**Files changed:** see §"Files Changed" in the final engineering report.

**Agora resources used:** the existing `gd_mod` pipeline only — see
`AGORA_INTEGRATION.md` §2-3 for the full before/after audit (including a leaked
pre-existing agent instance found and stopped, and a test-time live-dispatch
incident found and fixed at the fixture level — both disclosed there in full).

**Validation:** unit tests (`tests/test_gd_identity.py`, `tests/test_gd_webhook.py`,
`tests/test_group_discussion_voice.py` — 30 tests, mocked, no network) and real
Postgres/PostgREST integration tests (`tests/features/test_group_discussion.py`,
including a new voice-specific test exercising the actual new SQL RPCs) all pass;
`npx tsc --noEmit` and `next build` clean.

**Remaining limitations (honest, not glossed over):**
* The exact Agora webhook field carrying a per-turn speaker UID in a *multi-human*
  Conversational AI channel has not been observed against live 2+ participant
  traffic — the parser tries every plausible field name and is unit-tested against
  synthetic payloads only. **This must be confirmed against a real multi-party Agora
  session before speaker attribution is relied on for grading-grade evidence.**
* No live multi-browser/multi-device test was performed (2, 3, or 4 simultaneous
  human participants + moderator) — this requires real devices/microphones this
  environment does not have. Everything upstream of "does Agora correctly transcribe
  N overlapping human speakers and label each one" is implemented and tested; that
  specific product capability is Agora's, not this codebase's, and is asserted by
  Agora's own documentation (verified: "real-time speaker recognition and labeling
  for up to three simultaneous speakers") but not independently reproduced here.
* The live voice moderator's system prompt is fixed at dispatch time (topic,
  competencies, duration) and does not receive live updates to
  `moderator_state`/discussion signals mid-conversation — its real-time behavior is
  therefore somewhat independent of the asynchronous analysis pipeline's view of the
  discussion. This mirrors the existing text-pilot's own async/best-effort moderator
  response generation; voice sharpens the tension because the live agent must react
  in real time without waiting for that pipeline.

---

## Issue 2 — Role Play agent dropdown had no agents available

**Current (prior) behavior.** The actual Role Play feature (`app/role_play/`,
`frontend/src/features/role-play/`) had **no agent-selection UI or backend concept at
all** — sessions were created from a `scenario_id` + `candidate_id` only. There was no
literal "agent dropdown" in the code as such.

**Root cause.** Two things were true simultaneously, and either one alone would have
produced an empty/nonexistent dropdown:
1. **No listing endpoint existed.** `app/role_play/routes.py` had no `/agents`
   equivalent; the closest thing, the generic `/agents` endpoint
   (`app/routes/sessions.py`), lists agents for the Standard Interview lobby, not
   Role Play.
2. **Even a correct filter would have found nothing.** Every registered
   `AgentProfile` (Alex, Jordan, GD Moderator, Role Play) had an empty `metadata`
   dict with respect to capability — nothing declared which product surface it was
   for. A hypothetical `/roleplay/agents` built by filtering the generic registry with
   no further signal would have had to return either *all four* agents (wrong —
   Alex/Jordan are Standard-Interview-only) or *none* (also what a naive
   "role_play"-name-matching filter would find, since nothing was tagged). This is
   the literal mechanism behind "no agents are available": **the registry had no
   capability data to filter on, in either direction.**

**API involved:** none existed; added `GET /roleplay/agents`.
**Registry involved:** `app.agents.registry.AgentRegistry` — had no channel/capability
concept at all before this pass.
**Frontend issue:** no dropdown existed to render; `role-play-page.tsx` had a
"Scenario" select and a "Candidate" select only.
**Backend issue:** `RolePlayService.create()`/`SessionCreate` had no `agent_id`
concept; nothing to validate even if a client sent one.

**Fix:**
1. `AgentProfile.metadata["channels"]` — an explicit opt-in list
   (`"standard_interview"` / `"group_discussion"` / `"role_play"`) added to each of
   the four existing profiles (`app/agents/alex.py`, `jordan.py`, `gd_mod.py`,
   `role_play.py`). No new agent was created; this only tags the four that already
   exist.
2. `AgentRegistry.list_agents_for_channel(channel)` — a pure filter over the existing
   registry.
3. `GET /roleplay/agents` (`RolePlayService.compatible_agents()`) — the real listing
   endpoint, backed entirely by the registry, not a hardcoded list.
4. `SessionCreate.agent_id` (default `"role_play"`) — validated server-side against
   `compatible_agents()` in `RolePlayService.create()`; an unknown/incompatible
   `agent_id` is rejected with `ValidationError`, never silently accepted.
5. Frontend dropdown in `role-play-page.tsx` with the four required states (loading /
   available / empty / failed), and the selected `agent_id` is sent on session
   creation and persisted into `RolePlayState.agent_id`.
6. **The selection actually reaches the runtime**, not just the dropdown: see
   `docs/ROLE_PLAY.md` §4 for the traced path from `state.agent_id` through
   `AgoraAgentService.start_interview_agent(agent_id=state.agent_id, ...)`.

**Validation:** `tests/test_role_play_voice.py` (10 tests: registry filtering,
dynamic system prompt, dispatch/stop idempotency including autonomous completion,
RTC credential scoping, voice-turn ingestion/idempotency — all mocked, no network),
real Postgres/PostgREST integration tests (`tests/features/test_role_play.py`,
including a new voice-specific test exercising the actual new SQL RPC), `npx tsc
--noEmit` and `next build` clean.

**Acceptance criteria (§66 of the task) — status:**

```
[x] API returns compatible agents             GET /roleplay/agents
[x] Frontend successfully fetches agents      role-play-page.tsx useQuery
[x] Loading state works                        "Loading agents..."
[x] Agents are displayed                       real <select> from the API
[x] Agent can be selected                      agentId state -> createSession
[x] Selected agent ID reaches backend          SessionCreate.agent_id
[x] Backend validates selected agent           RolePlayService.create() ValidationError on unknown id
[x] Existing agent configuration is used       app.agents.role_play (unchanged pipeline)
[x] Role Play session starts                   control(start) dispatches with the pinned agent_id
[x] No duplicate agent is created              see AGORA_INTEGRATION.md §3 audit
```
