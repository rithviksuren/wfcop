from __future__ import annotations

from copilot_api.intelligence import WorkflowIntelligenceEngine
from copilot_api.llm import HeuristicProvider
from copilot_api.repository import WorkflowRepository
from copilot_api.service import CopilotService


EXAMPLE_REQUEST = (
    "Whenever a customer fills a form, create a Jira ticket, notify Slack, "
    "update CRM and send a follow-up email."
)


def build_engine(tmp_path):
    repository = WorkflowRepository(str(tmp_path / "intelligence.sqlite3"))
    return WorkflowIntelligenceEngine(repository), repository


def test_natural_language_is_extracted_into_structured_request(tmp_path):
    engine, _ = build_engine(tmp_path)

    analysis = engine.analyze(EXAMPLE_REQUEST)

    assert analysis.extracted.trigger == "A customer fills a form"
    assert analysis.extracted.tasks == [
        "Create a Jira ticket",
        "Notify Slack",
        "Update CRM",
        "Send a follow-up email",
    ]
    assert analysis.extracted.goal == "Lead management"


def test_intent_engine_identifies_business_context_and_apps(tmp_path):
    engine, _ = build_engine(tmp_path)

    intent = engine.analyze(EXAMPLE_REQUEST).intent

    assert intent.industry == "sales"
    assert intent.workflow_type == "lead_management"
    assert intent.priority == "medium"
    assert intent.apps == ["Jira", "Slack", "HubSpot", "Gmail"]


def test_rag_retrieves_proven_lead_management_workflow(tmp_path):
    engine, _ = build_engine(tmp_path)

    analysis = engine.analyze(EXAMPLE_REQUEST)

    assert analysis.recommendations[0].id == "sales-lead-intake"
    assert analysis.recommendations[0].match_score >= 0.8
    assert analysis.retrieval.knowledge_base == "flowmind-proven-workflows"
    assert "sales-lead-intake" in analysis.retrieval.retrieved_template_ids
    assert analysis.retrieval.guidance


def test_analysis_proposes_exact_order_without_saving(tmp_path):
    engine, repository = build_engine(tmp_path)

    analysis = engine.analyze(EXAMPLE_REQUEST)

    assert repository.list() == []
    assert [node.type for node in analysis.proposed_workflow.nodes] == [
        "form_submission_trigger",
        "jira_ticket_create",
        "slack_message",
        "crm_update",
        "email_send",
    ]
    assert analysis.missing_integrations == ["Jira", "Slack", "HubSpot", "Gmail"]


def test_approved_analysis_is_saved_only_after_build(tmp_path):
    repository = WorkflowRepository(str(tmp_path / "approved.sqlite3"))
    service = CopilotService(provider=HeuristicProvider(), repository=repository)
    analysis = service.analyze(EXAMPLE_REQUEST)

    assert repository.list(user_id="owner") == []

    built = service.build_analyzed_workflow(analysis.proposed_workflow, user_id="owner")

    assert built.owner_id == "owner"
    assert len(repository.list(user_id="owner")) == 1


def test_mail_related_to_topic_becomes_gmail_search_filter_and_task(tmp_path):
    engine, _ = build_engine(tmp_path)

    analysis = engine.analyze("I get a mail related to job search, create a task")

    assert analysis.intent.workflow_type == "email_management"
    assert analysis.intent.apps == ["Gmail"]
    assert analysis.extracted.goal == "Email follow-up"
    assert [node.type for node in analysis.proposed_workflow.nodes] == [
        "gmail_trigger",
        "filter_condition",
        "task_create",
    ]
    assert analysis.proposed_workflow.nodes[0].config["search_text"] == "job search"
    assert analysis.proposed_workflow.nodes[1].config == {
        "field": "email_text",
        "operator": "contains",
        "value": "job search",
    }


def test_legacy_mail_webhook_is_repaired(tmp_path):
    engine, _ = build_engine(tmp_path)
    workflow = engine.propose_workflow(
        engine.extract_request("Webhook request, create a task"),
        engine.understand_intent(
            "Webhook request, create a task",
            engine.extract_request("Webhook request, create a task"),
        ),
    )
    workflow.nodes[0].label = "I get a mail related to job search"
    workflow.nodes[0].description = "Receive HTTP requests"

    changed = engine.repair_legacy_email_workflow(workflow)

    assert changed is True
    assert [node.type for node in workflow.nodes] == [
        "gmail_trigger",
        "filter_condition",
        "task_create",
    ]
    assert workflow.nodes[0].config["search_text"] == "job search"
