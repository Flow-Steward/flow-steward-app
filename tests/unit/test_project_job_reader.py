"""Project authorization precedes the distinct Job read owner."""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from core.application.management.errors import ServiceForbiddenError, ServiceUnavailableError
from core.application.management.query_services import ProjectQueryService
from core.application.management.scope import ScopeContext

PROJECT_ID = "11111111-1111-4111-8111-111111111111"


def _service(project_store, job_provider=None):
    return ProjectQueryService(
        get_logger=lambda: project_store,
        get_job_store=job_provider,
        serialize_projects_grid_row=lambda row: row,
        get_queue_depth=lambda _project_id: 0,
        get_ready_queue_depth=lambda: 0,
    )


def _projects():
    return SimpleNamespace(
        get_project=Mock(return_value={"id": PROJECT_ID, "account_id": "account"}),
        list_scheduled_jobs=lambda *_args, **_kwargs: [],
    )


def test_detail_uses_distinct_job_reader_and_clamps_page():
    projects = _projects()
    jobs = SimpleNamespace(
        count_jobs=Mock(return_value=5), get_jobs=Mock(return_value=[{"id": "last"}])
    )
    detail = (
        _service(projects, lambda: jobs)
        .get_project_detail(
            PROJECT_ID, page=9, per_page=2, scope=ScopeContext(account_id="account")
        )
        .dump()
    )
    assert detail["total_jobs"] == 5
    assert detail["jobs"] == [{"id": "last"}]
    assert detail["page"] == 3
    jobs.count_jobs.assert_called_once_with(PROJECT_ID)
    jobs.get_jobs.assert_called_once_with(PROJECT_ID, limit=2, offset=4)


@pytest.mark.parametrize(
    "scope",
    [
        ScopeContext(account_id="foreign"),
        ScopeContext(account_id="account", allowed_project_ids=frozenset({"other"})),
    ],
)
def test_scope_denial_precedes_even_resolving_the_job_provider(scope):
    job_provider = Mock()
    service = _service(_projects(), job_provider)
    with pytest.raises(ServiceForbiddenError):
        service.get_project_detail(PROJECT_ID, scope=scope)
    job_provider.assert_not_called()


@pytest.mark.parametrize(
    "reader", [None, SimpleNamespace(), SimpleNamespace(count_jobs=lambda _id: 0)]
)
def test_missing_job_reader_does_not_manufacture_an_empty_project(reader):
    service = _service(_projects(), lambda: reader)
    scope = ScopeContext(account_id="account")
    with pytest.raises(ServiceUnavailableError, match="reader is unavailable"):
        service.get_project_detail(PROJECT_ID, scope=scope)


def test_legacy_aggregate_reader_remains_supported():
    aggregate = _projects()
    aggregate.count_jobs = Mock(return_value=0)
    aggregate.get_jobs = Mock()
    detail = (
        _service(aggregate)
        .get_project_detail(PROJECT_ID, scope=ScopeContext(account_id="account"))
        .dump()
    )
    assert detail["total_jobs"] == 0
    assert detail["jobs"] == []
    aggregate.get_jobs.assert_not_called()


def test_job_reads_use_the_returned_canonical_uuid_not_input_spelling():
    canonical_id = UUID("abcdefab-1111-4111-8111-111111111111")
    projects = _projects()
    projects.get_project.return_value["id"] = canonical_id
    jobs = SimpleNamespace(
        count_jobs=Mock(return_value=1), get_jobs=Mock(return_value=[{"id": "job"}])
    )
    _service(projects, lambda: jobs).get_project_detail(
        str(canonical_id).upper(), scope=ScopeContext(account_id="account")
    )
    jobs.count_jobs.assert_called_once_with(str(canonical_id))
    jobs.get_jobs.assert_called_once_with(str(canonical_id), limit=25, offset=0)
