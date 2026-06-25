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
    assert response.workflow.nodes[1].config["message_template"] == (
        "New email from {{from}}: {{subject}}"
    )
    assert response.workflow.edges[0].from_ == response.workflow.nodes[0].id


def test_modify_adds_notion_page(tmp_path):
    service = build_service(tmp_path)
    created = service.create("When I receive an email from Stripe, send a Slack message to the finance team.")
    original_nodes = [node.model_copy(deep=True) for node in created.workflow.nodes]
    original_edges = [edge.model_copy(deep=True) for edge in created.workflow.edges]

    modified = service.modify(created.workflow, "Also create a Notion page whenever an email arrives.")

    assert modified.validation.valid
    assert [node.type for node in modified.workflow.nodes] == [
        "gmail_trigger",
        "slack_message",
        "notion_create_page",
    ]
    assert modified.workflow.nodes[:2] == original_nodes
    assert original_edges[0] in modified.workflow.edges
    notion = modified.workflow.nodes[-1]
    assert notion.config["title_template"] == "Email: {{subject}}"
    assert "{{body}}" in notion.config["content_template"]
    assert any(
        edge.from_ == modified.workflow.nodes[0].id and edge.to == notion.id
        for edge in modified.workflow.edges
    )
    assert [operation.op for operation in modified.operations].count("add_node") == 1
    assert [operation.op for operation in modified.operations].count("connect_nodes") == 1
    assert not any(
        operation.op in {"remove_node", "disconnect_nodes"}
        for operation in modified.operations
    )
    assert modified.workflow.version == created.workflow.version + 1


def test_additive_modify_preserves_existing_steps_when_provider_drops_them(tmp_path):
    class DestructiveModifyProvider(LLMProvider):
        name = "destructive-modifier"

        def generate(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
            if task == "create":
                return HeuristicProvider().generate(task, payload)
            workflow = Workflow(
                name="Notion only",
                nodes=[
                    WorkflowNode(
                        id="replacement",
                        type="notion_create_page",
                        config={
                            "data_source_id": "default",
                            "title_template": "New page",
                            "content_template": "",
                        },
                    )
                ],
            )
            return {
                "workflow": workflow.model_dump(by_alias=True),
                "provider": self.name,
            }

    service = CopilotService(
        provider=DestructiveModifyProvider(),
        repository=WorkflowRepository(str(tmp_path / "safe-modify.sqlite3")),
    )
    created = service.create(
        "When I receive an email from Stripe, send a Slack message to the finance team."
    )

    modified = service.modify(
        created.workflow,
        "Also create a Notion page whenever an email arrives.",
    )

    assert modified.validation.valid
    assert [node.type for node in modified.workflow.nodes] == [
        "gmail_trigger",
        "slack_message",
        "notion_create_page",
    ]
    assert modified.workflow.nodes[0].config["from_contains"] == "Stripe"
    assert modified.workflow.nodes[1].config["channel_id"] == "finance"


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


def test_fix_accepts_external_error_shape_and_uses_workflow_context(tmp_path):
    service = build_service(tmp_path)
    created = service.create(
        "When I receive an email from Stripe, send a Slack message to the finance team."
    )
    slack = next(
        node for node in created.workflow.nodes if node.type == "slack_message"
    )
    del slack.config["channel_id"]
    external_error = {
        "node": "slack_message",
        "error": "channel_id missing",
    }

    normalized = ValidationErrorDetail.model_validate(external_error)
    assert normalized.node_type == "slack_message"
    assert normalized.field == "channel_id"
    assert normalized.code == "missing_required_config"

    fixed = service.fix(
        created.workflow,
        "Fix the workflow.",
        [external_error],
    )

    assert fixed.validation.valid
    assert [node.type for node in fixed.workflow.nodes] == [
        "gmail_trigger",
        "slack_message",
    ]
    repaired_slack = next(
        node for node in fixed.workflow.nodes if node.type == "slack_message"
    )
    assert repaired_slack.config["channel_id"] == "finance"
    assert fixed.workflow.edges == created.workflow.edges
    assert any(
        operation.op == "update_node"
        and operation.node_id == repaired_slack.id
        for operation in fixed.operations
    )


def test_fix_preserves_original_graph_when_provider_drops_nodes(tmp_path):
    class DestructiveFixProvider(LLMProvider):
        name = "destructive-fixer"

        def generate(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
            workflow = Workflow(name="Empty repair")
            return {
                "workflow": workflow.model_dump(by_alias=True),
                "provider": self.name,
            }

    service = CopilotService(
        provider=DestructiveFixProvider(),
        repository=WorkflowRepository(str(tmp_path / "safe-fix.sqlite3")),
    )
    workflow = Workflow(
        name="Stripe Emails to Finance Slack",
        nodes=[
            WorkflowNode(
                id="node_1",
                type="gmail_trigger",
                config={"from_contains": "Stripe", "search_text": ""},
            ),
            WorkflowNode(
                id="node_2",
                type="slack_message",
                config={"message_template": "New email from {{from}}: {{subject}}"},
            ),
        ],
        edges=[WorkflowEdge(from_="node_1", to="node_2")],
    )

    fixed = service.fix(
        workflow,
        "Fix the workflow.",
        [{"node": "slack_message", "error": "channel_id missing"}],
    )

    assert fixed.validation.valid
    assert [node.type for node in fixed.workflow.nodes] == [
        "gmail_trigger",
        "slack_message",
    ]
    assert fixed.workflow.nodes[1].config["channel_id"] == "finance"
    assert fixed.workflow.edges == [WorkflowEdge(from_="node_1", to="node_2")]


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
    assert "Gmail checks for a new unread email from Stripe." in response.explanation
    assert "It sends a Slack message to #finance" in response.explanation
    assert "New email from {{from}}: {{subject}}" in response.explanation
    assert response.validation.valid


def test_explain_describes_parallel_actions_without_exposing_notion_id(tmp_path):
    service = build_service(tmp_path)
    created = service.create(
        "When I receive an email from Stripe, send a Slack message to the finance team."
    )
    modified = service.modify(
        created.workflow,
        "Also create a Notion page whenever an email arrives.",
    )
    notion = next(
        node for node in modified.workflow.nodes if node.type == "notion_create_page"
    )
    notion.config["data_source_id"] = "bc1211ca-e3f1-4939-ae34-5260b16f627c"

    response = service.explain(modified.workflow, "Explain this workflow.")

    assert "Actions run in parallel:" in response.explanation
    assert "It sends a Slack message to #finance" in response.explanation
    assert 'It creates a Notion page titled "Email: {{subject}}"' in response.explanation
    assert "sender, recipient, date, subject, email body" in response.explanation
    assert "bc1211ca-e3f1-4939-ae34-5260b16f627c" not in response.explanation


def test_explain_works_when_ai_provider_is_unavailable(tmp_path):
    class UnavailableProvider(LLMProvider):
        name = "unavailable-ai"

        def generate(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
            raise LLMError("Provider unavailable.")

    service = CopilotService(
        provider=UnavailableProvider(),
        repository=WorkflowRepository(str(tmp_path / "explain.sqlite3")),
    )
    workflow = Workflow(
        name="Stripe Emails to Finance Slack",
        nodes=[
            WorkflowNode(
                id="node_1",
                type="gmail_trigger",
                role="trigger",
                config={"from_contains": "Stripe", "search_text": ""},
            ),
            WorkflowNode(
                id="node_2",
                type="slack_message",
                config={
                    "channel_id": "finance",
                    "message_template": "New email from {{from}}: {{subject}}",
                },
            ),
        ],
        edges=[WorkflowEdge(from_="node_1", to="node_2")],
    )

    response = service.explain(workflow, "Explain this workflow.")

    assert "Gmail checks for a new unread email from Stripe." in response.explanation
    assert "It sends a Slack message to #finance" in response.explanation
    assert response.provider == "unavailable-ai"


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


def test_fallback_treats_google_calendar_event_creation_as_action():
    provider = HeuristicProvider()

    result = provider.generate(
        "create",
        {
            "instruction": (
                "When I receive job-related mails, create a new event in google calendar"
            )
        },
    )

    workflow = Workflow.model_validate(result["workflow"])
    assert [node.type for node in workflow.nodes] == [
        "gmail_trigger",
        "filter_condition",
        "calendar_event_create",
    ]
    assert validate_workflow(workflow).valid


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


def test_job_related_mails_match_email_text_containing_job(tmp_path):
    service = build_service(tmp_path)

    created = service.create(
        "When I receive job-related mails, create a task in our task list."
    )

    workflow = created.workflow
    assert [node.type for node in workflow.nodes] == [
        "gmail_trigger",
        "filter_condition",
        "task_create",
    ]
    assert workflow.nodes[0].config["search_text"] == "job"
    assert workflow.nodes[1].config["value"] == "job"

    run = service.run_workflow(
        workflow,
        input_payload={
            "email": {
                "from": "recruiter@example.com",
                "subject": "A new job opening",
                "body": "This role matches your profile.",
            }
        },
    )

    assert run.status == "success"
    assert run.steps[1].output["condition"]["matched"] is True
    assert run.steps[2].output["created_task"]["title"] == (
        "Email follow-up: A new job opening"
    )


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
