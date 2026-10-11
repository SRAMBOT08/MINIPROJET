"""Applicant count must be derived live from the applications table, not trusted from the
jobs.applications_count column, which application_service.py never increments (spec: "Use real
persisted data. Do not hardcode.")."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.repositories.job_repo import JobRepo


class _Query:
    def __init__(self, db: "_DB", table: str) -> None:
        self.db, self.table = db, table
        self.filters: list[tuple[str, Any]] = []
        self.in_filters: list[tuple[str, list[Any]]] = []
        self.count_mode: str | None = None
        self.limit_value: int | None = None

    def select(self, *_columns: str, count: str | None = None) -> "_Query":
        self.count_mode = count
        return self

    def eq(self, key: str, value: Any) -> "_Query":
        self.filters.append((key, value))
        return self

    def in_(self, key: str, values: list[Any]) -> "_Query":
        self.in_filters.append((key, list(values)))
        return self

    def limit(self, value: int) -> "_Query":
        self.limit_value = value
        return self

    def order(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def range(self, *_a: Any, **_k: Any) -> "_Query":
        return self

    def execute(self) -> SimpleNamespace:
        rows = self.db.rows.get(self.table, [])
        matches = [
            row for row in rows
            if all(row.get(key) == value for key, value in self.filters)
            and all(row.get(key) in values for key, values in self.in_filters)
        ]
        data = matches[: self.limit_value] if self.limit_value is not None else matches
        count = len(matches) if self.count_mode == "exact" else None
        return SimpleNamespace(data=data, count=count)


class _DB:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {"jobs": [], "applications": []}

    def table(self, name: str) -> _Query:
        return _Query(self, name)


@pytest.mark.asyncio
async def test_get_by_id_ignores_stale_stored_count_and_counts_live_applications() -> None:
    db = _DB()
    # A stale/never-updated stored column, as it is in the real schema today.
    db.rows["jobs"] = [{"id": "job-1", "title": "Engineer", "applications_count": 0}]
    db.rows["applications"] = [
        {"id": "app-1", "job_id": "job-1"},
        {"id": "app-2", "job_id": "job-1"},
        {"id": "app-3", "job_id": "job-2"},
    ]
    repo = JobRepo(db)

    row = await repo.get_by_id("job-1")

    assert row["applications_count"] == 2


@pytest.mark.asyncio
async def test_list_populates_live_counts_per_job_in_one_extra_query() -> None:
    db = _DB()
    db.rows["jobs"] = [
        {"id": "job-1", "title": "Engineer", "created_at": "2026-01-01T00:00:00Z", "applications_count": 999},
        {"id": "job-2", "title": "Designer", "created_at": "2026-01-02T00:00:00Z", "applications_count": 0},
    ]
    db.rows["applications"] = [
        {"id": "app-1", "job_id": "job-1"},
        {"id": "app-2", "job_id": "job-1"},
        {"id": "app-3", "job_id": "job-2"},
    ]
    repo = JobRepo(db)

    rows, total = await repo.list()

    counts = {row["id"]: row["applications_count"] for row in rows}
    assert counts == {"job-1": 2, "job-2": 1}
    assert total == 2


@pytest.mark.asyncio
async def test_list_with_no_jobs_does_not_query_applications() -> None:
    db = _DB()
    repo = JobRepo(db)
    rows, total = await repo.list()
    assert rows == [] and total == 0
