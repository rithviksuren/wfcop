from __future__ import annotations

from typing import Any

from copilot_api.llm import LLMProvider
from copilot_api.models import Workflow, WorkflowEdge, WorkflowNode
from copilot_api.repository import WorkflowRepository
from copilot_api.service import CopilotService
from copilot_api.validation import repair_workflow, validate_workflow


def error_codes(workflow: Workflow) -> set[str]:
    return {error.code for error in validate_workflow(workflow).errors}


def test_validation_requires_slack_and_gmail_configuration():
    workflow = Workflow(
        nodes=[
            WorkflowNode(
                id="gmail",
                type="gmail_trigger",
                role="trigger",
                config={"from_contains": "   "},
            ),
            WorkflowNode(
                id="slack",
                type="slack_message",
                config={"channel_id": "", "message_template": "New email"},
            ),
        ],
        edges=[WorkflowEdge(from_="gmail", to="slack")],
    )

    result = validate_workflow(workflow)

    assert not result.valid
    missing = {
        (error.node_type, error.field)
        for error in result.errors
        if error.code == "missing_required_config"
    }
    assert ("gmail_trigger", "from_contains") in missing
    assert ("slack_message", "channel_id") in missing


def test_validation_rejects_dangling_duplicate_and_unreachable_connections():
    workflow = Workflow(
        nodes=[
            WorkflowNode(
                id="gmail",
                type="gmail_trigger",
                role="trigger",
                config={"from_contains": "Stripe"},
            ),
            WorkflowNode(
                id="slack",
                type="slack_message",
                config={
                    "channel_id": "finance",
                    "message_template": "New email",
                },
            ),
            WorkflowNode(
                id="task",
                type="task_create",
                config={"list_id": "default", "title_template": "Follow up"},
            ),
        ],
        edges=[
            WorkflowEdge(from_="gmail", to="slack"),
            WorkflowEdge(from_="gmail", to="slack"),
            WorkflowEdge(from_="missing", to="task"),
        ],
    )

    codes = error_codes(workflow)

    assert "duplicate_edge" in codes
    assert "edge_from_missing" in codes
    assert "node_unreachable" in codes


def test_validation_rejects_cycles_and_incoming_trigger_connections():
    workflow = Workflow(
        nodes=[
            WorkflowNode(
                id="gmail",
                type="gmail_trigger",
                role="trigger",
                config={"from_contains": "any sender"},
            ),
            WorkflowNode(
                id="slack",
                type="slack_message",
                config={
                    "channel_id": "general",
                    "message_template": "New email",
                },
            ),
        ],
        edges=[
            WorkflowEdge(from_="gmail", to="slack"),
            WorkflowEdge(from_="slack", to="gmail"),
        ],
    )

    codes = error_codes(workflow)

    assert "workflow_cycle" in codes
    assert "trigger_has_incoming_edge" in codes


def test_validation_requires_schedule_and_valid_config_values():
    workflow = Workflow(
        mode="scheduled",
        nodes=[
            WorkflowNode(
                id="calendar",
                type="calendar_event_trigger",
                role="trigger",
                config={"calendar_id": "primary", "lookahead_minutes": -1},
            ),
            WorkflowNode(
                id="condition",
                type="filter_condition",
                role="condition",
                config={"field": "tag", "operator": "approximately", "value": "urgent"},
            ),
        ],
        edges=[WorkflowEdge(from_="calendar", to="condition")],
    )

    result = validate_workflow(workflow)

    assert not result.valid
    assert "schedule_missing" in error_codes(workflow)
    invalid_fields = {
        error.field
        for error in result.errors
        if error.code == "invalid_config_value"
    }
    assert invalid_fields == {"lookahead_minutes", "operator"}


def test_safe_repair_fills_defaults_and_repairs_graph():
    workflow = Workflow(
        nodes=[
            WorkflowNode(
                id="gmail",
                type="gmail_trigger",
                config={"from_contains": ""},
            ),
            WorkflowNode(
                id="slack",
                type="slack_message",
                config={"message_template": "New email"},
            ),
        ],
        edges=[
            WorkflowEdge(from_="slack", to="gmail"),
            WorkflowEdge(from_="slack", to="missing"),
        ],
    )

    repaired = repair_workflow(workflow)

    assert validate_workflow(repaired).valid
    assert repaired.nodes[0].role == "trigger"
    assert repaired.nodes[0].config["from_contains"] == "any sender"
    assert repaired.nodes[1].config["channel_id"] == "general"
    assert repaired.edges == [WorkflowEdge(from_="gmail", to="slack")]


def test_generation_uses_validation_feedback_before_returning(tmp_path):
    class FeedbackAwareProvider(LLMProvider):
        name = "feedback-aware"

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def generate(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((task, payload))
            if len(self.calls) == 1:
                workflow = Workflow(
                    nodes=[
                        WorkflowNode(
                            id="gmail",
                            type="gmail_trigger",
                            role="trigger",
                            config={"from_contains": "Stripe"},
                        ),
                        WorkflowNode(
                            id="slack",
                            type="slack_message",
                            config={"message_template": "New email"},
                        ),
                    ],
                    edges=[WorkflowEdge(from_="gmail", to="slack")],
                )
            else:
                assert task == "fix"
                errors = payload["validation_errors"]
                assert any(
                    error["node_type"] == "slack_message"
                    and error["field"] == "channel_id"
                    for error in errors
                )
                workflow = Workflow.model_validate(payload["workflow"])
                workflow.nodes[1].config["channel_id"] = "finance"
            return {"workflow": workflow.model_dump(by_alias=True)}

    provider = FeedbackAwareProvider()
    service = CopilotService(
        provider=provider,
        repository=WorkflowRepository(str(tmp_path / "feedback.sqlite3")),
    )

    response = service.create(
        "When I receive an email from Stripe, send a Slack message to the finance team."
    )

    assert len(provider.calls) == 2
    assert response.validation.valid
    assert response.workflow.nodes[1].config["channel_id"] == "finance"
