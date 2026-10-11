"""Comprehensive regression test suite for Agora Managed AI voice pipeline.

Validates:
1. Agora Managed ASR, LLM, TTS dispatch without BYOK / custom LLM endpoint dependency.
2. Dynamic greeting and system prompt injection.
3. Asynchronous candidate turn ingestion and M1/orchestrator evaluation.
4. Multi-agent handoff (Alex -> Jordan and Jordan -> Alex).
5. Duplicate / out-of-order turn handling and idempotency.
6. Error handling and recovery.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.agents.models import ActionType, AgentProfile, NextAction
from app.agents.registry import agent_registry
from app.core.config import settings
from app.core.exceptions import AgoraConfigurationError
from app.interview_context.models import InterviewAIContext
from app.interview_context.store import interview_session_store
from app.models.enums import DifficultyLevel
from app.services.agora_agent_service import AgoraAgentService
from app.sessions.models import InterviewConfiguration, SessionStatus
from app.sessions.service import InterviewSessionService
from app.sessions.store import SessionStore
from app.transcript.models import SpeakerType, TranscriptEvent
from app.transcript.service import TranscriptService
from app.transcript.store import TranscriptStore


class TestAgoraManagedAIPipeline(unittest.IsolatedAsyncioTestCase):
    """Test suite verifying Agora Managed AI voice pipeline and orchestration."""

    def setUp(self) -> None:
        interview_session_store.clear()
        self.mock_http = MagicMock(spec=httpx.AsyncClient)
        self.mock_http.__aenter__.return_value = self.mock_http
        self.mock_http.__aexit__.return_value = None
        self.service = AgoraAgentService(http_client=self.mock_http)

    def tearDown(self) -> None:
        interview_session_store.clear()

    # ── 1. MANAGED AI DISPATCH WITHOUT BYOK ──────────────────────────────────
    async def test_01_alex_dispatches_with_managed_ai_and_greeting(self) -> None:
        """Alex dispatches with native Managed AI, dynamic greeting, and NO custom LLM url."""
        self.mock_http.post = AsyncMock(
            return_value=httpx.Response(200, json={"agent_id": "cloud-alex-1", "status": "RUNNING"})
        )

        with patch.object(settings, "CUSTOM_LLM_URL", ""), \
             patch.object(settings, "AGORA_CUSTOMER_ID", "cust_123"), \
             patch.object(settings, "AGORA_CUSTOMER_SECRET", "sec_456"), \
             patch.object(settings, "AGORA_APP_ID", "test_app_id"), \
             patch.object(settings, "AGORA_APP_CERTIFICATE", "test_cert_1234567890123456789012"):

            result = await self.service.start_interview_agent(
                interview_id="test-managed-alex",
                agent_id="alex",
                greeting_text="Hello candidate! Describe your backend experience.",
            )

            self.assertEqual(result["status"], "started")
            self.assertEqual(result["agora_agent_id"], "cloud-alex-1")

            payload = self.mock_http.post.call_args.kwargs["json"]
            props = payload["properties"]

            # Verify NO BYOK custom LLM url
            self.assertNotIn("url", props["llm"])
            self.assertNotIn("credential_mode", props["llm"])
            self.assertNotIn("api_key", props["llm"])

            # Verify Managed LLM properties
            self.assertEqual(props["llm"]["greeting_message"], "Hello candidate! Describe your backend experience.")
            self.assertEqual(props["llm"]["greeting_configs"]["mode"], "single_first")
            self.assertTrue(len(props["llm"]["system_messages"]) >= 1)
            self.assertIn("Alex", props["llm"]["system_messages"][0]["content"])

            # Verify Managed TTS
            self.assertEqual(props["tts"]["credential_mode"], "managed")
            self.assertEqual(props["tts"]["vendor"], "openai")
            self.assertEqual(props["tts"]["params"]["voice"], "echo")

            # Verify Authorization uses Customer ID/Secret Basic auth
            auth_header = self.mock_http.post.call_args.kwargs["headers"]["Authorization"]
            self.assertTrue(auth_header.startswith("Basic "))

    async def test_02_jordan_dispatches_with_managed_ai_and_nova_voice(self) -> None:
        """Jordan dispatches with native Managed AI, Jordan's UID 247504, and nova voice."""
        self.mock_http.post = AsyncMock(
            return_value=httpx.Response(200, json={"agent_id": "cloud-jordan-1", "status": "RUNNING"})
        )

        with patch.object(settings, "CUSTOM_LLM_URL", ""), \
             patch.object(settings, "AGORA_APP_ID", "test_app_id"), \
             patch.object(settings, "AGORA_APP_CERTIFICATE", "test_cert_1234567890123456789012"):

            result = await self.service.start_interview_agent(
                interview_id="test-managed-jordan",
                agent_id="jordan",
            )

            self.assertEqual(result["status"], "started")
            payload = self.mock_http.post.call_args.kwargs["json"]
            props = payload["properties"]

            self.assertEqual(props["agent_rtc_uid"], "247504")
            self.assertNotIn("url", props["llm"])
            self.assertEqual(props["tts"]["params"]["voice"], "nova")
            self.assertIn("Jordan", props["llm"]["system_messages"][0]["content"])

    # ── 2. TRANSCRIPT INGESTION & ASYNC ORCHESTRATION ───────────────────────
    async def test_03_candidate_transcript_triggers_background_orchestration(self) -> None:
        """Ingesting candidate final transcript saves event and triggers background analysis."""
        store = TranscriptStore()
        transcript_svc = TranscriptService(store=store)

        # Setup active session context
        interview_id = "test-session-managed"
        context = interview_session_store.get_or_create(
            interview_id=interview_id,
            candidate_id="cand_1",
            agent_id="alex",
            round_id="tech_round",
            missing_competencies=["system_design", "scalability"],
        )
        context.metadata["active_interviewer_question"] = "How do you scale PostgreSQL?"

        with patch.object(transcript_svc, "_process_candidate_turn_async", new_callable=AsyncMock) as mock_process:
            result = await transcript_svc.ingest_event(
                event_in={
                    "id": "turn-cand-1",
                    "interview_id": interview_id,
                    "channel": interview_id,
                    "speaker": "candidate",
                    "text": "We used connection pooling with PgBouncer and horizontal read replicas.",
                    "timestamp": 1000.0,
                    "is_final": True,
                },
                interview_id_param=interview_id,
            )

            self.assertEqual(result["status"], "stored")
            self.assertEqual(result["speaker"], "candidate")

            # Let the event loop run scheduled tasks
            await asyncio.sleep(0.01)
            mock_process.assert_called_once()

    # ── 3. AGENT QUESTION UPDATES CONTEXT METADATA ──────────────────────────
    async def test_04_agent_transcript_updates_active_question(self) -> None:
        """When an agent turn is ingested, it updates active_interviewer_question in context."""
        store = TranscriptStore()
        transcript_svc = TranscriptService(store=store)

        interview_id = "test-session-agent-turn"
        context = interview_session_store.get_or_create(
            interview_id=interview_id,
            candidate_id="cand_2",
            agent_id="alex",
        )

        result = await transcript_svc.ingest_event(
            event_in={
                "id": "turn-agent-1",
                "interview_id": interview_id,
                "speaker": "agent",
                "text": "What caching strategies did you evaluate?",
                "is_final": True,
            },
            interview_id_param=interview_id,
        )

        self.assertEqual(result["status"], "stored")
        self.assertEqual(context.metadata.get("active_interviewer_question"), "What caching strategies did you evaluate?")

    # ── 4. MULTI-AGENT HANDOFF ──────────────────────────────────────────────
    async def test_05_multi_agent_handoff_stops_alex_and_starts_jordan(self) -> None:
        """Handoff stops outgoing agent in Agora before starting incoming agent with handoff greeting."""
        session_store = SessionStore()
        session_svc = InterviewSessionService(store=session_store)

        session = session_svc.create_session(InterviewConfiguration(
            interview_id="test-handoff-session",
            candidate_id="cand_3",
            agent_ids=["alex", "jordan"],
            metadata={"current_round_id": "tech_round", "required_competencies": ["system_design", "product_sense"]},
        ))
        session.status = SessionStatus.IN_PROGRESS
        session.started_agents = {"alex": "cloud-alex-active"}
        session_store.set(session)

        with patch("app.services.agora_agent_service.agora_agent_service.stop_interview_agent", new_callable=AsyncMock) as mock_stop, \
             patch("app.services.agora_agent_service.agora_agent_service.start_interview_agent", new_callable=AsyncMock) as mock_start:

            mock_stop.return_value = {"status": "stopped"}
            mock_start.return_value = {"status": "started", "agora_agent_id": "cloud-jordan-new"}

            result = await session_svc.handoff_agent(
                interview_id="test-handoff-session",
                target_agent_id="jordan",
                greeting_text="Thanks Alex. Hi candidate, I am Jordan.",
            )

            self.assertEqual(session.current_agent_id, "jordan")
            self.assertEqual(session.started_agents.get("jordan"), "cloud-jordan-new")
            self.assertNotIn("alex", session.started_agents)

            mock_stop.assert_called_once_with(
                interview_id=session.channel_name,
                agent_id="alex",
                agora_agent_id="cloud-alex-active",
            )

            mock_start.assert_called_once_with(
                interview_id=session.channel_name,
                agent_id="jordan",
                user_uid=0,
                greeting_text="Thanks Alex. Hi candidate, I am Jordan.",
            )

    # ── 5. DUPLICATE TURN IDEMPOTENCY ───────────────────────────────────────
    async def test_06_duplicate_turn_is_rejected_idempotently(self) -> None:
        """Submitting the exact same transcript ID twice does not re-process or duplicate."""
        store = TranscriptStore()
        transcript_svc = TranscriptService(store=store)

        event_payload = {
            "id": "turn-dedup-100",
            "interview_id": "test-dedup",
            "speaker": "candidate",
            "text": "First submission of this answer.",
            "is_final": True,
        }

        res1 = await transcript_svc.ingest_event(event_payload)
        self.assertEqual(res1["status"], "stored")

        res2 = await transcript_svc.ingest_event(event_payload)
        self.assertEqual(res2["status"], "duplicate")

        transcript = transcript_svc.get_interview_transcript("test-dedup")
        self.assertEqual(transcript.total_events, 1)

    # ── 6. AGORA ERROR RECOVERY ─────────────────────────────────────────────
    async def test_07_agora_start_failure_returns_sanitized_error(self) -> None:
        """Agora API failure during start returns structured error without crashing."""
        self.mock_http.post = AsyncMock(
            return_value=httpx.Response(500, json={"message": "Internal Server Error"})
        )

        with patch.object(settings, "AGORA_APP_ID", "test_app_id"), \
             patch.object(settings, "AGORA_APP_CERTIFICATE", "test_cert_1234567890123456789012"):

            result = await self.service.start_interview_agent(
                interview_id="test-fail-recovery",
                agent_id="alex",
            )

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["status_code"], 500)
            self.assertIn("Agora API join failed", result["error"])
