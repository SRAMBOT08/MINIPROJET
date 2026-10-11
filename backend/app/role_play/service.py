import hashlib
import json
import structlog
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.agents.registry import agent_registry
from app.agents.role_play import ROLE_PLAY_PROFILE
from app.core.config import settings
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.integrations.aicredits_client import AICreditsError
from app.integrations.feature_db import execute, tenant_key
from app.voice.authorization import require_candidate, require_recruiter
from app.intelligence.role_play.analyzer import RolePlayIntelligence
from app.intelligence.core.evidence import ground_findings
from app.role_play.models import PersonaDefinition, ScenarioDefinition, RolePlayState, RolePlayAction, RolePlayActionType
from app.role_play.orchestrator import RolePlayOrchestrator
from app.role_play.realtime import AgoraRolePlayRealtimeAdapter
from app.role_play.response_generator import RolePlayResponseGenerator
from app.role_play.repository import RolePlayRepository
from app.role_play.reporting import RolePlayReporting, report_view

logger = structlog.stdlib.get_logger("intra_ai.role_play.service")
ROLE_PLAY_CHANNEL = "role_play"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class RolePlayService:
    def __init__(self, sb, client=None, realtime=None, policy_enricher=None):
        self.repo = RolePlayRepository(sb); self.client = client
        self.policy_enricher = policy_enricher
        self.intelligence = RolePlayIntelligence(client); self.orchestrator = RolePlayOrchestrator()
        self.responses = RolePlayResponseGenerator(client); self.realtime = realtime or AgoraRolePlayRealtimeAdapter()

    async def save_definition(self, actor, kind, body, key=None):
        require_recruiter(actor)
        if key:
            old = await self.repo.definition(actor, key, kind)
            if not old: raise NotFoundError("Role-play definition not found")
        if kind == "scenario":
            persona = await self.repo.definition(actor, str(body.definition.persona_id), "persona")
            if not persona or persona.get("archived_at"): raise ValidationError("Choose an active persona from your workspace")
        key = key or str(uuid5(NAMESPACE_URL, actor.user_id + ":" + kind + ":" + str(body.request_id)))
        return await self.repo.save_definition(actor, key, kind, body, fingerprint(body.definition.model_dump(mode="json")))

    async def require_session(self, actor, key, *, candidate_turn=False):
        row = await self.repo.session(key)
        if not row: raise NotFoundError("Role-play session not found")
        if actor.role == "candidate":
            if str(row["candidate_id"]) != actor.candidate_id: raise NotFoundError("Role-play session not found")
        elif candidate_turn or str(row["created_by"]) != actor.user_id or row["tenant_key"] != tenant_key(actor):
            raise ForbiddenError("Role-play session is outside your workspace")
        else: require_recruiter(actor)
        return row

    @staticmethod
    def compatible_agents():
        """Agents usable as the Role Play voice persona -- the AgentRegistry, filtered to
        the "role_play" channel, never a hardcoded list. See app.agents.registry."""
        return agent_registry.list_agents_for_channel(ROLE_PLAY_CHANNEL)

    async def create(self, actor, body):
        require_recruiter(actor)
        agent_id = (body.agent_id or "role_play").strip().lower()
        if agent_id not in {a.agent_id for a in self.compatible_agents()}:
            raise ValidationError(f"'{agent_id}' is not a configured Role Play voice agent")
        candidate = await require_candidate(actor, body.candidate_id, self.repo.sb)
        scenario = await self.repo.definition(actor, str(body.scenario_id), "scenario")
        if not scenario or scenario.get("archived_at"): raise NotFoundError("Active scenario not found")
        definition = ScenarioDefinition.model_validate(scenario["definition"])
        persona = await self.repo.definition(actor, str(definition.persona_id), "persona")
        if not persona or persona.get("archived_at"): raise ValidationError("Scenario persona is unavailable")
        person = PersonaDefinition.model_validate(persona["definition"])
        state = RolePlayState(escalation=person.initial_escalation, unresolved_objections=person.concerns[:10], agent_id=agent_id)
        parsed = candidate.get("parsed_resumes") or []
        claims = parsed[0] if isinstance(parsed,list) and parsed else parsed if isinstance(parsed,dict) else {}
        profile = {key: claims.get(key, [])[:10] for key in ("skills", "experience", "education", "projects")}
        payload = {"id": str(uuid5(NAMESPACE_URL, actor.user_id+":roleplay-session:"+str(body.request_id))),
            "scenario_id": str(body.scenario_id), "candidate_id": body.candidate_id,
            "scenario_snapshot": {"id": scenario["id"], "revision": scenario["revision"], "definition": definition.model_dump(mode="json")},
            "persona_snapshot": {"id": persona["id"], "revision": persona["revision"], "definition": person.model_dump(mode="json")},
            "candidate_profile": profile, "state": state.model_dump(mode="json"), "request_id": str(body.request_id),
            "request_hash": fingerprint(body.model_dump(mode="json"))}
        return await self.repo.create_session(actor, payload)

    async def view(self, actor, key):
        row = await self.require_session(actor,key)
        events = await self.repo.events(key)
        return self.project(row, actor, events)

    def project(self, row, actor, events=None):
        scenario = row["scenario_snapshot"]["definition"]
        state = row["state"]; phase = scenario["phases"][state["phase_index"]]
        elapsed = (datetime.now(timezone.utc)-datetime.fromisoformat(state["started_at"].replace("Z","+00:00"))).total_seconds() if state.get("started_at") else 0
        result = {"id": row["id"], "title": scenario["title"], "description": scenario["description"],
            "candidate_role": scenario["candidate_role"], "persona_name": row["persona_snapshot"]["definition"]["name"],
            "status": row["status"], "revision": row["revision"], "phase": {"title": phase["title"], "brief": phase["brief"]},
            "turn_count": state["turn_count"], "time_remaining_seconds": max(0,round(scenario["duration_seconds"]-elapsed)) if row["status"] not in {"completed","cancelled"} else 0,
            "report_status": row["report_status"], "report": report_view(row.get("report"),actor.role=="candidate"),
            "agent_id": state.get("agent_id","role_play"),
            "realtime": self.realtime.describe(row["id"]), "created_at": row["created_at"]}
        if actor.role != "candidate": result["candidate_id"] = str(row["candidate_id"])
        if events is not None:
            result["events"] = [{"id":event["id"],"revision":event["revision"],"kind":event["kind"],
                "text":event["source_text"],"response":event["response"],"created_at":event["created_at"],
                "analysis_status":event["analysis"].get("status","unavailable"),"policy_context":event.get("policy_context")} for event in events]
        return result

    async def _replay(self, key, request_id, request_hash):
        event = await self.repo.event_request(key,str(request_id))
        if event and event["request_hash"] != request_hash: raise ConflictError("Request ID belongs to another event")
        return event

    async def control(self, actor, key, body):
        row = await self.require_session(actor,key, candidate_turn=body.action=="start")
        request_hash = fingerprint({"kind": body.action})
        if await self._replay(key,body.request_id,request_hash): return await self.view(actor,key)
        state = RolePlayState.model_validate(row["state"]); now=datetime.now(timezone.utc)
        scenario = ScenarioDefinition.model_validate(row["scenario_snapshot"]["definition"])
        if body.action=="start":
            if state.status!="assigned": raise ConflictError("This scenario has already started")
            state.status="active"; state.started_at=now; state.revealed_keys=scenario.phases[0].reveal_keys[:]
            response=scenario.phases[0].brief
        elif body.action in {"finish","cancel"}:
            if state.status not in {"assigned","active"}: raise ConflictError("This scenario has ended")
            state.status="completed" if body.action=="finish" else "cancelled"; state.ended_at=now; state.completion_reason="participant_finished" if actor.role=="candidate" else "recruiter_finished"
            response="The simulation has ended."
        event={"id":str(uuid4()),"kind":body.action,"request_id":str(body.request_id),"request_hash":request_hash,
            "source_text":"","response":response,"analysis":{"status":"not_applicable"},"evidence":[],"action":{},"policy_context":None}
        await self.repo.commit(actor,key,body.expected_revision,event,state)
        if body.action=="start":
            await self._start_voice_agent(key,state,scenario,person=PersonaDefinition.model_validate(row["persona_snapshot"]["definition"]),phase_brief=response)
        elif body.action in {"finish","cancel"}:
            await self._stop_voice_agent(key,state)
        return await self.view(actor,key)

    # ── Realtime voice (Agora) ────────────────────────────────────────────────

    def _channel_name(self,key):return f"roleplay-{key}"

    def _system_prompt(self,state,scenario,person,phase):
        restricted="; ".join(person.restricted_information) or "none stated"
        allowed="; ".join(person.allowed_information) or "no specific constraints"
        return (
            f"{ROLE_PLAY_PROFILE.instructions}\n\n"
            f"You are: {person.name}. Personality: {person.personality}. Communication style: {person.communication_style}.\n"
            f"Scenario: {scenario.title} -- the candidate is acting as {scenario.candidate_role}.\n"
            f"Current phase: {phase.title}. {phase.brief}\n"
            f"You may reveal: {allowed}. You must NOT reveal: {restricted}.\n"
            f"Current escalation level: {state.escalation}/5 ({state.stance}).\n"
            "Keep every spoken turn short (one or two sentences) -- this is a live voice conversation, not written prose."
        )

    async def _start_voice_agent(self,key,state,scenario,person,phase_brief):
        if not self.realtime.describe(key).get("voice_available"):return
        if state.agora_agent_id:return
        from app.services.agora_agent_service import agora_agent_service
        phase=scenario.phases[state.phase_index]
        try:
            result=await agora_agent_service.start_interview_agent(
                interview_id=self._channel_name(key),agent_id=state.agent_id or "role_play",
                greeting_text=phase_brief,system_prompt_override=self._system_prompt(state,scenario,person,phase),
            )
            if result.get("status")=="started" and result.get("agora_agent_id"):
                state.agora_agent_id=result["agora_agent_id"]
                await execute(self.repo.sb.table("roleplay_sessions").update({"state":state.model_dump(mode="json")}).eq("id",key))
            else:
                logger.error("roleplay_voice_start_failed",session_id=key,result=result)
        except Exception as exc:
            # A failed voice dispatch must not block the text-mode simulation from starting.
            logger.error("roleplay_voice_start_error",session_id=key,error=str(exc))

    async def _stop_voice_agent(self,key,state):
        if not state.agora_agent_id:return
        from app.services.agora_agent_service import agora_agent_service
        try:
            await agora_agent_service.stop_interview_agent(interview_id=self._channel_name(key),agent_id=state.agent_id or "role_play",agora_agent_id=state.agora_agent_id)
        except Exception as exc:
            logger.error("roleplay_voice_stop_error",session_id=key,error=str(exc))
        finally:
            # Cleared even on a failed stop call (Agora idle_timeout reclaims the agent
            # regardless) so a completed/cancelled session never retries a stop call on
            # every subsequent read, and never looks "still connected" to the UI.
            stopped_state=state.model_copy(update={"agora_agent_id":None})
            try:await execute(self.repo.sb.table("roleplay_sessions").update({"state":stopped_state.model_dump(mode="json")}).eq("id",key))
            except AppError:pass

    async def rtc_credentials(self,actor,key):
        row=await self.require_session(actor,key)
        if actor.role!="candidate" or str(row["candidate_id"])!=actor.candidate_id:raise ForbiddenError("Only the assigned candidate can join by voice")
        realtime=self.realtime.describe(key)
        if not realtime.get("voice_available"):raise ValidationError("Voice is not available for this session")
        app_id=(settings.AGORA_APP_ID or "").strip();app_certificate=(settings.AGORA_APP_CERTIFICATE or "").strip()
        if not app_id or not app_certificate:raise ValidationError("Agora is not configured on this server")
        state=RolePlayState.model_validate(row["state"])
        if state.status!="active":raise ValidationError("Start the simulation before requesting voice credentials")
        from app.core.agora_token2 import RtcTokenBuilder2
        channel=self._channel_name(key)
        token=RtcTokenBuilder2.build_token_with_uid(app_id=app_id,app_certificate=app_certificate,channel_name=channel,
            uid=0,role=1,expire_seconds=3600,rtm_user_id=f"cand_{row['candidate_id']}")
        return {"agora_app_id":app_id,"agora_token":token,"agora_uid":0,"channel_name":channel,
            "rtm_user_id":f"cand_{row['candidate_id']}","expires_in":3600}

    async def turn(self, actor, key, body):
        row=await self.require_session(actor,key,candidate_turn=True)
        request_hash=fingerprint({"kind":"turn","text":body.text})
        if await self._replay(key,body.request_id,request_hash): return await self.view(actor,key)
        if row["revision"]!=body.expected_revision: raise ConflictError("Another turn arrived; refresh the session")
        event,new_state=await self._build_turn_event(row,str(body.request_id),request_hash,body.text)
        await self.repo.commit(actor,key,body.expected_revision,event,new_state)
        if new_state.status=="completed":
            # The orchestrator can end a scenario on its own (escalation limit, time/turn
            # limit, objectives demonstrated) without ever going through control(finish) --
            # the voice agent must be stopped here too, not only on an explicit control call.
            await self._stop_voice_agent(key,new_state)
        return await self.view(actor,key)

    async def ingest_voice_turn(self,key,text,turn_id):
        """Commit one final voice turn from the Agora RTC channel (see app.role_play.webhook).

        There is no per-request user JWT for a voice-originated turn -- see the
        append_roleplay_voice_turn migration comment for why an actor identity cannot
        be resolved in general for this path. `turn_id` (Agora's own stable per-utterance
        id, never a timestamp) is the sole idempotency key.
        """
        if not text.strip():return {"status":"ignored"}
        row=await self.repo.session(key)
        if not row:raise NotFoundError("Role-play session not found")
        state=RolePlayState.model_validate(row["state"])
        if state.status!="active":return {"status":"ignored","reason":"session_not_active"}
        request_id=str(uuid5(NAMESPACE_URL,key+":roleplay-voice-turn:"+turn_id))
        request_hash=fingerprint({"kind":"voice_turn","turn_id":turn_id,"text":text})
        existing=await self.repo.event_request(key,request_id)
        if existing:return {"status":"duplicate"}
        event,new_state=await self._build_turn_event(row,request_id,request_hash,text)
        await self.repo.append_voice_turn(key,event,new_state)
        if new_state.status=="completed":
            await self._stop_voice_agent(key,new_state)
        return {"status":"committed"}

    async def _build_turn_event(self,row,request_id,request_hash,text):
        key=row["id"]
        state=RolePlayState.model_validate(row["state"])
        if state.status!="active": raise ConflictError("Start an active session before responding")
        scenario=ScenarioDefinition.model_validate(row["scenario_snapshot"]["definition"])
        persona=PersonaDefinition.model_validate(row["persona_snapshot"]["definition"])
        now=datetime.now(timezone.utc)
        if state.started_at and (now-state.started_at).total_seconds()>=scenario.duration_seconds:
            raise ConflictError("The time limit has elapsed. Finish the session to review your feedback")
        phase=scenario.phases[state.phase_index]; event_id=str(uuid4())
        events=await self.repo.events(key)
        recent=[{"candidate":e["source_text"],"persona":e["response"]} for e in events[-4:]]
        policy_context = await self._policy_context(row, scenario, events)
        analysis=None; evidence=[]; error=None
        try:
            analysis=await self.intelligence.analyze(text=text,scenario=scenario,phase=phase,recent_turns=recent,
                                                     policy_context=policy_context)
            evidence=ground_findings(analysis.findings,text=text,competencies=scenario.competencies,
                event_id=event_id,candidate_id=str(row["candidate_id"]),session_id=key,source_type="role_play",observed_at=now,phase_id=phase.id)
        except AICreditsError as exc:
            analysis=None; error=exc.code
        new_state,action=self.orchestrator.decide(state,persona,scenario,analysis,now=now)
        try:
            response = self.responses.policy_response(action) if scenario.policy_query else await self.responses.generate(persona,scenario,new_state,action,text,recent)
        except AICreditsError: response="Please explain the next step you would take in this situation."
        event={"id":event_id,"kind":"turn","request_id":request_id,"request_hash":request_hash,
            "source_text":text,"response":response,"action":action.model_dump(mode="json"),
            "analysis":{"status":"evaluated" if analysis and evidence else "unavailable","error_code":error,
                        "signals":[s.model_dump(mode="json") for s in analysis.signals] if analysis else []},
            "evidence":[e.model_dump(mode="json") for e in evidence],"policy_context":policy_context}
        return event,new_state

    async def _policy_context(self, row, scenario, events):
        if not scenario.policy_query:
            return None
        # Pin the first usable source snapshot, including across process restarts.
        for event in events:
            context = event.get("policy_context")
            if isinstance(context, dict) and context.get("status") == "available":
                return context
        from app.core.config import settings
        if settings.COMPANY_KNOWLEDGE_ENABLED:
            from app.company_knowledge.grounding import CompanyPolicyEnricher
            try:
                enricher = self.policy_enricher or CompanyPolicyEnricher(self.repo.sb)
                context = await enricher.enrich(str(row["created_by"]), row["tenant_key"], scenario.policy_query)
                return context.model_dump(mode="json")
            except Exception:
                pass
        return {"source": "company_policy", "status": "unavailable", "excerpts": [],
                "effective_at": datetime.now(timezone.utc).isoformat(),
                "message": "Company policy is unavailable. Ask the company for clarification; do not assume a rule."}

    async def generate_report(self, actor, key):
        row=await self.require_session(actor,key)
        result=await RolePlayReporting(self.repo,self.client).generate(row,actor)
        return report_view(result,actor.role=="candidate")
