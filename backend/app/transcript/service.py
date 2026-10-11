"""Transcript service for ingesting, validating, normalizing, and retrieving conversation events."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Optional
import structlog

from app.agents.registry import agent_registry
from app.interview_context.store import interview_session_store
from app.transcript.models import (
    AgoraWebhookPayload,
    SpeakerType,
    TranscriptEvent,
    TranscriptEventCreate,
    TranscriptEventType,
    TranscriptListResponse,
)
from app.transcript.store import TranscriptStore, transcript_store

logger = structlog.stdlib.get_logger("intra_ai.transcript.service")


class TranscriptService:
    """Service for handling transcript and conversation turn events from Agora."""

    def __init__(self, store: Optional[TranscriptStore] = None) -> None:
        self.store = store or transcript_store
        self._background_tasks: set[asyncio.Task] = set()

    def _resolve_session_and_agent(
        self,
        identifier: Optional[str] = None,
        channel: Optional[str] = None,
        agent_id_hint: Optional[str] = None,
    ) -> tuple[str, str, str]:
        """Resolve canonical interview_id, channel, and logical agent_id."""
        candidate_ids = [identifier, channel]
        resolved_interview_id = "test-room-101"
        for cid in candidate_ids:
            if cid and cid.strip():
                resolved_interview_id = interview_session_store.resolve_interview_id(cid.strip())
                break

        resolved_channel = channel.strip() if channel and channel.strip() else resolved_interview_id

        # Resolve logical agent ID
        resolved_agent_id = (agent_id_hint or "").strip().lower()
        if not resolved_agent_id or resolved_agent_id not in agent_registry.list_agent_ids():
            ctx = interview_session_store.get(resolved_interview_id)
            if ctx and ctx.current_agent_id:
                resolved_agent_id = ctx.current_agent_id
            else:
                resolved_agent_id = "alex"

        return resolved_interview_id, resolved_channel, resolved_agent_id

    async def ingest_agora_webhook(
        self,
        payload_data: dict[str, Any] | AgoraWebhookPayload,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Ingest Agora Cloud Webhook notification (e.g. Event 103 Agent History or Event 112 Turns Finished)."""
        t_start = time.perf_counter()

        if isinstance(payload_data, AgoraWebhookPayload):
            raw = payload_data.model_dump()
        else:
            raw = dict(payload_data)

        event_type = raw.get("event_type") or raw.get("event_id")
        notice_id = raw.get("notice_id")
        notify_ts = raw.get("notify_ts") or (time.time() * 1000)

        inner_payload = raw.get("payload") or {}
        channel = inner_payload.get("channel") or inner_payload.get("name") or raw.get("channel")
        agora_agent_id = inner_payload.get("agent_id") or raw.get("agent_id")

        logger.info(
            "[AGORA_EVENT_RECEIVED]",
            event_type=event_type,
            notice_id=notice_id,
            channel=channel,
            agora_agent_id=agora_agent_id,
        )

        # 1. Resolve interview and agent
        interview_id, resolved_channel, agent_id = self._resolve_session_and_agent(
            identifier=agora_agent_id or channel,
            channel=channel,
        )

        events_processed = 0
        events_stored = 0

        # 2. Handle Event 103: Agent History
        # Payload contains 'contents': list of {role: 'user'|'assistant', content: str, speech_start_ms: int, speech_end_ms: int}
        contents = inner_payload.get("contents")
        if isinstance(contents, list) and contents:
            for idx, item in enumerate(contents):
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role", "")).lower()
                text = str(item.get("content", "")).strip()
                if not text:
                    continue

                events_processed += 1
                speaker = SpeakerType.AGENT if role in ("assistant", "agent") else SpeakerType.CANDIDATE
                turn_ts = float(item.get("speech_start_ms") or notify_ts or (time.time() * 1000))
                turn_id = item.get("turn_id") or f"{interview_id}-hist-{idx}-{int(turn_ts)}"

                event = TranscriptEvent(
                    id=str(turn_id),
                    interview_id=interview_id,
                    channel=resolved_channel,
                    agent_id=agent_id if speaker == SpeakerType.AGENT else None,
                    speaker=speaker,
                    text=text,
                    timestamp=turn_ts,
                    sequence=idx + 1,
                    event_type=TranscriptEventType.TRANSCRIPT,
                    is_final=True,
                    source="agora_webhook_103",
                    metadata={
                        "speech_start_ms": item.get("speech_start_ms"),
                        "speech_end_ms": item.get("speech_end_ms"),
                        "algorithmic_delay": item.get("speech_algorithmic_delay"),
                    },
                )
                if self.store.add_event_sync(event):
                    events_stored += 1

        # 3. Handle Event 112: Turns Finished
        # Payload contains 'turns': list of turns
        turns = inner_payload.get("turns")
        if isinstance(turns, list) and turns:
            for idx, turn in enumerate(turns):
                if not isinstance(turn, dict):
                    continue
                turn_role = str(turn.get("speaker") or turn.get("role") or "").lower()
                text = str(turn.get("text") or turn.get("content") or "").strip()
                if not text:
                    continue

                events_processed += 1
                speaker = SpeakerType.AGENT if turn_role in ("assistant", "agent") else SpeakerType.CANDIDATE
                turn_ts = float(turn.get("timestamp") or turn.get("start_ts") or notify_ts or (time.time() * 1000))
                turn_id = str(turn.get("turn_id") or turn.get("id") or f"{interview_id}-turn-{idx}-{int(turn_ts)}")

                event = TranscriptEvent(
                    id=turn_id,
                    interview_id=interview_id,
                    channel=resolved_channel,
                    agent_id=agent_id if speaker == SpeakerType.AGENT else None,
                    speaker=speaker,
                    text=text,
                    timestamp=turn_ts,
                    sequence=idx + 1,
                    event_type=TranscriptEventType.TRANSCRIPT,
                    is_final=True,
                    source="agora_webhook_112",
                    metadata={"turn_id": turn.get("turn_id"), "latency_ms": turn.get("latency_ms")},
                )
                if self.store.add_event_sync(event):
                    events_stored += 1

        duration_ms = round((time.perf_counter() - t_start) * 1000, 2)
        return {
            "status": "ok",
            "event_type": event_type,
            "interview_id": interview_id,
            "channel": resolved_channel,
            "events_processed": events_processed,
            "events_stored": events_stored,
            "duration_ms": duration_ms,
        }

    async def ingest_event(
        self,
        event_in: TranscriptEventCreate | dict[str, Any],
        interview_id_param: Optional[str] = None,
    ) -> dict[str, Any]:
        """Ingest an individual normalized transcript turn or RTM event."""
        t_start = time.perf_counter()

        if isinstance(event_in, TranscriptEventCreate):
            data = event_in.model_dump()
        else:
            data = dict(event_in)

        text = str(data.get("text", "")).strip()
        if not text:
            return {"status": "error", "message": "Text content cannot be empty."}

        # Resolve interview and channel
        raw_id = interview_id_param or data.get("interview_id") or data.get("channel")
        interview_id, channel, agent_id = self._resolve_session_and_agent(
            identifier=raw_id,
            channel=data.get("channel"),
            agent_id_hint=data.get("agent_id"),
        )

        # Determine speaker
        raw_speaker = str(data.get("speaker") or data.get("role") or "").lower()
        if raw_speaker in ("agent", "assistant"):
            speaker = SpeakerType.AGENT
        elif raw_speaker in ("system",):
            speaker = SpeakerType.SYSTEM
        else:
            speaker = SpeakerType.CANDIDATE

        # Timestamp and event ID
        event_ts = float(data.get("timestamp") or (time.time() * 1000))
        event_id = str(data.get("id") or f"evt-{uuid.uuid4().hex[:12]}")

        event = TranscriptEvent(
            id=event_id,
            interview_id=interview_id,
            channel=channel,
            agent_id=agent_id if speaker == SpeakerType.AGENT else None,
            speaker=speaker,
            speaker_uid=str(data.get("speaker_uid")) if data.get("speaker_uid") is not None else None,
            text=text,
            timestamp=event_ts,
            sequence=data.get("sequence"),
            event_type=TranscriptEventType(data.get("event_type") or "transcript"),
            is_final=bool(data.get("is_final", True)),
            source=str(data.get("source") or "direct_ingestion"),
            metadata=data.get("metadata") or {},
        )

        stored = self.store.add_event_sync(event)
        duration_ms = round((time.perf_counter() - t_start) * 1000, 2)

        if stored and event.is_final:
            if speaker == SpeakerType.AGENT:
                ctx = interview_session_store.get(interview_id)
                if ctx:
                    ctx.metadata["active_interviewer_question"] = text
            elif speaker == SpeakerType.CANDIDATE:
                task = asyncio.create_task(
                    self._process_candidate_turn_async(event, interview_id, agent_id)
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

        return {
            "status": "stored" if stored else "duplicate",
            "event_id": event.id,
            "interview_id": interview_id,
            "speaker": event.speaker.value,
            "duration_ms": duration_ms,
            "active_agent_id": agent_id,
        }

    async def _process_candidate_turn_async(
        self,
        event: TranscriptEvent,
        interview_id: str,
        agent_id: str,
    ) -> None:
        """Asynchronously analyze candidate turns via M1 and evaluate with Meta-Orchestrator."""
        try:
            context = interview_session_store.get(interview_id)
            if not context:
                return

            candidate_text = (event.text or "").strip()
            if not candidate_text:
                return

            # Check if there is an active question
            question_text = context.metadata.get("active_interviewer_question")
            if not question_text:
                events = self.store.get_transcript(interview_id, is_final_only=True)
                for ev in reversed(events):
                    if ev.speaker == SpeakerType.AGENT and ev.text.strip():
                        question_text = ev.text.strip()
                        break

            if not question_text:
                question_text = "Could you describe a recent project you worked on?"

            from app.interview_intelligence.models import InterviewAnswerInput
            from app.interview_intelligence.analyzer import m1_analyzer, apply_analysis_to_context

            profile = agent_registry.get_profile(agent_id) if agent_registry.has_agent(agent_id) else None
            competency = next(
                (c for c in context.missing_competencies if profile and c in profile.focal_competencies),
                context.missing_competencies[0] if context.missing_competencies else "general",
            )

            input_data = InterviewAnswerInput(
                interview_id=interview_id,
                turn_id=event.id,
                question_text=question_text,
                answer_text=candidate_text,
                competency=competency,
                difficulty=context.difficulty,
                context_metadata=context.metadata,
            )

            analysis = await m1_analyzer.analyze_answer_async(input_data)
            apply_analysis_to_context(context, analysis)

            context.metadata["last_candidate_answer"] = candidate_text
            context.metadata["last_m1_answer_id"] = analysis.answer_id

            from app.orchestrator.service import meta_orchestrator, MetaOrchestrator
            from app.agents.models import ActionType

            next_action = await meta_orchestrator.decide_async(
                context=context,
                analysis=analysis,
                current_question_text=question_text,
            )

            MetaOrchestrator.record_assessment(context, next_action)
            if next_action.difficulty:
                context.set_difficulty(next_action.difficulty)

            if next_action.action == ActionType.SWITCH_AGENT and next_action.target_agent_id:
                from app.sessions.service import interview_session_service
                session = interview_session_service.get_session(interview_id)
                if session and session.current_agent_id != next_action.target_agent_id:
                    logger.info(
                        "[MANAGED_AGENT_SWITCH_TRIGGERED]",
                        interview_id=interview_id,
                        from_agent=agent_id,
                        to_agent=next_action.target_agent_id,
                    )
                    from app.custom_llm.adapter import generate_handoff_question
                    target_profile = agent_registry.get_profile(next_action.target_agent_id)
                    handoff_greeting = generate_handoff_question(target_profile, context, next_action.competency)
                    await interview_session_service.handoff_agent(
                        interview_id=interview_id,
                        target_agent_id=next_action.target_agent_id,
                        greeting_text=handoff_greeting,
                    )
            elif next_action.action == ActionType.COMPLETE:
                from app.sessions.service import interview_session_service
                session = interview_session_service.get_session(interview_id)
                if session:
                    logger.info("[MANAGED_INTERVIEW_COMPLETION_TRIGGERED]", interview_id=interview_id)
                    await interview_session_service.stop_session(interview_id)

        except Exception as exc:
            logger.warning(
                "background_turn_orchestration_failed",
                interview_id=interview_id,
                error=str(exc),
            )

    def get_interview_transcript(
        self,
        interview_id: str,
        is_final_only: bool = True,
    ) -> TranscriptListResponse:
        """Retrieve ordered normalized transcript events for an interview."""
        canonical_id, channel, agent_id = self._resolve_session_and_agent(identifier=interview_id)
        events = self.store.get_transcript(canonical_id, is_final_only=is_final_only)

        return TranscriptListResponse(
            interview_id=canonical_id,
            channel=channel,
            agent_id=agent_id,
            total_events=len(events),
            events=events,
        )


# Global singleton service
transcript_service = TranscriptService()
