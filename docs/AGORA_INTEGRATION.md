# Agora Integration — Shared Architecture

This document covers what is shared across Standard Interview, Group Discussion, and
Role Play: the agent registry, the dispatch service, and the Agora resource audit for
this pass. Feature-specific architecture is in `GROUP_DISCUSSION.md` and
`ROLE_PLAY.md`.

## 1. One registry, one dispatch service, four logical agents

```
app.agents.registry.AgentRegistry
        │
        ├── alex     (channels: ["standard_interview"])
        ├── jordan   (channels: ["standard_interview"])
        ├── gd_mod   (channels: ["group_discussion"])      ← existing, now wired
        └── role_play(channels: ["role_play"])              ← existing, now wired
        │
        ▼
app.services.agora_agent_service.AgoraAgentService
        .start_interview_agent(interview_id, agent_id, ...)
        .stop_interview_agent(interview_id, agent_id, agora_agent_id)
        │
        ▼
Agora Conversational AI Agent v2 REST API (api.agora.io)
```

`gd_mod` and `role_play` were already registered (a prior commit, `b98f288`) with
real Agora Agent Studio pipeline IDs, mapped via `get_gd_mod_agora_mapping()` /
`get_role_play_agora_mapping()` exactly like Alex/Jordan. **This work did not
register any new agent or create any new Agora pipeline** — it wired the *existing*
registrations into the GD and Role Play product features, which previously never
called `AgoraAgentService` at all.

The one function, `AgoraAgentService.start_interview_agent()`, now serves all four
agents across all three features. It gained two new optional parameters, both
backward compatible (Alex/Jordan never pass them, so their dispatch payload is
byte-for-byte unchanged from before this pass):

* `system_prompt_override` — lets GD/Role Play give the live agent a
  session-specific prompt (topic+competencies for the moderator; persona
  identity+phase for the character) instead of the fixed `AgentProfile.instructions`.
* `remote_rtc_uids` — lets a future caller narrow which UIDs the agent subscribes to
  (defaults to `["*"]`, unchanged from before, which is what makes a multi-participant
  GD channel work at all — the moderator already subscribed to every remote UID).

## 2. Registered pipelines (unchanged by this pass)

| Agent | Project ID | Pipeline ID | Reserved RTC UID |
|---|---|---|---|
| `alex` | `a71666df598e499992a0ee5499dc7dcf` | `6d40244417034eb4bdae42218f820a85` | mapping default |
| `jordan` | `a71666df598e499992a0ee5499dc7dcf` | `682bbf59d8914e97a983e40a3dab895a` | mapping default |
| `gd_mod` | `a71666df598e499992a0ee5499dc7dcf` | `4c8d370abbb640d09a1a4d8a5c64322f` | `998877` |
| `role_play` | `a71666df598e499992a0ee5499dc7dcf` | `e31086fedf71452e807948c0a59e4142` | `888989` |

All four share one Agora project/account (`a71666df598e499992a0ee5499dc7dcf`,
migrated in an earlier commit). No pipeline ID above was changed by this pass.

## 3. Agora resource audit (before/after)

Per the task's explicit requirement to verify no duplicate/leaked agent instances:

```
Before this pass (registered pipelines): 4 logical agents (alex, jordan, gd_mod, role_play)
After this pass (registered pipelines):  4 logical agents -- unchanged
```

**Live agent *instances*** (ephemeral runtime dispatches, not pipeline
configuration) were checked via `GET /conversational-ai-agent/v2/projects/{app_id}/agents`
before and after this work:

* Before wiring GD/Role Play control-flow to real dispatch, this environment's Agora
  project had **one already-RUNNING agent instance** left over from a prior session
  (start timestamp predates this session's own work; most likely an unstopped
  verification dispatch from the commit that originally registered `gd_mod`/
  `role_play`). This was discovered and stopped as part of this audit — a genuine
  leaked resource, not something this pass introduced, but cleaned up as part of it.
* During development, running this repo's **existing** `tests/features/test_role_play.py`
  integration tests (which call `RolePlayService.control(..., action="start")`)
  against this environment's real `.env` credentials caused **three real, billed
  Agora agent dispatches** before the test files were updated to mock
  `AgoraAgentService.start_interview_agent`/`stop_interview_agent` (see the `_no_live_agora_calls`
  fixture added to both `tests/features/test_group_discussion.py` and
  `tests/features/test_role_play.py`). All three were confirmed stopped via the same
  agent-listing endpoint. **This is now fixed at the test-fixture level** so it cannot
  recur: every test that reaches a `control()`/`turn()` call in either feature's
  real-Postgres integration suite runs with Agora dispatch mocked, regardless of what
  a developer's local `.env` contains.
* Final state, verified immediately before writing this document:
  `{"data":{"count":0,"list":[]}}` — **zero running agent instances**, and the
  registered pipeline count is unchanged at 4.

This incident is disclosed in full (not just the outcome) because it is directly
relevant to the task's "No duplicate agents" / "Agora Resource Audit Before/After"
requirements, and because it surfaced a real product bug fixed as a result (see §4).

## 4. Bug found via this audit: autonomous completion didn't stop the voice agent

Both `GDService` and `RolePlayService` can transition a session to
`completed`/`cancelled` **without** going through an explicit `control(finish/cancel)`
call — GD's moderator can autonomously decide `END_DISCUSSION` past the configured
duration (inside `finish_gd_analysis`), and Role Play's orchestrator can autonomously
decide `END_SCENARIO` (escalation limit, time/turn limit, objectives demonstrated)
inside `turn()`. The original implementation only stopped the dispatched voice agent
from the explicit `control()` branch, so an autonomously-completed session would
leave its voice agent running until Agora's own idle timeout (or never, if the agent
keeps receiving audio). Fixed: `RolePlayService.turn()`/`ingest_voice_turn()` and
`GDService.message()`/`ingest_voice_turn()`/`generate_report()` all now check for
this transition and stop the agent (`_stop_moderator_if_ended` / the `new_state.status
=="completed"` check in `turn()`), clearing the stored `agora_agent_id` so a repeat
check never issues a redundant stop call. Covered by both mocked unit tests and
real-Postgres integration tests.

## 5. Environment variables

See the tables in `GROUP_DISCUSSION.md` §8 and this document's §2. No new required
variable was introduced; `AGORA_NOTIFICATION_SECRET` (already defined in
`app/core/config.py`/`.env.example`, previously unused anywhere in the codebase) is
now consumed by the new GD/Role Play voice webhooks for HMAC verification.

## 6. Separation of responsibilities (unchanged principle, reaffirmed)

```
Agora           = realtime voice transport + ASR + TTS
Logical agent   = persona/moderator identity + system prompt
Orchestrator /
GDModerator     = discussion/scenario control (deterministic + selective LLM use)
M1 / Intelligence = evaluation, evidence
Persistence     = Postgres (gd_events / roleplay_events), Knowledge Graph
```

Nothing in this pass moved durable state, authorization, or evaluation into the
Agora agent itself — the agent only ever receives a system prompt and produces
speech; every decision that has to be trustworthy (who can join, what counts as a
contribution, how it's scored) still happens in the backend, mostly inside the
existing Postgres RPCs.
