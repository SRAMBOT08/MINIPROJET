# Intra AI — Pitch Agent Knowledge Base

> **Purpose of this file.** This is end-to-end product and technical knowledge for a voice
> agent that will pitch Intra AI to judges and answer their questions live. It is written to
> be accurate first, impressive second — every claim below is sourced from the actual
> codebase and its own documentation, not from marketing language. Where something is
> partial, pending, or a known limitation, it is stated as such: a pitch agent that gets
> caught overclaiming loses more credibility than one that says "that part is still in
> progress" and moves on. Use the **Anticipated judge questions** section at the end as a
> rehearsal script.

---

## 1. What Intra AI is, in one breath

Intra AI is an AI-assisted recruitment and interview platform. Recruiters create jobs,
review parsed resumes, manage applications, schedule interviews, and get evidence-backed
reports. Candidates apply, join realtime **voice** interviews with AI interviewer agents,
and get feedback. Underneath the voice interface sits a genuine multi-agent decision
system — not a single LLM improvising a chat, but a pipeline that separates *understanding
an answer* from *deciding what happens next*, keeps a durable memory of what a candidate
has demonstrated across rounds, and only ever lets its interviewer agents take one of three
well-defined actions.

The one-sentence pitch: **"Intra AI turns a voice interview into a structured, adaptive,
evidence-producing conversation — instead of a scripted Q&A read aloud by a chatbot."**

## 2. The problem this solves

Two failure modes exist in the market today, and Intra AI is built to avoid both:

1. **Fully scripted AI interviews** — a fixed question list read aloud by a voice bot.
   These don't adapt to what the candidate actually said, can't probe a vague answer, and
   produce no defensible evidence trail — just a transcript and a guess at a score.
2. **Fully freeform LLM chat interviews** — a single large model both asks questions and
   decides the outcome in one undifferentiated pass. These are unpredictable, hard to
   audit, and can't explain *why* they moved on, switched interviewer, or ended the
   interview.

Intra AI's answer is architectural: split "understand the answer" (M1) from "decide what
happens next" (the Meta-Orchestrator), make every decision fall into exactly three typed
actions, and back every claim about a candidate with retrievable evidence in a knowledge
graph — not a vibe.

## 3. Who it's for

- **Recruiters / hiring teams** — create and publish jobs, review AI-scored applications,
  build reusable interview templates (rounds × agents × duration × focus), schedule or
  instantly invite candidates, review evidence-backed reports, and use **Morgan** (a voice
  assistant) to do dashboard-driven recruiting work hands-free.
- **Candidates** — apply, track applications, take the real Agora-voice interview with
  adaptive AI interviewers, receive a saved report and feedback, and separately practice
  with **Taylor** before the real thing.

---

## 4. Product surface — what's actually built

Current product status, straight from the repository's own README (not aspirational):

| Area | Status | What it does |
|---|---|---|
| Jobs and applications | **Implemented** | Recruiters create/publish jobs; candidates submit one application per job. |
| Resume ingestion | **Implemented** | PDF/text extracted and structured via AICredits (Gemini 3.1 Flash-Lite); bounded local extractor as fallback. |
| Eligibility scoring | **Implemented** | Deterministic skills/experience/education/project scoring sets application status; recruiters can override. |
| Scheduling | **Implemented** | Slots, reschedule, reusable templates, 10-minute instant interview invitations. |
| Official voice interview | **Implemented** | Candidate UI joins Agora RTC/RTM with registered interviewer agents. |
| Adaptive multi-agent interview | **Implemented** | M1 evaluates answers; Meta-Orchestrator selects questions, difficulty, handoffs, or completion. |
| Candidate memory | **Implemented** | Session context + Neo4j evidence memory feed subsequent turns and handoffs. |
| Recruiter report | **Implemented** | Evidence-backed report: coverage, per-round results, strengths, improvements, recommendation. |
| Candidate feedback | **Implemented** | Persisted 1–5 rating + three coaching lines on successful report generation. |
| Report PDF | **Partial** | API can redirect to an existing `pdf_url`; does not yet generate a new PDF artifact. |
| Morgan (recruiter assistant) | **Implemented, configuration-gated** | Voice assistant for scoped HR data, reviewed actions, email/calendar/Slack workflows, scheduling. |
| Taylor (candidate practice) | **Implemented, configuration-gated** | Practice assistant with CV/job context and end-of-practice coaching; never touches official scores. |
| Candidate onboarding | **Implemented (extension)** | Independent, versioned private CV profiles, reused across applications and Taylor general practice. |
| Company knowledge | **Implemented (extension)** | Versioned, effective-dated company documents; bounded, citation-preserving retrieval. |
| Role-play | **Implemented (extension)** | Reusable persona/scenario behavioral simulations, separate intelligence/orchestrator, own reports. |
| Group discussion | **Implemented (extension)** | Multi-candidate shared text sessions, hashed invitations, individual per-participant reports. |
| Hosted (production) activation | **Pending** | Local/dev verified; hosted Supabase migration for the four extension features not yet applied. AWS deployment explicitly paused pending authorization. |

**Honesty note for the agent:** if asked "is this live in production," the accurate answer
is: the core ATS + adaptive interview + Morgan + Taylor are implemented and locally/dev
verified with real provider integrations; the four newer extension features (onboarding,
company knowledge, role-play, group discussion) are implemented and passing local
verification, but the hosted database migration and AWS deployment for them have not been
executed yet. Say that plainly rather than implying everything is already live for real
users.

---

## 5. The core architecture — how one interview turn actually works

This is the heart of the product and the part worth explaining in the most technical
depth, because it's the actual differentiator.

### 5.1 The end-to-end loop

```
Candidate speaks
  → Agora RTC captures audio → VAD/turn detection → Deepgram nova-3 ASR
  → Agora calls the backend's OpenAI-compatible Custom LLM Adapter
    (POST /api/v1/chat/completions, also mounted at /v1/chat/completions)
  → Fast Path intent classification
  → [substantive answer only] M1 Interview Intelligence → AnswerAnalysis
  → InterviewAIContext updated (durable interview-local state)
  → Persistent candidate memory retrieved from Neo4j (evidence, not embeddings)
  → AgentTurnContext assembled (one bounded snapshot for this turn)
  → Meta-Orchestrator (LangGraph) reasons → NextAction
  → NextAction validated against its contract
  → ASK_QUESTION / SWITCH_AGENT / COMPLETE executed
  → Response text returned to Agora → OpenAI tts-1 voice → candidate hears it
  → loop repeats on the next answer, or the interview completes and a report is requested
```

Agora owns the realtime media path end-to-end — RTC transport, VAD, ASR, TTS. Intra AI does
**not** run its own realtime audio pipeline or an OpenAI Realtime WebSocket relay; it
receives a normal HTTP chat-completion request per turn and returns text.

### 5.2 Fast Path — most turns never touch a model

Every turn is classified into one of four intents before anything else happens:
`AUDIO_CHECK`, `TIME_PAUSE`, `REPEAT_QUESTION`, `INTERVIEW_ANSWER`. The first three are
control turns — "can you hear me," "give me a second," "can you repeat that" — and are
answered instantly from a fixed script, **without calling M1 or the Meta-Orchestrator at
all**. Only `INTERVIEW_ANSWER` proceeds into real reasoning. This exists because a
sub-second reply to "can you hear me?" matters for interview experience, and a model
round-trip there would just feel broken.

### 5.3 M1 — Interview Intelligence (understands the answer)

M1 evaluates the candidate's answer and produces a typed `AnswerAnalysis`:

- overall performance and confidence
- vague-answer detection and missing information
- contradiction detection (against what was said earlier, this round or a prior one)
- evidence items, each carrying answer/round/competency/source-agent provenance
- competency findings
- a recommended follow-up

**M1 does not choose the next question, switch agents, or end the interview.** That
separation is deliberate: an "understand" model and a "decide" model are different jobs
with different failure modes, and conflating them makes both harder to trust and to debug.

Model: **`google/gemini-3.1-flash-lite`**, routed through Intra AI's internal AICredits
gateway. The provider is pluggable (`M1_PROVIDER` env var — Gemini, Groq, Ollama, or a mock
all implement the same interface) but Gemini 3.1 Flash-Lite is the configured default.

### 5.4 InterviewAIContext — the interview's short-term memory

A single authoritative, mutable object per interview, updated once per turn:
`evaluated_competencies`, `accumulated_evidence`, `open_questions`,
`missing_competencies`, `detected_contradictions`, `question_history`, plus the active
round/agent/difficulty. This is not a log — it's the live state the rest of the pipeline
reasons from.

### 5.5 Persistent candidate memory — Neo4j, not a vector database

Before the orchestrator decides anything, relevant historical evidence about this
candidate is retrieved from a **Neo4j AuraDB knowledge graph** — typed nodes (`Candidate`,
`InterviewRound`, `Question`, `Answer`, `Evidence`, `Competency`, `Project`, `Skill`,
`Technology`) connected by typed relationships (participation, question→answer,
answer→evidence, evidence→competency, candidate→project→skill→technology).

This is a **deliberate architectural choice, worth stating explicitly if asked**: retrieval
is graph traversal, not semantic/vector similarity search. Every fact handed back to an
agent is a specific, provenance-tagged prior answer — traceable to an exact round, question,
and source agent — not a "closest match" from an embedding index. For a system whose output
can affect a hiring decision, traceability was prioritized over the recall flexibility a
vector store would add.

### 5.6 AgentTurnContext — one bounded snapshot per turn

Everything above — CV facts, job description, persistent memory, live `InterviewAIContext`,
the active agent's profile, the current Q&A, and M1's `AnswerAnalysis` — is assembled into
exactly one bounded object, rebuilt fresh every turn. It is explicitly **not** a new source
of truth; CV facts and verified interview evidence are kept visibly distinct inside it,
never merged into one undifferentiated blob (a candidate's resume claim is not the same
thing as something they actually demonstrated in the interview).

### 5.7 The Meta-Orchestrator — where the routing decision happens

A **LangGraph** state machine (`orchestrator/graph.py`) is the only place in the pipeline
where an LLM chooses what happens next:

```
validate_state → analyze_decision_state
  → (route) → build_complete_action ──────────────┐
  → (route) → pre_nemotron_guardrails               │
        → (route) → [model reasoning call] ─────┐   │
        → (route) → build_action ────────────────┼───┤
                                                  ▼   ▼
                                             build_action → validate_action → END
```

Two deterministic shortcuts exist on purpose: if the interview state already makes
completion obvious, the graph builds a `COMPLETE` action without ever calling the model;
if the guardrail stage already forces an outcome, it goes straight to building the action.
**The model is not called on every turn** — only when a genuine judgment call is needed.
Invalid model output, invalid targets, premature completion, or a provider failure all fall
back to a deterministic policy rather than producing an unvalidated action.

Model for the reasoning step: **`google/gemini-3.1-flash-lite`** via the same AICredits
gateway (`ORCHESTRATOR_PROVIDER=aicredits`).

### 5.8 NextAction — exactly three actions, always validated

```
ActionType: ASK_QUESTION | SWITCH_AGENT | COMPLETE
```

Fields: `target_agent_id`, `competency`, `difficulty`, `question_text`, `rationale`.
Difficulty, competency, and question text are **metadata on an action**, never a fourth
action of their own — a follow-up or clarification is just `ASK_QUESTION` with different
metadata. `validate_action` enforces the contract in code: for example, `SWITCH_AGENT` can
never target the agent that is already speaking, and `COMPLETE` requires actual progress.

### 5.9 Agent registry — not a fixed Alex-then-Jordan script

Agent selection is **registry-based**, not a hardcoded two-step pipeline. The default
registry ships two official interview agents:

| Agent | Role | Focus | Difficulty range |
|---|---|---|---|
| **Alex** | Technical Manager | System design, architecture, coding/problem-solving, scalability, debugging, reliability, security, performance, testing, deployment | Easy → Expert |
| **Jordan** | Senior Product Manager | Product sense, customer understanding, prioritization, strategy, trade-offs, metrics, validation, stakeholder communication | Easy → Expert |

Each `AgentProfile` defines identity, focal competencies, questioning style, instructions,
difficulty bounds, and allowed actions; a separate `AgoraAgentMapping` binds it to its Agora
project/pipeline, RTC UID, ASR/TTS configuration, greeting, and voice. **Interview
configuration is two independent dimensions** — rounds and agents — supporting 1×1, 1×N,
N×1, and N×N combinations (one agent across many rounds, many agents in one round, etc.),
snapshotted into reusable templates. On a handoff, the receiving agent inherits the full
accumulated `InterviewAIContext` — **no context reset.**

### 5.10 Post-interview reporting

On completion, the report service: collects saved configuration, answers, evaluations,
transcripts, and graph-backed recovery evidence → requires at least two evaluated answers
and validated evidence provenance → computes an overall score from saved evaluation
evidence → maps it to a 1–5 candidate rating (`round(1 + overall_score / 25, 1)`) → asks
**AICredits GPT-5 Nano** (`openai/gpt-5-nano`) to format the recruiter narrative *without
rescoring* → asks **AICredits Gemini 2.5 Flash-Lite** for two candidate-facing coaching
sentences → prepends a deterministic performance-band sentence → atomically persists
everything in Supabase. Recruiters see evidence-oriented analysis; candidates see only a
safe rating and feedback — **a report is never presented as ready if evidence or provider
output fails validation.**

---

## 6. Morgan — the recruiter's voice assistant

Morgan lives in the recruiter dashboard ("Ask Morgan") and is architecturally a completely
different trust model from the interview pipeline above, because it acts on real production
data.

- **Model:** Agora-managed `gpt-4.1-mini` (a different model from M1/Orchestrator's Gemini
  — chosen specifically for this conversational-tool-use job), on an **isolated Agora
  project**, separate from the official interview product's Agora project.
- **Tool access via MCP (Model Context Protocol):** when a session starts, the backend
  mints a short-lived, session/user/persona-bound MCP bearer token (a different audience
  from a normal login token — it can't be used to sign into the app, and expires with the
  session). Agora's join call is told to attach this as an MCP server pointer
  (`llm.mcp_servers`), and Agora's own MCP client then talks directly to Intra AI's own
  JSON-RPC endpoint (`POST /voice/mcp`, server name `IntraAssistantTools`) — doing
  `initialize` → `tools/list` → `tools/call`.
- **~26 tools, persona-gated:** reads (search/get candidates, applications, interviews,
  reports, available slots, jobs, recruiting overview, templates…) execute immediately;
  every **write** (`schedule_interview`, `bulk_shortlist_candidates`,
  `update_application_status`, `send_candidate_email`, …) returns only an **immutable,
  5-minute reviewable proposal**. The model has no confirm tool — a spoken "yes" alone can
  never execute anything. The recruiter reviews and clicks **Confirm Action**; execution
  then goes through a Redis one-time claim so a retried confirmation can't double-book or
  double-send.
- **A second, narrower connection for the outside world:** Gmail, Google Calendar, and
  Slack aren't Intra AI's data, so Morgan doesn't reach them directly at all. The
  **backend** (never the model) opens a separate, server-only MCP client connection to a
  **Composio-hosted Tool Router**, allow-listing exactly eight remote operations
  (`GMAIL_GET_PROFILE`, `GOOGLECALENDAR_LIST_CALENDARS`, `SLACK_TEST_AUTH`,
  `SLACK_LIST_CONVERSATIONS` for reads; `GMAIL_CREATE_EMAIL_DRAFT`, `GMAIL_SEND_EMAIL`,
  `GOOGLECALENDAR_CREATE_EVENT`, `SLACK_SEND_MESSAGE` for writes), bound to one specific
  recruiter's connected account. The model never sees Composio's broader catalogue or its
  credentials.

## 7. Taylor — the candidate's practice coach

Taylor is architecturally the mirror image of Morgan: same voice infrastructure, zero live
tools, by explicit design.

- Candidate picks general practice or a specific application, optional target role,
  experience level. The backend loads the candidate's saved parsed CV and the selected job
  description **once**, before the call starts, and builds one bounded system prompt — with
  an explicit instruction to treat that CV/JD text as *untrusted data*, never as
  instructions (a prompt-injection guard, since resume content is attacker-reachable text).
- **No tools — enforced twice in code, not left to omission:** every Taylor join explicitly
  sets `enable_tools = False` and `mcp_servers = []`; passing any MCP parameter for Taylor
  is rejected before the Agora call even happens. The `/voice/mcp` endpoint independently
  hard-denies the Taylor persona too.
- Uses Taylor's own **saved Agora Studio model** (native, not the Agora-managed override
  Morgan uses) — once the conversation starts, Intra AI's backend is not in the loop at all
  for any individual turn: no Fast Path, no M1, no orchestrator.
- **Finish & Get Feedback:** stops the mic first, reads Taylor's own conversation history
  back from Agora, and asks that *same* native model — no second model, no new API key —
  for coaching through a single request. The response must contain 14 fixed markers in
  exact order (`INTRA_FEEDBACK_BEGIN` … summary, three scored criteria — clarity,
  relevance, specificity — one strength, one improvement, one grounded example …
  `INTRA_FEEDBACK_END`); malformed output is rejected outright, never repaired or guessed.
  The suggested example answer must reuse the candidate's own recorded sentences in
  original order — a rewrite that fails this check falls back to a deterministic template
  built from the candidate's own wording with explicit blanks, rather than ever showing an
  invented answer.
- Feedback lives in a **separate Redis namespace with a 24-hour TTL**, fully isolated from
  official scores, evidence, or the knowledge graph. **Practice is never a hiring signal.**

## 8. The four newer extension features

These sit beside the protected interview stack, not inside it — a deliberate isolation
boundary: the interview's M1, `InterviewAIContext`, `AgentTurnContext`, Meta-Orchestrator,
and `NextAction` are explicitly protected, and none of the new domains import them. Removing
all four extension routers would leave the interview flow fully operational.

- **Candidate onboarding** — candidate-owned, versioned private CV profiles, independent of
  any specific job application; a current-version pointer, replacement, and reuse (across
  applications and as Taylor's general-practice context) without ever requiring a job to
  onboard.
- **Company knowledge** — owner/tenant-scoped company documents with immutable versions and
  effective-dated windows (so policy that changed on a given date is retrievable
  correctly), bounded lexical retrieval, and citation-preserving answers. It is **not**
  automatically injected into the protected Standard Interview prompts — it's an
  independently callable, authorized enrichment endpoint.
- **Role-play** — reusable persona/scenario behavioral simulations with their own
  `RolePlayIntelligence` / `RolePlayOrchestrator` / `RolePlayAction` — deliberately not
  reusing M1 or the interview orchestrator, because "observe → decide → act" for a
  behavioral simulation is a different contract than for a structured interview.
- **Group discussion** — shared, multi-candidate text sessions with random/hashed/expiring/
  session-bound invitations, server-assigned event ordering, deterministic participation
  signals, its own moderator, and **individual, private per-candidate reports** — no
  participant sees another participant's private evaluation.

All four currently use **AICredits Gemini 3.1 Flash-Lite** in their respective model slots
(`AICREDITS_RESUME_MODEL`, `AICREDITS_COMPANY_MODEL`, `AICREDITS_ROLE_PLAY_MODEL`,
`AICREDITS_GD_MODEL`) and are feature-flagged
(`CANDIDATE_ONBOARDING_ENABLED`, `COMPANY_KNOWLEDGE_ENABLED`, `ROLE_PLAY_ENABLED`,
`GROUP_DISCUSSION_ENABLED`). New voice for role-play/GD is explicit future scope — their
current realtime adapters are text-only and report voice as unavailable rather than
pretending to support it.

---

## 9. Technology stack

| Layer | Implementation |
|---|---|
| Frontend | Next.js 16.1, React 19, TypeScript, Tailwind CSS 4, Radix UI, TanStack Query, Agora RTC/RTM browser SDKs |
| Backend | Python 3.11, FastAPI, Pydantic 2, HTTPX, LangGraph, structured logging |
| Application data | Supabase / PostgreSQL (via Supabase client + PostgREST) |
| Resume objects | Private Supabase Storage (optional S3-compatible code exists but isn't required) |
| Candidate memory | Neo4j AuraDB (graph, not vector) |
| Auxiliary state | Redis (Morgan/Taylor sessions, confirmation claims, expiry) |
| Voice | Agora Conversational AI + Agora Agent Studio |
| Realtime reasoning / reporting | AICredits — an internal OpenAI-compatible gateway with explicit per-feature model/key slots |
| ASR / TTS | Deepgram `nova-3` (ASR), OpenAI `tts-1` (TTS), via Agora's managed pipeline |
| Connected HR tools | Composio Tool Router (Morgan only, 8 allow-listed operations) |
| Email | Resend, when configured |
| Packaging | Dockerfiles (frontend/backend) + Docker Compose for local orchestration |

### Model matrix (exact, current defaults)

| Component | Provider | Model |
|---|---|---|
| Resume parsing | AICredits | `google/gemini-3.1-flash-lite` |
| M1 Interview Intelligence | AICredits | `google/gemini-3.1-flash-lite` |
| Meta-Orchestrator | AICredits | `google/gemini-3.1-flash-lite` |
| Recruiter report narrative | AICredits | `openai/gpt-5-nano` |
| Candidate feedback coaching | AICredits | `google/gemini-2.5-flash-lite` |
| Official interview voice | Agora Conversational AI | Deepgram `nova-3` ASR, OpenAI `tts-1` TTS |
| Morgan | Agora-managed | `gpt-4.1-mini` |
| Taylor | Agora Agent Studio (saved) | Studio-native model, identifier kept out of the repo |

Groq, Ollama, and plain Gemini API compatibility classes remain in source (used for tests
and explicit local configuration) but are **not** the active default provider path — if a
judge asks "why don't I see Groq/Nemotron," the honest answer is: those exist as swappable
fallback providers behind one interface, not as the shipped default.

---

## 10. Security & trust boundaries

- Every JWT is verified server-side and resolved to an active persisted user; role checks
  separate recruiter/admin from candidate operations.
- Recruiter access is **job-ownership scoped** — sharing a tenant does not grant access to
  another recruiter's jobs or candidates.
- Private resumes are served through authorized backend routes, never public object URLs.
- All provider credentials (Supabase service-role, Neo4j, Agora, AICredits, Composio,
  email) stay server-side; none are exposed through `NEXT_PUBLIC_*` variables.
- Knowledge-graph evidence always carries answer/round/competency/source-agent provenance —
  nothing is asserted about a candidate without a traceable source.
- Candidate-facing report endpoints project only candidate-safe feedback, never
  recruiter-only analysis.
- Morgan's MCP tokens have a distinct audience from login tokens and cannot be used to sign
  into the app; every write is a reviewed, human-confirmed, one-time-claimed action.
- Taylor's tool-free posture is enforced at two independent points in code, not left as an
  absence of configuration.

## 11. Deployment posture (say this honestly if asked "is this deployed")

- Container-ready: Dockerfiles for frontend/backend, `docker-compose.yml` for local
  orchestration, and a `docker-compose.production.yml` + Caddy config for production
  packaging.
- Intended topology: **one EC2 host** running the frontend + backend containers, plus Redis
  when Morgan/Taylor are enabled, connecting outward to Supabase, Neo4j AuraDB, Agora,
  AICredits, Composio, and Resend. **No ECS Fargate, ALB, CloudFront, RDS, or S3** are part
  of the current plan — this is intentionally a simple, single-host topology, not an
  over-engineered cloud footprint.
- **AWS deployment is explicitly paused** until application functionality and external
  integrations are fully validated and deployment is authorized — this is a stated project
  policy, not an oversight.
- The four extension features are implemented and pass local/dev verification with real
  provider integrations (live AICredits, private Supabase Storage, live AuraDB), but their
  hosted Supabase migration has not yet been applied — hosted activation for those four is
  the one explicitly pending step.

## 12. What makes this technically defensible (the "why," for skeptical judges)

1. **Understanding and deciding are different models with different jobs.** M1 only
   analyzes; the Meta-Orchestrator only routes. This isn't just clean code — it means a
   bug or hallucination in "did this answer show System Design skill" can't accidentally
   also corrupt "should we switch interviewers," because they're different calls with
   different contracts.
2. **The decision space is closed.** Exactly three actions, each schema-validated before
   execution. An LLM proposes; application code validates and enforces the contract before
   anything reaches the candidate.
3. **Memory is a graph with provenance, not a vector similarity guess.** Every fact handed
   to an interviewer about a candidate's past traces to a specific answer, in a specific
   round. That matters when the output can affect whether someone gets a job.
4. **The reasoning model isn't even always called.** The orchestrator short-circuits to a
   deterministic decision when the state already makes the outcome obvious — cheaper,
   faster, and more predictable than "ask the LLM every time."
5. **Trust is asymmetric by design, and enforced in code.** The same voice infrastructure
   runs three agents with three very different privilege levels — Alex/Jordan reason freely
   inside a validated contract; Morgan can act on real data but only after a human confirms;
   Taylor has zero live tools, enforced at two independent checkpoints, because practice
   must never leak into a hiring signal. That boundary isn't a policy doc — it's asserted in
   the request path itself.
6. **Extension features are architecturally quarantined.** Role-play, group discussion,
   company knowledge, and onboarding cannot import the protected interview internals at
   all — a codebase-level guarantee, verified by an actual import check, that new features
   can't silently destabilize the core interview product.

---

## 13. Anticipated judge questions — rehearsal answers

**"Is this just ChatGPT with a voice wrapper?"**
No — the interviewer's *voice* uses standard conversational AI infra (Agora + Deepgram +
OpenAI TTS), but the *decision* about what to ask next comes from a structured pipeline: a
dedicated answer-analysis model, a durable interview-state object, graph-retrieved
candidate memory, and a LangGraph orchestrator that can only emit one of three validated
actions. A single chatbot doesn't have that separation, that memory, or that contract.

**"What stops the AI from hallucinating a candidate's skills?"**
Two things. First, M1's evidence extraction is scoped to what was actually said in this
answer, with provenance attached. Second, the knowledge graph retrieval that feeds
candidate history back into the interview is deterministic graph traversal off real prior
answers — not a similarity search that can surface a plausible-sounding but wrong memory.

**"Why Neo4j and not a vector database?"**
Because the system needs to say *why* it believes something about a candidate, not just
*that* it's similar to something. A graph gives exact provenance — this competency claim
traces to this answer, in this round, from this interviewer. That traceability was chosen
over the extra recall flexibility a vector index would add.

**"What happens if the LLM gives a bad or invalid decision?"**
The Meta-Orchestrator's output is schema-validated before it ever executes — invalid
targets, an ineligible `SWITCH_AGENT`, or a malformed action all fall back to a
deterministic policy instead of reaching the candidate. The model proposes; code enforces.

**"Can Morgan (the recruiter assistant) take actions on its own?"**
No. Every write action — scheduling, status changes, emails — returns an immutable
proposal the recruiter has to read and explicitly confirm. The model itself has no confirm
tool; a spoken "yes" cannot execute anything.

**"Does candidate practice with Taylor affect their real interview score?"**
No, structurally not — Taylor's feedback lives in a separate Redis namespace with a 24-hour
TTL and never touches `InterviewAIContext`, the evidence pipeline, or the knowledge graph.

**"Is this deployed / production-ready right now?"**
The core ATS, the adaptive interview pipeline, Morgan, and Taylor are implemented and
verified with real provider integrations locally. Four newer extension features
(onboarding, company knowledge, role-play, group discussion) are implemented and passing
local verification but their hosted database migration hasn't been applied yet, and AWS
deployment is intentionally paused until that's done and explicitly authorized. That's a
sequencing decision, not a gap in the product.

**"What's the actual tech stack?"**
Next.js/React/TypeScript frontend; Python/FastAPI/Pydantic/LangGraph backend; Supabase/
PostgreSQL as the system of record; Neo4j AuraDB for candidate memory; Agora for realtime
voice (Deepgram ASR, OpenAI TTS); Gemini 3.1 Flash-Lite for interview intelligence and
orchestration via an internal AICredits gateway; Redis for assistant session state.

**"Why two different agents (Alex/Jordan) and not just one?"**
Because interviews aren't one competency — technical depth and product sense need
different interviewers with different focal competencies and questioning styles. The
architecture is registry-based specifically so this isn't limited to two: any number of
agents can be registered, and the orchestrator picks who's next based on live coverage
gaps, not a fixed order.

**"What was hardest to get right?"**
Keeping the decision boundary honest under real model unpredictability — making sure that
even when the reasoning model proposes something malformed, ambiguous, or out of contract,
the system degrades to a safe deterministic action instead of either crashing the interview
or silently doing the wrong thing. That's why `validate_action` and the guardrail stage
exist as first-class steps in the graph, not an afterthought.

---

*Sourced from: `intra-ai/README.md`, `docs/FEATURE_BLUEPRINT.md`, `docs/MORGAN_HR_WORKFLOWS.md`,
`docs/MORGAN_TAYLOR_DELIVERY.md`, `docs/TAYLOR_NATIVE_PRACTICE.md`, and the live backend
source (`app/orchestrator/graph.py`, `app/interview_intelligence/`, `app/interview_context/`,
`app/agent_context/`, `app/agents/`, `app/knowledge_graph/`, `app/voice/`,
`backend/.env.example`) as of the date this file was written. If the codebase changes,
re-verify the model matrix and status table before reusing this file.*
