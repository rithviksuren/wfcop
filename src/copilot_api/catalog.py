from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NodeDefinition:
    type: str
    description: str
    role: str = "action"
    required_config: tuple[str, ...] = ()
    defaults: dict[str, Any] = field(default_factory=dict)


NODE_CATALOG: dict[str, NodeDefinition] = {
    "gmail_trigger": NodeDefinition(
        type="gmail_trigger",
        description="Finds a new unread email, optionally searching for specific text",
        role="trigger",
        required_config=("from_contains",),
        defaults={"from_contains": "any sender", "search_text": ""},
    ),
    "calendar_event_trigger": NodeDefinition(
        type="calendar_event_trigger",
        description="Checks Google Calendar for upcoming events",
        role="trigger",
        required_config=("calendar_id", "lookahead_minutes"),
        defaults={"calendar_id": "primary", "lookahead_minutes": 60},
    ),
    "slack_message": NodeDefinition(
        type="slack_message",
        description="Send a Slack message",
        required_config=("channel_id", "message_template"),
        defaults={"channel_id": "general", "message_template": "New workflow event received."},
    ),
    "filter_condition": NodeDefinition(
        type="filter_condition",
        description="Continue only when a field or the combined email text matches",
        role="condition",
        required_config=("field", "operator", "value"),
        defaults={"field": "tag", "operator": "equals", "value": "urgent"},
    ),
    "task_create": NodeDefinition(
        type="task_create",
        description="Create a task in the team task list",
        required_config=("list_id", "title_template"),
        defaults={"list_id": "default", "title_template": "Email follow-up: {{subject}}"},
    ),
    "reminder_create": NodeDefinition(
        type="reminder_create",
        description="Create a reminder notification",
        required_config=("channel", "message_template"),
        defaults={"channel": "in_app", "message_template": "Upcoming calendar event"},
    ),
    "notion_create_page": NodeDefinition(
        type="notion_create_page",
        description="Create a Notion page with a title and content",
        required_config=("title_template",),
        defaults={
            "data_source_id": "default",
            "title_template": "New workflow event",
            "content_template": "",
        },
    ),
    "webhook": NodeDefinition(
        type="webhook",
        description="Receive HTTP requests",
        role="trigger",
        required_config=("path",),
        defaults={"path": "/webhooks/default"},
    ),
    "teams_message": NodeDefinition(
        type="teams_message",
        description="Send a Microsoft Teams message",
        required_config=("channel_id", "message_template"),
        defaults={"channel_id": "general", "message_template": "New workflow event received."},
    ),
    "form_submission_trigger": NodeDefinition(
        type="form_submission_trigger",
        description="Starts when a customer submits a form",
        role="trigger",
        required_config=("form_id",),
        defaults={"form_id": "any"},
    ),
    "jira_ticket_create": NodeDefinition(
        type="jira_ticket_create",
        description="Create a Jira issue",
        required_config=("project_key", "summary_template", "description_template"),
        defaults={
            "project_key": "LEADS",
            "summary_template": "New lead: {{name}}",
            "description_template": "Submitted by {{email}}",
        },
    ),
    "crm_update": NodeDefinition(
        type="crm_update",
        description="Create or update a contact in the connected CRM",
        required_config=("provider", "email_field"),
        defaults={"provider": "hubspot", "email_field": "email"},
    ),
    "email_send": NodeDefinition(
        type="email_send",
        description="Send a follow-up email through Gmail",
        required_config=("to_template", "subject_template", "body_template"),
        defaults={
            "to_template": "{{email}}",
            "subject_template": "Thanks for contacting us",
            "body_template": "Hi {{name}}, thanks for your interest. Our team will follow up shortly.",
        },
    ),
}


def catalog_for_prompt() -> list[dict[str, Any]]:
    return [
        {
            "type": definition.type,
            "description": definition.description,
            "role": definition.role,
            "required_config": list(definition.required_config),
            "defaults": definition.defaults,
        }
        for definition in NODE_CATALOG.values()
    ]
