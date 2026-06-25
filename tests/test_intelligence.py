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


def test_stripe_email_to_finance_slack_acceptance_requirement(tmp_path):
    repository = WorkflowRepository(str(tmp_path / "stripe-slack.sqlite3"))
    service = CopilotService(provider=HeuristicProvider(), repository=repository)
    instruction = (
        "When I receive an email from Stripe, "
        "send a Slack message to the finance team."
    )

    analysis = service.analyze(instruction)

    assert analysis.extracted.trigger == "I receive an email from Stripe"
    assert analysis.extracted.tasks == [
        "Send a Slack message to the finance team",
    ]
    assert analysis.unsupported_tasks == []
    assert [node.type for node in analysis.proposed_workflow.nodes] == [
        "gmail_trigger",
        "slack_message",
    ]
    gmail, slack = analysis.proposed_workflow.nodes
    assert gmail.config["from_contains"] == "Stripe"
    assert slack.config == {
        "channel_id": "finance",
        "message_template": "New email from {{from}}: {{subject}}",
    }
    assert analysis.proposed_workflow.edges[0].from_ == gmail.id
    assert analysis.proposed_workflow.edges[0].to == slack.id

    built = service.build_analyzed_workflow(
        analysis.proposed_workflow,
        user_id="owner",
        instruction=instruction,
    )

    assert built.owner_id == "owner"
    assert [node.type for node in built.nodes] == [
        "gmail_trigger",
        "slack_message",
    ]
    assert repository.get(built.id) is not None


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


def test_mail_to_task_request_recommends_email_task_workflow_first(tmp_path):
    engine, _ = build_engine(tmp_path)

    analysis = engine.analyze(
        "when i get a mail related to job create a new task in the task section"
    )

    assert analysis.recommendations[0].id == "email-follow-up-tasks"
    assert analysis.recommendations[0].match_score > analysis.recommendations[1].match_score
    assert [node.type for node in analysis.proposed_workflow.nodes] == [
        "gmail_trigger",
        "filter_condition",
        "task_create",
    ]


def test_mail_to_google_calendar_event_is_supported(tmp_path):
    engine, _ = build_engine(tmp_path)

    analysis = engine.analyze(
        "when i get a mail related to job create a new event in google calendar"
    )

    assert analysis.unsupported_tasks == []
    assert analysis.intent.apps == ["Gmail", "Google Calendar"]
    assert analysis.missing_integrations == ["Gmail", "Google Calendar"]
    assert [node.type for node in analysis.proposed_workflow.nodes] == [
        "gmail_trigger",
        "filter_condition",
        "calendar_event_create",
    ]
    calendar = analysis.proposed_workflow.nodes[2]
    assert calendar.config["summary_template"] == "Email: {{subject}}"
    assert "{{body}}" in calendar.config["description_template"]


def test_topic_related_mail_uses_the_topic_as_the_search_value(tmp_path):
    engine, _ = build_engine(tmp_path)

    analysis = engine.analyze(
        "Check my inbox for job related mails and create a task"
    )

    assert analysis.intent.apps == ["Gmail"]
    assert [node.type for node in analysis.proposed_workflow.nodes] == [
        "gmail_trigger",
        "filter_condition",
        "task_create",
    ]
    assert analysis.proposed_workflow.nodes[0].config["search_text"] == "job"
    assert analysis.proposed_workflow.nodes[1].config == {
        "field": "email_text",
        "operator": "contains",
        "value": "job",
    }
    assert analysis.proposed_workflow.nodes[0].description == (
        'Find unread emails containing "job"'
    )


def test_quoted_email_phrase_remains_an_exact_multiword_search(tmp_path):
    engine, _ = build_engine(tmp_path)

    analysis = engine.analyze(
        'Check emails containing phrase "job related mails" and create a task'
    )

    assert analysis.proposed_workflow.nodes[0].config["search_text"] == (
        "job related mails"
    )
    assert analysis.proposed_workflow.nodes[1].config["value"] == (
        "job related mails"
    )


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


def test_newsletter_email_details_are_copied_to_notion_without_invented_actions(tmp_path):
    engine, _ = build_engine(tmp_path)

    analysis = engine.analyze(
        "check email for newletters and add its details to a page in my notion."
    )

    assert analysis.extracted.trigger == "Check email for newletters"
    assert analysis.extracted.tasks == [
        "Add its details to a page in my notion",
    ]
    assert analysis.unsupported_tasks == []
    assert [node.type for node in analysis.proposed_workflow.nodes] == [
        "gmail_trigger",
        "filter_condition",
        "notion_create_page",
    ]
    assert analysis.proposed_workflow.nodes[0].config["search_text"] == "newsletter"
    assert analysis.proposed_workflow.nodes[1].config == {
        "field": "email_text",
        "operator": "contains",
        "value": "newsletter",
    }
    notion = analysis.proposed_workflow.nodes[2]
    assert notion.config["title_template"] == "Email: {{subject}}"
    assert "{{from}}" in notion.config["content_template"]
    assert "{{body}}" in notion.config["content_template"]


def test_recommendations_do_not_inject_unrequested_template_steps(tmp_path):
    engine, _ = build_engine(tmp_path)

    analysis = engine.analyze("When an email arrives, archive it in Dropbox.")

    assert analysis.unsupported_tasks == ["Archive it in Dropbox"]
    assert [node.type for node in analysis.proposed_workflow.nodes] == [
        "gmail_trigger",
    ]
