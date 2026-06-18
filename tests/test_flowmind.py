from __future__ import annotations

from copilot_api.llm import HeuristicProvider
from copilot_api.models import (
    ShareWorkflowRequest,
    TeamMember,
    UpdateWorkflowRequest,
    WorkflowPermissionGrant,
)
from copilot_api.repository import WorkflowRepository
from copilot_api.service import CopilotService


def build_service(tmp_path):
    repository = WorkflowRepository(str(tmp_path / "flowmind.sqlite3"))
    return CopilotService(provider=HeuristicProvider(), repository=repository), repository


def test_create_flowmind_workflow_has_canvas_metadata(tmp_path):
    service, repository = build_service(tmp_path)

    response = service.create(
        "Every morning, check my inbox for emails tagged urgent and create a task in our task list.",
        user_id="user_owner",
    )

    assert response.validation.valid
    assert response.workflow.owner_id == "user_owner"
    assert response.workflow.status == "draft"
    assert [node.role for node in response.workflow.nodes] == ["trigger", "condition", "action"]
    assert all(node.label for node in response.workflow.nodes)
    assert all(node.description for node in response.workflow.nodes)

    summaries = repository.list(user_id="user_owner")
    assert summaries[0].permission == "edit_run"
    assert summaries[0].status == "draft"


def test_scheduled_workflow_requires_schedule_and_can_be_activated(tmp_path):
    service, _ = build_service(tmp_path)
    created = service.create("When an email arrives from Stripe, send a Slack message.", user_id="admin_1")

    try:
        service.update_workflow(created.workflow, UpdateWorkflowRequest(mode="scheduled"), user_id="admin_1")
    except ValueError as exc:
        assert "trigger_schedule" in str(exc)
    else:
        raise AssertionError("Scheduled workflow without trigger_schedule should fail.")

    updated = service.update_workflow(
        created.workflow,
        UpdateWorkflowRequest(mode="scheduled", trigger_schedule="daily at 09:00", status="active"),
        user_id="admin_1",
    )

    assert updated.mode == "scheduled"
    assert updated.trigger_schedule == "daily at 09:00"
    assert updated.status == "active"


def test_share_workflow_controls_dashboard_visibility(tmp_path):
    service, repository = build_service(tmp_path)
    owner = repository.save_member(TeamMember(id="admin_1", email="admin@example.com", role="admin"))
    member = repository.save_member(TeamMember(id="member_1", email="member@example.com", role="member"))
    created = service.create("When an email arrives from Stripe, send a Slack message.", user_id=owner.id)

    assert repository.list(user_id=member.id) == []

    repository.share_workflow(
        created.workflow,
        visibility="restricted",
        team_permission="run",
        members=[WorkflowPermissionGrant(user_id=member.id, permission="run")],
    )

    member_summaries = repository.list(user_id=member.id)
    assert len(member_summaries) == 1
    assert member_summaries[0].permission == "run"
    assert member_summaries[0].visibility == "restricted"


def test_run_workflow_persists_step_level_history(tmp_path):
    service, repository = build_service(tmp_path)
    created = service.create(
        "Every morning, check my inbox for emails tagged urgent and create a task in our task list.",
        user_id="user_owner",
    )

    run = service.run_workflow(created.workflow, input_payload={"source": "manual test"})

    assert run.status == "success"
    assert run.duration_ms is not None
    assert len(run.steps) == len(created.workflow.nodes)
    assert run.steps[0].input == {"source": "manual test"}
    assert run.steps[-1].output["sequence"] == len(created.workflow.nodes)

    runs = repository.list_runs(created.workflow.id)
    assert runs[0].id == run.id
    assert runs[0].steps[-1].label == created.workflow.nodes[-1].label
