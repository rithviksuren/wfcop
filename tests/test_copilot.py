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


def test_jobs_email_instruction_generates_precise_scheduled_workflow(tmp_path):
    service = build_service(tmp_path)

    response = service.create(
        'Every morning, check my inbox for emails with word "Jobs" and create a task in our task list.'
    )

    workflow = response.workflow
    assert [node.type for node in workflow.nodes] == [
        "gmail_trigger",
        "filter_condition",
        "task_create",
    ]
    assert workflow.mode == "scheduled"
    assert workflow.trigger_schedule == "daily at 09:00"
    assert workflow.status == "active"
    assert workflow.nodes[0].config["search_text"] == "Jobs"
    assert workflow.nodes[1].config == {
        "field": "email_text",
        "operator": "contains",
        "value": "Jobs",
    }
    assert workflow.nodes[2].config["title_template"] == "Email follow-up: {{subject}}"
    assert "Jobs" in workflow.name


def test_service_repairs_incomplete_ai_workflow(tmp_path):
    class IncompleteProvider(LLMProvider):
        name = "incomplete-ai"

        def generate(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
            workflow = Workflow(
                name="Gmail Trigger -> Task Create",
                nodes=[
                    WorkflowNode(id="a", type="gmail_trigger", config={"from_contains": "any sender"}),
                    WorkflowNode(
                        id="b",
                        type="task_create",
                        config={"list_id": "default", "title_template": "Follow up on urgent email"},
                    ),
                ],
                edges=[WorkflowEdge(from_="a", to="b")],
            )
            return {"workflow": workflow.model_dump(by_alias=True), "provider": self.name}

    service = CopilotService(
        provider=IncompleteProvider(),
        repository=WorkflowRepository(str(tmp_path / "repaired.sqlite3")),
    )

    response = service.create(
        'Every morning, check my inbox for emails with word "Jobs" and create a task in our task list.'
    )

    assert [node.type for node in response.workflow.nodes] == [
        "gmail_trigger",
        "filter_condition",
        "task_create",
    ]
    assert response.workflow.nodes[1].config["value"] == "Jobs"
    assert response.provider == "incomplete-ai"


def test_jobs_filter_matches_subject_or_body_and_uses_subject_for_task(tmp_path):
    service = build_service(tmp_path)
    created = service.create(
        'Every morning, check my inbox for emails with word "Jobs" and create a task in our task list.'
    )

    run = service.run_workflow(
        created.workflow,
        input_payload={
            "email": {
                "from": "alerts@example.com",
                "subject": "New Jobs for Python developers",
                "body": "Here are today’s openings.",
            }
        },
    )

    assert run.status == "success"
    assert len(run.steps) == 3
    assert run.steps[1].output["condition"]["matched"] is True
    assert run.steps[2].output["created_task"]["title"] == "Email follow-up: New Jobs for Python developers"
