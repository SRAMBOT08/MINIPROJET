"""Unit tests for GD realtime-voice service methods: moderator dispatch, RTC credential
issuance, and speaker-attributed voice turn ingestion. Repository and Agora calls are
mocked; these do not require a database or network access (see tests/features/ for the
real-Postgres integration tests)."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.group_discussion.models import VoiceTranscriptIn
from app.group_discussion.service import GDService


def make_session(**overrides):
    base = {
        "id": "gd-session-1",
        "status": "active",
        "revision": 3,
        "created_by": "hr1",
        "tenant_key": "user:hr1",
        "moderator_state": {},
        "started_at": "2026-01-01T00:00:00+00:00",
        "configuration": {
            "title": "Prioritization", "topic": "How should we prioritize customer requests?",
            "duration_seconds": 1200, "max_participants": 6, "min_participants": 2,
            "competencies": ["communication", "collaboration"], "policy_query": "",
        },
    }
    base.update(overrides)
    return base


def make_participant(pid, status="joined", **overrides):
    base = {"id": pid, "candidate_id": f"cand-{pid}", "display_name": pid.title(),
            "status": status, "hand_raised": False, "accepted_at": "2026-01-01T00:00:00+00:00",
            "report_status": "not_started"}
    base.update(overrides)
    return base


class GDVoiceServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agora_patch = patch.object(settings, "AGORA_APP_ID", "test-app-id")
        self.cert_patch = patch.object(settings, "AGORA_APP_CERTIFICATE", "test-cert-1234567890123456789012")
        self.agora_patch.start(); self.cert_patch.start()
        self.addCleanup(self.agora_patch.stop); self.addCleanup(self.cert_patch.stop)
        self.svc = GDService(sb=MagicMock())
        self.svc.repo = AsyncMock()
        self.svc.repo.events = AsyncMock(return_value=[])

    async def test_start_moderator_dispatches_gd_mod_and_persists_agent_id(self):
        session = make_session(moderator_state={})
        with patch("app.services.agora_agent_service.agora_agent_service.start_interview_agent",
                    new=AsyncMock(return_value={"status": "started", "agora_agent_id": "agora-agent-xyz"})) as start:
            await self.svc._start_moderator(session)
        start.assert_awaited_once()
        kwargs = start.call_args.kwargs
        self.assertEqual(kwargs["agent_id"], "gd_mod")
        self.assertEqual(kwargs["interview_id"], "gd-gd-session-1")
        self.assertIn("How should we prioritize", kwargs["system_prompt_override"])
        self.svc.repo.set_moderator_agent.assert_awaited_once_with("gd-session-1", "agora-agent-xyz")

    async def test_start_moderator_is_idempotent_when_already_dispatched(self):
        session = make_session(moderator_state={"agora_agent_id": "already-running"})
        with patch("app.services.agora_agent_service.agora_agent_service.start_interview_agent",
                    new=AsyncMock()) as start:
            await self.svc._start_moderator(session)
        start.assert_not_awaited()

    async def test_start_moderator_failure_does_not_raise(self):
        session = make_session(moderator_state={})
        with patch("app.services.agora_agent_service.agora_agent_service.start_interview_agent",
                    new=AsyncMock(side_effect=RuntimeError("network down"))):
            await self.svc._start_moderator(session)  # must not raise
        self.svc.repo.set_moderator_agent.assert_not_awaited()

    async def test_stop_moderator_calls_stop_with_stored_agent_id(self):
        session = make_session(moderator_state={"agora_agent_id": "agora-agent-xyz"})
        with patch("app.services.agora_agent_service.agora_agent_service.stop_interview_agent",
                    new=AsyncMock(return_value={"status": "stopped"})) as stop:
            await self.svc._stop_moderator(session)
        stop.assert_awaited_once_with(interview_id="gd-gd-session-1", agent_id="gd_mod", agora_agent_id="agora-agent-xyz")

    async def test_stop_moderator_noop_when_never_started(self):
        with patch("app.services.agora_agent_service.agora_agent_service.stop_interview_agent", new=AsyncMock()) as stop:
            await self.svc._stop_moderator(make_session(moderator_state={}))
        stop.assert_not_awaited()

    async def test_rtc_credentials_requires_joined_participant(self):
        actor = MagicMock(role="candidate", candidate_id="cand-p1", user_id="u1", tenant_id=None)
        session = make_session()
        self.svc.repo.session = AsyncMock(return_value=session)
        self.svc.repo.participants = AsyncMock(return_value=[make_participant("p1", status="invited")])
        with self.assertRaises(ForbiddenError):
            await self.svc.rtc_credentials(actor, "gd-session-1")

    async def test_rtc_credentials_issues_scoped_token_and_triggers_moderator_start(self):
        actor = MagicMock(role="candidate", candidate_id="cand-p1", user_id="u1", tenant_id=None)
        session = make_session()
        self.svc.repo.session = AsyncMock(return_value=session)
        self.svc.repo.participants = AsyncMock(return_value=[make_participant("p1"), make_participant("p2")])
        with patch("app.services.agora_agent_service.agora_agent_service.start_interview_agent",
                    new=AsyncMock(return_value={"status": "started", "agora_agent_id": "a1"})):
            result = await self.svc.rtc_credentials(actor, "gd-session-1")
        self.assertEqual(result["agora_app_id"], "test-app-id")
        self.assertEqual(result["channel_name"], "gd-gd-session-1")
        self.assertIsInstance(result["agora_uid"], int)
        self.assertTrue(result["agora_token"])

    async def test_rtc_credentials_rejects_when_agora_not_configured(self):
        self.agora_patch.stop(); self.cert_patch.stop()
        try:
            with patch.object(settings, "AGORA_APP_ID", ""), patch.object(settings, "AGORA_APP_CERTIFICATE", ""):
                actor = MagicMock(role="candidate", candidate_id="cand-p1", user_id="u1", tenant_id=None)
                self.svc.repo.session = AsyncMock(return_value=make_session())
                self.svc.repo.participants = AsyncMock(return_value=[make_participant("p1")])
                with self.assertRaises(ValidationError):
                    await self.svc.rtc_credentials(actor, "gd-session-1")
        finally:
            self.agora_patch.start(); self.cert_patch.start()

    async def test_ingest_voice_turn_resolves_speaker_and_commits_exactly_once(self):
        session = make_session()
        participants = [make_participant("p1"), make_participant("p2")]
        self.svc.repo.session = AsyncMock(return_value=session)
        self.svc.repo.participants = AsyncMock(return_value=participants)
        self.svc.processing.drain = AsyncMock(return_value=1)
        from app.group_discussion.identity import assign_participant_uids
        uid = assign_participant_uids("gd-session-1", ["p1", "p2"])["p1"]
        transcript = VoiceTranscriptIn(rtc_uid=uid, turn_id="turn-1", text="I propose we ship weekly.", is_final=True)
        result = await self.svc.ingest_voice_turn("gd-session-1", transcript)
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["participant_id"], "p1")
        self.svc.repo.append_voice.assert_awaited_once()
        args = self.svc.repo.append_voice.call_args.args
        self.assertEqual(args[0], "gd-session-1")
        self.assertEqual(args[1], "p1")

    async def test_ingest_voice_turn_ignores_partial_transcripts(self):
        transcript = VoiceTranscriptIn(rtc_uid=1, turn_id="turn-1", text="partial frag", is_final=False)
        result = await self.svc.ingest_voice_turn("gd-session-1", transcript)
        self.assertEqual(result["status"], "ignored")
        self.svc.repo.append_voice.assert_not_awaited()

    async def test_ingest_voice_turn_ignores_unknown_speaker_uid(self):
        session = make_session()
        self.svc.repo.session = AsyncMock(return_value=session)
        self.svc.repo.participants = AsyncMock(return_value=[make_participant("p1")])
        transcript = VoiceTranscriptIn(rtc_uid=999999, turn_id="turn-1", text="mystery speaker", is_final=True)
        result = await self.svc.ingest_voice_turn("gd-session-1", transcript)
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "unknown_speaker")
        self.svc.repo.append_voice.assert_not_awaited()

    async def test_ingest_voice_turn_ignores_when_discussion_not_active(self):
        session = make_session(status="lobby")
        self.svc.repo.session = AsyncMock(return_value=session)
        transcript = VoiceTranscriptIn(rtc_uid=1, turn_id="turn-1", text="too early", is_final=True)
        result = await self.svc.ingest_voice_turn("gd-session-1", transcript)
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "discussion_not_active")

    async def test_control_start_transition_dispatches_moderator(self):
        actor = MagicMock(role="recruiter", user_id="hr1", tenant_id=None, candidate_id=None)
        before = make_session(status="lobby")
        after = make_session(status="active")
        self.svc.repo.session = AsyncMock(return_value=before)
        self.svc.repo.participants = AsyncMock(return_value=[make_participant("p1")])
        self.svc.repo.control = AsyncMock(return_value=after)
        with patch.object(self.svc, "_start_moderator", new=AsyncMock()) as start_mod, \
             patch.object(self.svc, "_stop_moderator", new=AsyncMock()) as stop_mod, \
             patch.object(self.svc, "view", new=AsyncMock(return_value={})):
            await self.svc.control(actor, "gd-session-1", MagicMock(action="start"))
        start_mod.assert_awaited_once_with(after)
        stop_mod.assert_not_awaited()

    async def test_stop_moderator_if_ended_stops_on_autonomous_completion(self):
        # The moderator can end a discussion on its own (finish_gd_analysis sets
        # status='completed' when END_DISCUSSION fires past the configured duration)
        # without ever going through control(finish) -- ingest_voice_turn/message must
        # still stop the voice agent in that case.
        session = make_session(status="completed", moderator_state={"agora_agent_id": "agora-agent-xyz"})
        self.svc.repo.session = AsyncMock(return_value=session)
        with patch.object(self.svc, "_stop_moderator", new=AsyncMock()) as stop_mod:
            await self.svc._stop_moderator_if_ended("gd-session-1")
        stop_mod.assert_awaited_once_with(session)

    async def test_stop_moderator_if_ended_noop_while_still_active(self):
        session = make_session(status="active", moderator_state={"agora_agent_id": "agora-agent-xyz"})
        self.svc.repo.session = AsyncMock(return_value=session)
        with patch.object(self.svc, "_stop_moderator", new=AsyncMock()) as stop_mod:
            await self.svc._stop_moderator_if_ended("gd-session-1")
        stop_mod.assert_not_awaited()

    async def test_control_finish_transition_stops_moderator(self):
        actor = MagicMock(role="recruiter", user_id="hr1", tenant_id=None, candidate_id=None)
        before = make_session(status="active")
        after = make_session(status="completed")
        self.svc.repo.session = AsyncMock(return_value=before)
        self.svc.repo.participants = AsyncMock(return_value=[make_participant("p1")])
        self.svc.repo.control = AsyncMock(return_value=after)
        with patch.object(self.svc, "_start_moderator", new=AsyncMock()) as start_mod, \
             patch.object(self.svc, "_stop_moderator", new=AsyncMock()) as stop_mod, \
             patch.object(self.svc, "view", new=AsyncMock(return_value={})):
            await self.svc.control(actor, "gd-session-1", MagicMock(action="finish"))
        stop_mod.assert_awaited_once_with(after)
        start_mod.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
