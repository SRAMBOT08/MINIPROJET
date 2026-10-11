import base64
import hashlib
import hmac
import json
import structlog
from datetime import datetime,timedelta,timezone
from uuid import NAMESPACE_URL,uuid4,uuid5

from app.core.config import settings
from app.core.exceptions import AppError,ForbiddenError,NotFoundError,ValidationError
from app.integrations.feature_db import tenant_key
from app.voice.authorization import require_candidate,require_recruiter
from app.intelligence.group_discussion.signals import participation_signals
from app.agents.gd_mod import GD_MOD_PROFILE
from app.group_discussion.identity import assign_participant_uids,resolve_speaker
from app.group_discussion.realtime import AgoraGDRealtimeAdapter
from app.group_discussion.repository import GDRepository
from app.group_discussion.processing import GDProcessing
from app.group_discussion.reporting import GDReporting,report_view

logger=structlog.stdlib.get_logger("intra_ai.group_discussion.service")


def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()


class GDService:
    def __init__(self,sb,client=None,secret=None,policy_enricher=None):
        self.repo=GDRepository(sb);self.client=client;self.processing=GDProcessing(self.repo,client,policy_enricher)
        self.secret=secret if secret is not None else settings.GD_INVITATION_SECRET or settings.JWT_SECRET
        self.realtime=AgoraGDRealtimeAdapter()

    async def require_session(self,actor,key,owner_only=False):
        row=await self.repo.session(key)
        if not row:raise NotFoundError("Discussion not found")
        participants=await self.repo.participants(key)
        participant=None
        if actor.role=="candidate" and not owner_only:
            participant=next((p for p in participants if str(p["candidate_id"])==actor.candidate_id),None)
            if not participant or participant["status"]=="removed":raise NotFoundError("Discussion not found")
        else:
            require_recruiter(actor)
            if str(row["created_by"])!=actor.user_id or row["tenant_key"]!=tenant_key(actor):raise NotFoundError("Discussion not found")
        return row,participants,participant

    async def create(self,actor,body):
        require_recruiter(actor)
        payload={"id":str(uuid5(NAMESPACE_URL,actor.user_id+":gd-session:"+str(body.request_id))),
            "request_id":str(body.request_id),"request_hash":fingerprint(body.model_dump(mode="json")),
            "configuration":body.configuration.model_dump(mode="json")}
        return await self.repo.create(actor,payload)

    async def invite(self,actor,key,body):
        await self.require_session(actor,key,owner_only=True)
        candidate=await require_candidate(actor,body.candidate_id,self.repo.sb)
        if len(self.secret.encode())<32:raise ValidationError("Configure a GD invitation signing secret of at least 32 bytes")
        # Purpose-separated PRF permits reliable replay without storing plaintext tokens.
        raw=hmac.new(self.secret.encode(),("gd-invitation:"+key+":"+str(body.request_id)).encode(),hashlib.sha256).digest()
        token=base64.urlsafe_b64encode(raw).decode().rstrip("=")
        payload={"id":str(uuid5(NAMESPACE_URL,key+":"+body.candidate_id)),"candidate_id":body.candidate_id,
            "display_name":str(candidate.get("name") or "Participant").split()[0][:80],
            "token_hash":hashlib.sha256(token.encode()).hexdigest(),"request_id":str(body.request_id),
            "request_hash":fingerprint(body.model_dump(mode="json")),
            "expires_at":(datetime.now(timezone.utc)+timedelta(minutes=body.expires_minutes)).isoformat()}
        saved=await self.repo.invite(actor,key,payload)
        if saved["invitation_hash"]!=payload["token_hash"]:raise ValidationError("The invitation signing key changed. Issue a new invitation")
        return {"participant_id":saved["id"],"expires_at":saved["invitation_expires_at"],
            "join_url":settings.FRONTEND_URL.rstrip("/")+"/discussion-invite#session="+key+"&token="+token}

    async def accept(self,actor,key,body):
        if actor.role!="candidate" or not actor.candidate_id:raise ForbiddenError("A linked candidate account is required")
        await self.repo.accept(actor,key,hashlib.sha256(body.token.encode()).hexdigest(),str(body.request_id))
        return await self.view(actor,key)

    async def view(self,actor,key):
        row,participants,own=await self.require_session(actor,key)
        events=await self.repo.events(key) if own is None or own.get("accepted_at") else []
        signals=participation_signals(events,participants,started_at=row.get("started_at"),duration_seconds=row["configuration"]["duration_seconds"])
        config=row["configuration"]
        realtime=self.realtime.describe(key)
        uids=assign_participant_uids(key,[p["id"] for p in participants]) if realtime.get("voice_available") else {}
        result={"id":key,"title":config["title"],"topic":config["topic"],"status":row["status"],"revision":row["revision"],
            "max_participants":config["max_participants"],"min_participants":config["min_participants"],
            "duration_seconds":config["duration_seconds"],"time_remaining_seconds":signals["time_remaining_seconds"] if row["status"] not in {"completed","cancelled"} else 0,
            "realtime":realtime,"own_participant_id":own["id"] if own else None,
            "moderator_agent_id":"gd_mod" if (row.get("moderator_state") or {}).get("agora_agent_id") else None,
            "participants":[{"id":p["id"],"display_name":p["display_name"],"status":p["status"],"hand_raised":p["hand_raised"],
                **({"rtc_uid":uids[p["id"]]} if p["id"] in uids else {}),
                **({"candidate_id":str(p["candidate_id"]),"report_status":p["report_status"]} if own is None else {})} for p in participants],
            "events":[{"id":e["id"],"sequence":e["sequence"],"participant_id":e["participant_id"],"kind":e["kind"],"text":e["source_text"],
                "response":e["response"],"reply_to":e["reply_to"],"created_at":e["created_at"],"analysis_status":e["analysis"].get("status"),
                "policy_context":e.get("policy_context") or None} for e in events],
            "analysis_pending":sum(e["analysis"].get("status")=="pending" for e in events),
            "participation":signals["participants"].get(own["id"]) if own else signals,
            "report_status":own["report_status"] if own else None,"report":report_view(own.get("report"),True) if own else None}
        return result

    async def control(self,actor,key,body):
        before,_,_=await self.require_session(actor,key,owner_only=True)
        after=await self.repo.control(actor,key,body)
        if before["status"]!="active" and after["status"]=="active":
            await self._start_moderator(after)
        elif before["status"]=="active" and after["status"] in {"completed","cancelled"}:
            await self._stop_moderator(after)
        return await self.view(actor,key)

    # ── Realtime voice (Agora) ────────────────────────────────────────────────

    def _channel_name(self,key):return f"gd-{key}"

    def _moderator_system_prompt(self,configuration):
        # Deliberately excludes company-policy excerpts: the live voice persona must
        # never improvise or recite grounded policy text. Policy-aware, evidence-backed
        # responses stay in the asynchronous analysis pipeline (GDProcessing/GDResponseGenerator),
        # which pins and quotes the authorized snapshot. This keeps the two paths honest:
        # the voice moderator facilitates; only the grounded pipeline speaks for policy.
        competencies=", ".join(configuration["competencies"])
        return (
            f"{GD_MOD_PROFILE.instructions}\n\n"
            f"Discussion topic: {configuration['topic']}\n"
            f"Competencies being observed: {competencies}\n"
            f"Planned duration: {configuration['duration_seconds'] // 60} minutes.\n"
            "Keep every utterance to one or two short sentences. You are a live voice moderator, not a lecturer."
        )

    async def _start_moderator(self,session):
        if not self.realtime.describe(session["id"]).get("voice_available"):return
        if (session.get("moderator_state") or {}).get("agora_agent_id"):return
        from app.services.agora_agent_service import agora_agent_service
        configuration=session["configuration"]
        try:
            result=await agora_agent_service.start_interview_agent(
                interview_id=self._channel_name(session["id"]),
                agent_id="gd_mod",
                greeting_text=f"Welcome everyone. Today's topic is: {configuration['topic']}. Let's begin.",
                system_prompt_override=self._moderator_system_prompt(configuration),
            )
            if result.get("status")=="started" and result.get("agora_agent_id"):
                await self.repo.set_moderator_agent(session["id"],result["agora_agent_id"])
            else:
                logger.error("gd_moderator_start_failed",session_id=session["id"],result=result)
        except AppError:raise
        except Exception as exc:
            # A failed moderator dispatch must not block the human discussion from starting;
            # participants can still see/hear each other and text contributions still flow.
            logger.error("gd_moderator_start_error",session_id=session["id"],error=str(exc))

    async def _stop_moderator(self,session):
        agora_agent_id=(session.get("moderator_state") or {}).get("agora_agent_id")
        if not agora_agent_id:return
        from app.services.agora_agent_service import agora_agent_service
        try:
            await agora_agent_service.stop_interview_agent(
                interview_id=self._channel_name(session["id"]),agent_id="gd_mod",agora_agent_id=agora_agent_id,
            )
        except Exception as exc:
            logger.error("gd_moderator_stop_error",session_id=session["id"],error=str(exc))
        finally:
            # Cleared even on a failed stop call (Agora idle_timeout reclaims the agent
            # regardless) so a completed/cancelled discussion never retries a stop call
            # on every subsequent drain -- and never looks "still connected" to the UI.
            try:await self.repo.set_moderator_agent(session["id"],None)
            except AppError:pass

    async def rtc_credentials(self,actor,key):
        """Issue a scoped Agora RTC(+RTM) token for the caller's own participant identity.

        Never accepts a client-supplied UID or participant id: the caller's own
        joined participant row is the only identity resolved, exactly like every
        other candidate-facing GD action.
        """
        row,participants,own=await self.require_session(actor,key)
        if not own or own["status"]!="joined":raise ForbiddenError("Join the discussion before requesting voice credentials")
        realtime=self.realtime.describe(key)
        if not realtime.get("voice_available"):raise ValidationError("Voice is not available for this discussion")
        app_id=(settings.AGORA_APP_ID or "").strip();app_certificate=(settings.AGORA_APP_CERTIFICATE or "").strip()
        if not app_id or not app_certificate:raise ValidationError("Agora is not configured on this server")
        from app.core.agora_token2 import RtcTokenBuilder2
        uid=assign_participant_uids(key,[p["id"] for p in participants])[own["id"]]
        channel=self._channel_name(key)
        token=RtcTokenBuilder2.build_token_with_uid(app_id=app_id,app_certificate=app_certificate,channel_name=channel,
            uid=uid,role=1,expire_seconds=max(row["configuration"]["duration_seconds"],1800),rtm_user_id=f"gd_{own['id']}")
        if row["status"]=="active":await self._start_moderator(row)
        return {"agora_app_id":app_id,"agora_token":token,"agora_uid":uid,"channel_name":channel,
            "rtm_user_id":f"gd_{own['id']}","expires_in":max(row["configuration"]["duration_seconds"],1800)}

    async def ingest_voice_turn(self,key,transcript):
        """Commit one final, speaker-attributed voice turn from the Agora RTC channel.

        Partial (non-final) ASR updates are intentionally not persisted as discussion
        contributions -- Agora's live subtitle stream retransmits growing prefixes of the
        same utterance, and only the final revision is a real, once-only contribution.
        """
        if not transcript.is_final or not transcript.text.strip():return {"status":"ignored"}
        row=await self.repo.session(key)
        if not row:raise NotFoundError("Discussion not found")
        if row["status"]!="active":return {"status":"ignored","reason":"discussion_not_active"}
        participants=await self.repo.participants(key)
        roster=[p["id"] for p in participants]
        participant_id=resolve_speaker(key,roster,transcript.rtc_uid)
        if not participant_id:return {"status":"ignored","reason":"unknown_speaker"}
        participant=next(p for p in participants if p["id"]==participant_id)
        if participant["status"]!="joined":return {"status":"ignored","reason":"participant_not_joined"}
        # Agora's own turn_id is the idempotency key -- never a timestamp -- so a retried
        # or duplicated delivery of the same final turn commits exactly once.
        request_id=str(uuid5(NAMESPACE_URL,key+":gd-voice-turn:"+transcript.turn_id))
        event={"id":str(uuid4()),"request_id":request_id,
            "request_hash":fingerprint({"kind":"voice_turn","turn_id":transcript.turn_id,"text":transcript.text}),
            "source_text":transcript.text}
        await self.repo.append_voice(key,participant_id,event)
        try:await self.processing.drain(key,max_events=1)
        except AppError:pass
        await self._stop_moderator_if_ended(key)
        return {"status":"committed","participant_id":participant_id}

    async def _stop_moderator_if_ended(self,key):
        # The moderator can autonomously end a discussion (finish_gd_analysis sets
        # status='completed' when END_DISCUSSION fires past the configured duration)
        # without ever going through GDService.control() -- the voice agent must be
        # stopped here too, not only on an explicit HR "finish"/"cancel" action.
        session=await self.repo.session(key)
        if session and session["status"] in {"completed","cancelled"}:
            await self._stop_moderator(session)

    async def attendance(self,actor,key,body):
        await self.require_session(actor,key)
        await self.repo.attendance(actor,key,body.action,str(body.request_id))
        return await self.view(actor,key)

    async def remove(self,actor,key,participant_id,request_id):
        await self.require_session(actor,key,owner_only=True)
        await self.repo.attendance(actor,key,"remove",str(request_id),participant_id)
        return await self.view(actor,key)

    async def message(self,actor,key,body):
        _,_,own=await self.require_session(actor,key)
        if not own or own["status"]!="joined":raise ForbiddenError("Join the discussion before contributing")
        event={"id":str(uuid4()),"request_id":str(body.request_id),"request_hash":fingerprint(body.model_dump(mode="json",exclude={"request_id"})),
            "source_text":body.text,"reply_to":str(body.reply_to) if body.reply_to else None}
        await self.repo.append(actor,key,event)
        # Raw input is already durable. Another worker may own the analysis lease.
        try:await self.processing.drain(key,max_events=1)
        except AppError:pass
        await self._stop_moderator_if_ended(key)
        return await self.view(actor,key)

    async def generate_report(self,actor,key,participant_id):
        row,participants,own=await self.require_session(actor,key)
        participant=next((p for p in participants if p["id"]==participant_id),None)
        if not participant or own and own["id"]!=participant_id:raise NotFoundError("Participant report not found")
        await self.processing.drain(key,max_events=3)
        await self._stop_moderator_if_ended(key)
        result=await GDReporting(self.repo,self.client).generate(row,participant,actor)
        return report_view(result,actor.role=="candidate")
