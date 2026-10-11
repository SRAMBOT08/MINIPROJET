"""Unit tests for Role Play realtime-voice service methods: agent-registry filtering,
voice dispatch, RTC credential issuance, and voice-turn ingestion. Repository and Agora
calls are mocked (see tests/features/test_role_play.py for the real-Postgres path)."""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.agents.registry import agent_registry
from app.core.config import settings
from app.core.exceptions import NotFoundError, ValidationError
from app.integrations.aicredits_client import AICreditsError
from app.role_play.service import RolePlayService


def make_scenario_definition():
    return {
        "title": "Angry customer", "description": "A customer whose order arrived damaged.",
        "candidate_role": "Support agent", "persona_id": "11111111-1111-1111-1111-111111111111",
        "phases": [{"id": "opening", "title": "Opening", "brief": "Greet the customer.",
                    "objectives": ["Acknowledge the issue"], "advance_on": ["solution"],
                    "min_turns": 1, "max_turns": 4, "reveal_keys": []}],
        "competencies": ["communication", "empathy"], "success_signals": ["solution"],
        "failure_signals": ["hostility"], "hidden_information": {}, "max_turns": 12,
        "duration_seconds": 900, "policy_query": "",
    }


def make_persona_definition():
    return {
        "name": "Jordan the customer", "description": "A frustrated customer seeking a refund.",
        "personality": "Impatient but reasonable if heard out.", "communication_style": "Direct, occasionally curt.",
        "objectives": ["Get a refund"], "concerns": ["Being ignored"],
        "allowed_information": ["Order number is 12345"], "restricted_information": ["Internal refund policy limit"],
        "escalation_signals": ["hostility", "dismissal"], "deescalation_signals": ["empathy", "clarification"],
        "initial_escalation": 2,
    }


def make_session_row(**overrides):
    base = {
        "id": "rp-session-1", "candidate_id": "cand-1", "created_by": "hr1", "tenant_key": "user:hr1",
        "revision": 1,
        "scenario_snapshot": {"id": "scn-1", "revision": 1, "definition": make_scenario_definition()},
        "persona_snapshot": {"id": "per-1", "revision": 1, "definition": make_persona_definition()},
        "state": {"status": "active", "phase_index": 0, "phase_turns": 0, "turn_count": 0, "escalation": 2,
                  "stance": "guarded", "revealed_keys": [], "commitments": [], "achieved_phases": [],
                  "unresolved_objections": [], "started_at": datetime.now(timezone.utc).isoformat(), "ended_at": None,
                  "completion_reason": None, "agent_id": "role_play", "agora_agent_id": None},
    }
    base.update(overrides)
    return base


class RolePlayAgentRegistryTests(unittest.TestCase):
    def test_compatible_agents_returns_only_role_play_channel_agents(self):
        ids = {a.agent_id for a in RolePlayService.compatible_agents()}
        self.assertIn("role_play", ids)
        self.assertNotIn("alex", ids)
        self.assertNotIn("jordan", ids)
        self.assertNotIn("gd_mod", ids)

    def test_registry_channel_filtering_is_explicit_opt_in(self):
        # An agent with no "channels" metadata is invisible to every channel filter --
        # this is the actual root cause of the empty Role Play dropdown before this fix.
        self.assertEqual(agent_registry.list_agents_for_channel("nonexistent_channel"), [])


class RolePlayVoiceServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agora_patch = patch.object(settings, "AGORA_APP_ID", "test-app-id")
        self.cert_patch = patch.object(settings, "AGORA_APP_CERTIFICATE", "test-cert-1234567890123456789012")
        self.agora_patch.start(); self.cert_patch.start()
        self.addCleanup(self.agora_patch.stop); self.addCleanup(self.cert_patch.stop)
        self.svc = RolePlayService(sb=MagicMock())
        self.svc.repo = AsyncMock()
        self.svc.repo.sb = MagicMock()  # .sb.table(...) chains are sync query-builder calls
        self.svc.repo.events = AsyncMock(return_value=[])

    async def test_create_rejects_unknown_agent_id(self):
        actor = MagicMock(role="recruiter", user_id="hr1", tenant_id=None)
        body = MagicMock(agent_id="not-a-real-agent", scenario_id="scn-1", candidate_id="cand-1",
                          request_id="00000000-0000-0000-0000-000000000000")
        with self.assertRaises(ValidationError):
            await self.svc.create(actor, body)

    async def test_start_voice_agent_dispatches_role_play_with_persona_system_prompt(self):
        row = make_session_row()
        from app.role_play.models import RolePlayState, ScenarioDefinition, PersonaDefinition
        state = RolePlayState.model_validate(row["state"])
        scenario = ScenarioDefinition.model_validate(row["scenario_snapshot"]["definition"])
        person = PersonaDefinition.model_validate(row["persona_snapshot"]["definition"])
        with patch("app.services.agora_agent_service.agora_agent_service.start_interview_agent",
                    new=AsyncMock(return_value={"status": "started", "agora_agent_id": "agora-rp-1"})) as start, \
             patch("app.role_play.service.execute", new=AsyncMock()) as execute_mock:
            await self.svc._start_voice_agent("rp-session-1", state, scenario, person, phase_brief="Greet the customer.")
        start.assert_awaited_once()
        kwargs = start.call_args.kwargs
        self.assertEqual(kwargs["agent_id"], "role_play")
        self.assertEqual(kwargs["interview_id"], "roleplay-rp-session-1")
        self.assertIn("Jordan the customer", kwargs["system_prompt_override"])
        self.assertIn("Internal refund policy limit", kwargs["system_prompt_override"])
        self.assertEqual(state.agora_agent_id, "agora-rp-1")
        execute_mock.assert_awaited_once()

    async def test_start_voice_agent_is_idempotent(self):
        row = make_session_row()
        from app.role_play.models import RolePlayState, ScenarioDefinition, PersonaDefinition
        state = RolePlayState.model_validate({**row["state"], "agora_agent_id": "already-running"})
        scenario = ScenarioDefinition.model_validate(row["scenario_snapshot"]["definition"])
        person = PersonaDefinition.model_validate(row["persona_snapshot"]["definition"])
        with patch("app.services.agora_agent_service.agora_agent_service.start_interview_agent", new=AsyncMock()) as start:
            await self.svc._start_voice_agent("rp-session-1", state, scenario, person, phase_brief="hi")
        start.assert_not_awaited()

    async def test_rtc_credentials_rejects_non_owning_candidate(self):
        # require_session already hides session existence from a non-owning candidate
        # (NotFoundError, not ForbiddenError) -- the same behavior every other candidate
        # action on this session gets.
        actor = MagicMock(role="candidate", candidate_id="someone-else", user_id="u2", tenant_id=None)
        self.svc.repo.session = AsyncMock(return_value=make_session_row())
        with self.assertRaises(NotFoundError):
            await self.svc.rtc_credentials(actor, "rp-session-1")

    async def test_rtc_credentials_issues_token_for_assigned_candidate(self):
        actor = MagicMock(role="candidate", candidate_id="cand-1", user_id="u1", tenant_id=None)
        self.svc.repo.session = AsyncMock(return_value=make_session_row())
        result = await self.svc.rtc_credentials(actor, "rp-session-1")
        self.assertEqual(result["agora_app_id"], "test-app-id")
        self.assertEqual(result["channel_name"], "roleplay-rp-session-1")
        self.assertTrue(result["agora_token"])

    async def test_ingest_voice_turn_commits_via_dedicated_rpc_not_actor_commit(self):
        row = make_session_row()
        self.svc.repo.session = AsyncMock(return_value=row)
        self.svc.repo.event_request = AsyncMock(return_value=None)
        self.svc.repo.append_voice_turn = AsyncMock(return_value={"id": "evt-1"})
        self.svc.intelligence.analyze = AsyncMock(side_effect=AICreditsError("credits_exhausted"))
        self.svc.responses.generate = AsyncMock(return_value="Understood, let's continue.")
        result = await self.svc.ingest_voice_turn("rp-session-1", "I hear you, let's find a solution.", "turn-1")
        self.assertEqual(result["status"], "committed")
        self.svc.repo.append_voice_turn.assert_awaited_once()
        self.svc.repo.commit.assert_not_awaited()

    async def test_ingest_voice_turn_is_idempotent_on_repeated_turn_id(self):
        row = make_session_row()
        self.svc.repo.session = AsyncMock(return_value=row)
        self.svc.repo.event_request = AsyncMock(return_value={"id": "evt-1"})
        result = await self.svc.ingest_voice_turn("rp-session-1", "repeat me", "turn-1")
        self.assertEqual(result["status"], "duplicate")
        self.svc.repo.append_voice_turn.assert_not_awaited()

    async def test_ingest_voice_turn_ignores_when_session_not_active(self):
        row = make_session_row(state={**make_session_row()["state"], "status": "completed"})
        self.svc.repo.session = AsyncMock(return_value=row)
        result = await self.svc.ingest_voice_turn("rp-session-1", "too late", "turn-1")
        self.assertEqual(result["status"], "ignored")


if __name__ == "__main__":
    unittest.main()
