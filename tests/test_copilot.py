from __future__ import annotations

from typing import Any

from copilot_api.llm import FallbackProvider, HeuristicProvider, LLMError, LLMProvider
from copilot_api.models import ValidationErrorDetail, Workflow, WorkflowEdge, WorkflowNode
from copilot_api.repository import WorkflowRepository
from copilot_api.service import CopilotService
from copilot_api.validation import validate_workflow


def build_service(tmp_path):
    return CopilotService(provider=HeuristicProvider(), repository=WorkflowRepository(str(tmp_path / "workflows.sqlite3")))


def test_create_stripe_to_slack_workflow(tmp_path):
    service = build_service(tmp_path)

    response = service.create("When I receive an email from Stripe, send a Slack message to the finance team.")

    assert response.validation.valid
    assert [node.type for node in response.workflow.nodes] == ["gmail_trigger", "slack_message"]
    assert response.workflow.nodes[0].config["from_contains"] == "Stripe"
    assert response.workflow.nodes[1].config["channel_id"] == "finance"
    assert response.workflow.edges[0].from_ == response.workflow.nodes[0].id


def test_modify_adds_notion_page(tmp_path):
    service = build_service(tmp_path)
    created = service.create("When I receive an email from Stripe, send a Slack message to the finance team.")

    modified = service.modify(created.workflow, "Also create a Notion page whenever an email arrives.")

    assert modified.validation.valid
    assert "notion_create_page" in {node.type for node in modified.workflow.nodes}
    assert any(operation.op == "add_node" for operation in modified.operations)
    assert any(edge.to == modified.workflow.nodes[-1].id for edge in modified.workflow.edges)


def test_fix_adds_missing_slack_channel(tmp_path):
    service = build_service(tmp_path)
    workflow = Workflow(
        nodes=[
            WorkflowNode(id="node_1", type="gmail_trigger", config={"from_contains": "Stripe"}),
            WorkflowNode(id="node_2", type="slack_message", config={"message_template": "New email"}),
        ],
        edges=[WorkflowEdge(from_="node_1", to="node_2")],
    )
    errors = validate_workflow(workflow).errors

    fixed = service.fix(workflow, "Fix the workflow.", errors)

    assert fixed.validation.valid
    slack = next(node for node in fixed.workflow.nodes if node.type == "slack_message")
    assert slack.config["channel_id"] == "general"
    assert any(operation.op == "update_node" for operation in fixed.operations)


def test_validation_rejects_bad_edges():
    workflow = Workflow(
        nodes=[WorkflowNode(id="node_1", type="gmail_trigger", config={"from_contains": "Stripe"})],
        edges=[WorkflowEdge(from_="node_1", to="missing")],
    )

    result = validate_workflow(workflow)

    assert not result.valid
    assert any(error.code == "edge_to_missing" for error in result.errors)


def test_explain_returns_human_readable_text(tmp_path):
    service = build_service(tmp_path)
    created = service.create("When I receive an email from Stripe, send a Slack message to the finance team.")

    response = service.explain(created.workflow, "Explain this workflow.")

    assert response.explanation
    assert "workflow" in response.explanation.lower()


def test_provider_falls_back_when_openai_is_rate_limited():
    class RateLimitedProvider(LLMProvider):
        name = "openai"

        def generate(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
            raise LLMError("OpenAI is rate limited or out of quota.")

    provider = FallbackProvider(primary=RateLimitedProvider(), fallback=HeuristicProvider())

    result = provider.generate("create", {"instruction": "Check email tagged urgent and create a task."})

    workflow = Workflow.model_validate(result["workflow"])
    assert [node.type for node in workflow.nodes] == ["gmail_trigger", "filter_condition", "task_create"]
    assert "Generated locally" in result["explanation"]


def test_fallback_understands_calendar_reminder_workflows():
    provider = HeuristicProvider()

    result = provider.generate("create", {"instruction": "Check google calendar for events and set reminder"})

    workflow = Workflow.model_validate(result["workflow"])
    assert [node.type for node in workflow.nodes] == ["calendar_event_trigger", "reminder_create"]
