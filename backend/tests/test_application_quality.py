"""Regression coverage for recruiter overrides and application scoring fallbacks."""

from unittest.mock import AsyncMock

import pytest

from app.models.enums import ApplicationStatus
from app.services.application_service import ApplicationService
from app.services.eligibility_service import EligibilityService


@pytest.mark.asyncio
async def test_recruiter_can_reconsider_an_automatically_rejected_application() -> None:
    repo = type("Repo", (), {})()
    repo.get_by_id = AsyncMock(
        return_value={"id": "app-1", "status": ApplicationStatus.REJECTED.value}
    )
    repo.update_status = AsyncMock(
        return_value={
            "id": "app-1",
            "job_id": "job-1",
            "candidate_id": "candidate-1",
            "status": ApplicationStatus.SHORTLISTED.value,
            "eligibility_score": 43.0,
            "created_at": "2026-09-05T00:00:00+00:00",
        }
    )

    result = await ApplicationService(repo, None, None).shortlist("app-1")

    assert result.status == ApplicationStatus.SHORTLISTED
    assert result.eligibility_score == 43.0
    repo.update_status.assert_awaited_once_with("app-1", ApplicationStatus.SHORTLISTED.value)


def test_experience_score_uses_explicit_application_years_when_parser_has_no_entries() -> None:
    assert EligibilityService._calculate_experience_match([], 3, 7, candidate_years=6) == 100.0
    assert EligibilityService._calculate_experience_match([], 3, 7, candidate_years=1) == 33.33


class _FakeEligibilityTable:
    """Minimal fake for the raw eligibility_scores table access in check_eligibility."""

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def maybe_single(self):
        return self

    def update(self, *_a, **_k):
        return self

    def insert(self, row):
        self.inserted = row
        return self

    def execute(self):
        return type("Result", (), {"data": None})()


@pytest.mark.asyncio
async def test_check_eligibility_auto_invites_on_threshold_crossing() -> None:
    """Crossing the threshold must both auto-shortlist and auto-invite in one pass,
    with no manual "Send Invite" click required (spec: avoid unnecessary HR approval
    once the eligibility threshold is genuinely crossed)."""
    fake_table = _FakeEligibilityTable()
    app_repo = type("Repo", (), {})()
    app_repo._sb = type("SB", (), {"table": lambda self, name: fake_table})()
    app_repo.get_by_id = AsyncMock(return_value={"id": "app-1", "job_id": "job-1"})
    app_repo.get_parsed_resume = AsyncMock(
        return_value={"skills": ["Python", "FastAPI"], "experience": [], "education": []}
    )
    app_repo.update_status = AsyncMock(return_value={})

    job_repo = type("Repo", (), {})()
    job_repo.get_by_id = AsyncMock(
        return_value={
            "id": "job-1",
            "required_skills": ["Python"],
            "experience_min": 0,
            "experience_max": 10,
            "education": None,
            "eligibility_threshold": 10.0,
        }
    )

    score = await EligibilityService(app_repo, job_repo).check_eligibility("app-1")

    assert score >= 10.0
    calls = app_repo.update_status.await_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == "app-1" and calls[0].args[1] == ApplicationStatus.SHORTLISTED.value
    assert calls[1].args == ("app-1", ApplicationStatus.INVITED.value)


@pytest.mark.asyncio
async def test_check_eligibility_below_threshold_never_invites() -> None:
    fake_table = _FakeEligibilityTable()
    app_repo = type("Repo", (), {})()
    app_repo._sb = type("SB", (), {"table": lambda self, name: fake_table})()
    app_repo.get_by_id = AsyncMock(return_value={"id": "app-1", "job_id": "job-1"})
    app_repo.get_parsed_resume = AsyncMock(return_value={"skills": [], "experience": [], "education": []})
    app_repo.update_status = AsyncMock(return_value={})

    job_repo = type("Repo", (), {})()
    job_repo.get_by_id = AsyncMock(
        return_value={
            "id": "job-1",
            "required_skills": ["Rust", "Kubernetes", "Go"],
            "experience_min": 5,
            "experience_max": 10,
            "education": "Master's Degree",
            "eligibility_threshold": 95.0,
        }
    )

    await EligibilityService(app_repo, job_repo).check_eligibility("app-1")

    calls = app_repo.update_status.await_args_list
    assert len(calls) == 1
    assert calls[0].args[1] == ApplicationStatus.REJECTED.value
