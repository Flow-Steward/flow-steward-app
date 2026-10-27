"""Job-domain CLI bindings to shared job, approval, and health services."""

from __future__ import annotations

from collections.abc import Mapping

from core.application.management.command_models import (
    ApprovalDecisionInput,
    JobAcknowledgeInput,
)
from core.application.management.command_specs.jobs import (
    JobAcknowledgeCommandInput,
    JobApprovalCommandInput,
    JobCancelCommandInput,
    JobErrorsCommandInput,
    JobHealthSnapshotCommandInput,
    JobIdCommandInput,
    JobLogsCommandInput,
    JobMessagesCommandInput,
    JobShowCommandInput,
    JobsListCommandInput,
)
from core.application.management.errors import ServiceUnavailableError
from core.entrypoints.cli.platform.service_bindings.common import (
    RuntimeServices,
    ServiceFactory,
    await_maybe,
    cli_operator_scope,
)


async def jobs_list(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, JobsListCommandInput)
        else JobsListCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.job_query_service.list_jobs(
            project_id=str(command.project_id or "").strip() or None,
            status=str(command.status or "").strip(),
            limit=command.limit,
            offset=command.offset,
            scope=cli_operator_scope(project_id=command.project_id),
        )
    )


async def jobs_show(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, JobShowCommandInput)
        else JobShowCommandInput.model_validate(value)
    )
    if not command.include_details:
        from core.application.management.job_public_projection import (
            job_public_summary_projection,
        )

        status = await await_maybe(
            runtime.job_query_service.get_job_status(
                command.job_id,
                scope=cli_operator_scope(),
            )
        )
        return job_public_summary_projection(status)
    return await await_maybe(
        runtime.job_query_service.get_job_detail_contract(
            command.job_id,
            scope=cli_operator_scope(),
            user_role="operator",
        )
    )


async def jobs_status(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value if isinstance(value, JobIdCommandInput) else JobIdCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.job_query_service.get_job_status(command.job_id, scope=cli_operator_scope())
    )


async def jobs_logs(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, JobLogsCommandInput)
        else JobLogsCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.job_query_service.list_logs(
            command.job_id,
            since_id=command.since_id,
            before_id=command.before_id,
            newest=command.newest,
            limit=command.limit,
            scope=cli_operator_scope(),
        )
    )


async def jobs_messages(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, JobMessagesCommandInput)
        else JobMessagesCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.job_query_service.list_messages(
            command.job_id,
            since_id=command.since_id,
            before_id=command.before_id,
            newest=command.newest,
            step_id=command.step_id,
            producer=command.producer,
            skill=command.skill,
            status=command.status,
            limit=command.limit,
            scope=cli_operator_scope(),
        )
    )


async def jobs_errors(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, JobErrorsCommandInput)
        else JobErrorsCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.job_query_service.list_errors(
            command.job_id,
            limit=command.limit,
            scope=cli_operator_scope(),
        )
    )


async def jobs_approve(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, JobApprovalCommandInput)
        else JobApprovalCommandInput.model_validate(value)
    )
    decision = ApprovalDecisionInput(
        approved=True,
        action=str(command.action or "approve").strip() or "approve",
        comment=command.comment,
        approval_id=command.approval_id,
    )
    return await await_maybe(
        runtime.approval_command_service.resume_task(
            thread_id=command.thread_id,
            scope=cli_operator_scope(),
            decision=decision,
        )
    )


async def jobs_reject(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, JobApprovalCommandInput)
        else JobApprovalCommandInput.model_validate(value)
    )
    decision = ApprovalDecisionInput(
        approved=False,
        action=str(command.action or "reject").strip() or "reject",
        comment=command.comment,
        approval_id=command.approval_id,
    )
    return await await_maybe(
        runtime.approval_command_service.resume_task(
            thread_id=command.thread_id,
            scope=cli_operator_scope(),
            decision=decision,
        )
    )


async def jobs_ack(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, JobAcknowledgeCommandInput)
        else JobAcknowledgeCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.job_command_service.acknowledge_job(
            command.job_id,
            JobAcknowledgeInput(actor=command.actor),
            scope=cli_operator_scope(),
        )
    )


async def jobs_unack(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value if isinstance(value, JobIdCommandInput) else JobIdCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.job_command_service.unacknowledge_job(
            command.job_id,
            scope=cli_operator_scope(),
        )
    )


async def jobs_cancel(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value
        if isinstance(value, JobCancelCommandInput)
        else JobCancelCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.job_command_service.cancel_job(
            command.job_id,
            actor=command.actor,
            comment=command.comment,
            scope=cli_operator_scope(),
        )
    )


async def jobs_restart(runtime: RuntimeServices, value: object, **_kwargs: object) -> object:
    command = (
        value if isinstance(value, JobIdCommandInput) else JobIdCommandInput.model_validate(value)
    )
    return await await_maybe(
        runtime.job_command_service.restart_job(command.job_id, scope=cli_operator_scope())
    )


async def jobs_health_snapshot(
    runtime: RuntimeServices, value: object, **_kwargs: object
) -> object:
    command = (
        value
        if isinstance(value, JobHealthSnapshotCommandInput)
        else JobHealthSnapshotCommandInput.model_validate(value)
    )
    snapshot = await await_maybe(
        runtime.ops_health_snapshot_service.collect(
            base_url=command.base_url,
            verify_ssl=command.verify_ssl,
            timeout_seconds=command.timeout_seconds,
            dsn=command.dsn,
            stale_heartbeat_seconds=command.stale_heartbeat_seconds,
        )
    )
    if isinstance(snapshot, Mapping) and not bool(snapshot.get("ok")):
        failed_components = [
            name
            for name in ("service", "database")
            if not bool(
                snapshot.get(name, {}).get("ok")
                if isinstance(snapshot.get(name), Mapping)
                else False
            )
        ]
        failed_summary = ", ".join(failed_components) or "dependency"
        raise ServiceUnavailableError(f"{failed_summary} health check failed")
    return snapshot


JOB_SERVICE_FACTORIES: dict[str, ServiceFactory] = {
    "jobs.list": jobs_list,
    "jobs.show": jobs_show,
    "jobs.status": jobs_status,
    "jobs.logs": jobs_logs,
    "jobs.messages": jobs_messages,
    "jobs.errors": jobs_errors,
    "jobs.approve": jobs_approve,
    "jobs.reject": jobs_reject,
    "jobs.ack": jobs_ack,
    "jobs.unack": jobs_unack,
    "jobs.cancel": jobs_cancel,
    "jobs.restart": jobs_restart,
    "jobs.health-snapshot": jobs_health_snapshot,
}
