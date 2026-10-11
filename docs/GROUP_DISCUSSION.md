# Group Discussion — Architecture

Status: realtime voice implemented and code-reviewed; **not yet verified against live
multi-browser Agora traffic** (see "Verification status" at the end of this document
and `docs/GD_ROLEPLAY_ROOT_CAUSE_REPORT.md`).

## 1. Session lifecycle

```
HR creates a GD (title, topic, duration, participant limit, competencies,
optional company-policy query)
        │
        ▼
gd_sessions row (status=lobby) ── app/group_discussion/service.py:create()
        │
        ▼
HR invites candidates (HMAC-signed, single-use invitation link)
        │
        ▼
Candidate accepts → gd_participants row (status=joined)
        │
        ▼
HR starts the discussion (status=lobby→active, quorum-checked server-side)
        │        │
        │        └──► AI moderator (gd_mod) dispatched into the Agora RTC channel
        ▼
Participants request RTC credentials → join the shared Agora voice channel
        │
        ▼
Speech is transcribed by Agora, delivered to our webhook, resolved to a
participant, and committed as a discussion event (see §4-5)
        │
        ▼
Discussion completes (time limit, HR finish, or moderator END_DISCUSSION)
        │
        ▼
Moderator agent stopped; each participant's individual report is generated on
demand from the shared event log (see §7)
```

Every state transition (`create`, `invite`, `accept`, `control`, `attendance`,
`message`) is a Postgres RPC (`migrations/20260909_04_group_discussion.sql`) that
enforces its own authorization, idempotency (via `request_id`/`request_hash`), and
revision checks — this predates the voice work and was not changed.

## 2. RTC architecture

A GD channel is **not** N independent one-candidate-to-AI conversations. It is one
Agora RTC channel (`gd-<session_id>`) shared by every joined participant and the
moderator agent, matching the existing `AgoraAgentService` join payload's
`remote_rtc_uids: ["*"]` (already used for Standard Interview, now doing real work
because more than one non-agent UID is actually present in a GD channel).

```
Candidate A ─┐
Candidate B ─┼──► Agora RTC channel "gd-<id>" ◄── GD Moderator (gd_mod)
Candidate C ─┘
```

* **Existing agent, reused, not duplicated.** The moderator is the `gd_mod` logical
  agent already registered in `app.agents.registry` (added by an earlier commit,
  mapped to a real Agora Agent Studio pipeline — see `AGORA_INTEGRATION.md`). GD
  dispatch calls the exact same `AgoraAgentService.start_interview_agent()` Standard
  Interview uses for Alex/Jordan, with `agent_id="gd_mod"`. No new Agora resource is
  created by this work.
* **Dynamic system prompt.** `AgoraAgentService.start_interview_agent()` gained an
  optional `system_prompt_override` parameter (Alex/Jordan never pass it, so their
  behavior is unchanged byte-for-byte). GD uses it to give the live moderator the
  session's actual topic, competencies, and duration —
  `GDService._moderator_system_prompt()`. Company-policy excerpts are deliberately
  **excluded** from the live voice prompt; see §6.
* **Idempotent dispatch.** `GDService._start_moderator()`/`_stop_moderator()` track
  the dispatched `agora_agent_id` in `gd_sessions.moderator_state` (an existing
  jsonb column) and no-op if already started/stopped — starting the discussion
  twice, or a retried request, never spawns a second moderator agent.

## 3. Participant identity and RTC UIDs

`app/group_discussion/identity.py` deterministically derives each participant's RTC
UID from `(session_id, participant_id)` (CRC32-based, collision-probed). No schema
migration or extra write path is needed — the backend can always recompute the
mapping from the existing participant roster, and it is stable for the life of the
session. UID `998877` is reserved for the moderator and never assigned to a human.

```
participant_id  →  candidate_id (existing gd_participants row)
       │
       ▼ (deterministic hash, session-scoped)
   rtc_uid       →  what Agora reports on the wire
```

`GDService.rtc_credentials()` issues a scoped Agora RTC+RTM token
(`RtcTokenBuilder2.build_token_with_uid`, the same token builder Standard Interview
already uses) for the caller's own resolved participant identity only — a
participant can never request credentials for someone else's UID, and an
unauthenticated caller gets nothing (route requires `current_actor` + a `joined`
participant row).

## 4. Speaker-aware transcripts

Speaker attribution **must** come from Agora's own ASR pipeline, not a browser
client's self-report — any participant's client could otherwise forge a "final"
contribution attributed to someone else. `POST /group-discussions/{id}/agora-webhook`
is a server-to-server endpoint (verified via the `Agora-Signature-V2` HMAC header
when `AGORA_NOTIFICATION_SECRET` is configured — see §8) that Agora calls with
per-turn transcript data. `app/group_discussion/webhook.py` normalizes that payload
into `VoiceTranscriptIn { rtc_uid, turn_id, text, is_final }`, and
`GDService.ingest_voice_turn()`:

1. Resolves `rtc_uid → participant_id` via `identity.resolve_speaker()` against the
   *current* joined roster (a UID belonging to a removed/left participant, or nobody,
   resolves to nothing and the turn is dropped — never mis-attributed).
2. Commits the turn through a dedicated RPC, `append_gd_voice_message`
   (`migrations/20260912_01_gd_voice.sql`) — **not** the existing
   `append_gd_message`, because that RPC authenticates by resolving a JWT actor's
   email to a candidate row, and a voice turn arrives with no per-request user JWT
   at all. The new RPC independently re-validates the participant is `joined` and
   the session is `active`, exactly like the authenticated path does.
3. Reuses the existing analysis/evidence pipeline unchanged
   (`GDProcessing.drain()`), so voice-sourced and text-sourced contributions are
   evaluated identically.

```json
{
  "speaker_id": "candidate_123",      // gd_participants.candidate_id
  "rtc_uid": 456123,
  "turn_id": "agora-turn-9f2",
  "text": "I think the biggest issue is...",
  "final": true
}
```

**Partial transcripts.** `ingest_voice_turn()` ignores every `is_final=false`
segment outright — Agora's live subtitle stream retransmits growing prefixes of the
same utterance, and only the final revision is a real contribution. `turn_id`
(Agora's own stable per-utterance id) is the sole idempotency key, mapped to a
deterministic `request_id = uuid5(session_id + ":gd-voice-turn:" + turn_id)`; a
retried or duplicated webhook delivery for the same turn commits exactly once
(enforced by the RPC's `UNIQUE(session_id, request_id)` constraint, the same
mechanism the text path already relies on). **Timestamps are never used as a
dedup key.**

**Known limitation — see the root-cause report.** The exact field Agora uses to
carry a per-turn speaker UID in a *multi-human-participant* Conversational AI
channel has not been observed against live 2+ participant traffic in this
environment. `webhook.py` tries every plausible field name
(`uid`/`user_id`/`speaker_uid`/`stream_id`/`rtc_uid`) and is covered by unit tests
against synthetic payloads, but this must be confirmed against a real multi-party
Agora session before the attribution is relied on for grading-grade evidence.

## 5. Discussion and participant state

Unchanged from the existing text pilot — this was already correctly built:
`app.intelligence.group_discussion.signals.participation_signals()` computes
per-participant turn counts, speaking/contribution counts, and discussion-level
`time_remaining_seconds`/`total_turns`; `GDModerator.decide()`
(`app/group_discussion/moderator.py`) is a deterministic state machine (time
warnings, imbalance detection, stagnation detection) that only escalates to an LLM
call (`GDIntelligence.analyze`) for semantic judgment (off-topic/hostile/unclear),
not for every transcript fragment. Voice changes nothing here except that
contributions can now arrive from the webhook path in addition to the typed-message
path — both funnel into the same `gd_events` table and the same `drain()` pipeline.

`GDService.view()` additionally now exposes, per participant, `rtc_uid` (when voice
is available) and a session-level `moderator_agent_id` flag, so the frontend can
render live speaking indicators and moderator-connected status without
guessing.

## 6. Company-policy context

Unchanged and preserved as-is: `GDProcessing._policy_context` pins the *first*
usable authorized snapshot for the whole discussion (immutable for that session)
and every scripted moderator line for a policy-grounded action comes from
`GDResponseGenerator.policy_response()` — never free-form generation — exactly as
before. The **live voice moderator's system prompt deliberately excludes policy
excerpts** (see §2) so the real-time persona can never improvise or misquote
grounded policy; only the asynchronous, evidence-backed pipeline speaks for policy.

## 7. Evaluation and persistence

Unchanged: evaluation stays individual even though the discussion is shared
(`ground_findings(..., participant_id=...)`), evidence is attributed to a specific
candidate/turn/competency, and `publish_gd_report`'s SQL independently re-derives
the score from stored evidence rather than trusting a client-submitted number.
Nothing about persistence, the Knowledge Graph integration, or reporting was
touched by the voice work — it already worked on top of `gd_events`, and voice
turns are just another way `gd_events` rows get created.

## 8. Environment configuration

| Variable | Purpose | Required | Used by |
|---|---|---|---|
| `AGORA_APP_ID` / `AGORA_APP_CERTIFICATE` | RTC/RTM token signing, dispatch auth | Yes, for voice | `identity`/`rtc_credentials`/`AgoraAgentService` |
| `AGORA_GD_MOD_PIPELINE_ID` | Existing Agora Agent Studio pipeline for the moderator | No (has a verified default) | `app.agents.gd_mod` |
| `AGORA_CUSTOMER_ID`/`AGORA_CUSTOMER_SECRET` | Preferred auth for the Conversational AI REST API | No (falls back to a per-request token) | `AgoraAgentService` |
| `AGORA_NOTIFICATION_SECRET` | HMAC-verifies the GD/Role Play voice webhooks | No — verification is skipped (with a log) if unset, matching this codebase's existing degrade-gracefully pattern for optional secrets | `group_discussion/webhook.py` |

If `AGORA_APP_ID`/`AGORA_APP_CERTIFICATE` are blank, `AgoraGDRealtimeAdapter.describe()`
reports `voice_available: false` and the discussion runs exactly as the original
text pilot did — no code path assumes Agora is configured.

## 9. Failure handling

* A failed moderator dispatch (`_start_moderator`) never blocks the discussion from
  starting — it's logged and the discussion proceeds; a participant's next
  `rtc_credentials` call retries the dispatch (idempotent).
* A participant disconnecting from Agora RTC does not end the session for anyone
  else — GD session state lives in Postgres, not in the RTC connection.
* Server-side participant-limit and quorum checks (already existing SQL) are
  untouched and still authoritative regardless of what the frontend shows.

## Verification status

**Verified in this pass:** unit tests for UID assignment/collision handling,
webhook signature verification, webhook payload parsing, moderator/voice dispatch
idempotency, RTC credential scoping, and voice-turn ingestion/dedup (all mocked —
no live Agora traffic); `npx tsc --noEmit` and `next build` clean for the frontend;
full existing backend regression suite re-run (see the root-cause report for the
27 pre-existing, unrelated failures observed and why they are not regressions).

**NOT verified — requires a live environment with real devices/browsers:**
multi-participant audio (2, 3, 4 simultaneous humans + moderator), the exact Agora
webhook field carrying a per-turn speaker UID in a multi-human channel (§4), live
moderator turn-taking behavior and response latency, and reconnect/network-loss
behavior under real conditions.
