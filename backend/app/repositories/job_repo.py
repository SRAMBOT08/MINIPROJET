"""Repository for job posting operations."""

from __future__ import annotations

from typing import Any

import structlog
from supabase import Client
from app.services.round_agents import encode_agent_ids

logger = structlog.stdlib.get_logger("intra_ai.repo.job")


class JobRepo:
    """Encapsulates all Supabase queries against the ``jobs`` table."""

    def __init__(self, supabase: Client) -> None:
        self._sb = supabase

    async def create(self, data: dict[str, Any]) -> dict[str, Any]:
        """Insert a new job posting."""
        result = self._sb.table("jobs").insert(data).execute()
        return result.data[0]

    async def get_by_id(self, job_id: str) -> dict[str, Any] | None:
        """Fetch a single job by ID, with a live applicant count (not the stored
        applications_count column, which is never incremented anywhere)."""
        result = (
            self._sb.table("jobs")
            .select("*, job_rounds(*)")
            .eq("id", job_id)
            .limit(1)
            .execute()
        )
        if not result.data:
            return None
        row = result.data[0]
        row["applications_count"] = await self._count_applications(job_id)
        return row

    async def list(
        self,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """List jobs with optional filters and pagination. Returns (rows, total)."""
        query = self._sb.table("jobs").select("*, job_rounds(*)", count="exact")

        if filters:
            if "job_ids" in filters:
                if not filters["job_ids"]:
                    return [], 0
                query = query.in_("id", filters["job_ids"])
            if status := filters.get("status"):
                query = query.eq("status", status)
            if department := filters.get("department"):
                query = query.eq("department", department)
            if search := filters.get("search"):
                query = query.ilike("title", f"%{search}%")

        offset = (page - 1) * per_page
        query = query.order("created_at", desc=True).range(offset, offset + per_page - 1)

        result = query.execute()
        total = result.count if result.count is not None else len(result.data)
        rows = result.data
        counts = await self._count_applications_by_job([row["id"] for row in rows if row.get("id")])
        for row in rows:
            row["applications_count"] = counts.get(row.get("id"), 0)
        return rows, total

    async def _count_applications(self, job_id: str) -> int:
        """Live applicant count for one job. The applications_count column on the
        jobs table is never written anywhere in application_service.py, so the jobs
        list previously showed a permanently stale value; this mirrors the live
        count the job detail page's useJobApplications query already derives."""
        result = (
            self._sb.table("applications")
            .select("id", count="exact")
            .eq("job_id", job_id)
            .limit(1)
            .execute()
        )
        return result.count or 0

    async def _count_applications_by_job(self, job_ids: list[str]) -> dict[str, int]:
        """Live applicant counts for a page of jobs in one extra query, rather than
        one count query per job."""
        if not job_ids:
            return {}
        result = self._sb.table("applications").select("job_id").in_("job_id", job_ids).execute()
        counts: dict[str, int] = {}
        for row in result.data or []:
            job_id = row.get("job_id")
            if job_id:
                counts[job_id] = counts.get(job_id, 0) + 1
        return counts

    async def update(self, job_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Update a job posting. Returns the updated row."""
        result = (
            self._sb.table("jobs")
            .update(data)
            .eq("id", job_id)
            .execute()
        )
        return result.data[0]

    async def replace_rounds(self, job_id: str, rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace a job's interview-round configuration atomically at the API level."""
        self._sb.table("job_rounds").delete().eq("job_id", job_id).execute()
        if not rounds:
            return []
        try:
            result = self._sb.table("job_rounds").insert(rounds).execute()
            return result.data
        except Exception as exc:
            # Older hosted schemas do not have agent_ids yet. Preserve the
            # assignment in a private focus-area marker until the migration is
            # applied, then transparently return the same logical contract.
            logger.warning("job_round_agent_ids_column_unavailable", error=str(exc))
            legacy_rows = []
            for row in rounds:
                legacy = dict(row)
                ids = legacy.pop("agent_ids", None)
                legacy["focus_areas"] = encode_agent_ids(legacy.get("focus_areas"), ids)
                legacy_rows.append(legacy)
            result = self._sb.table("job_rounds").insert(legacy_rows).execute()
            return result.data
