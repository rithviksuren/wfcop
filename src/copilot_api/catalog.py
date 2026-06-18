from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NodeDefinition:
    type: str
    description: str
    required_config: tuple[str, ...] = ()
    defaults: dict[str, Any] = field(default_factory=dict)


NODE_CATALOG: dict[str, NodeDefinition] = {
    "gmail_trigger": NodeDefinition(
        type="gmail_trigger",
        description="Triggers when a new email arrives",
        required_config=("from_contains",),
        defaults={"from_contains": "any sender"},
    ),
    "calendar_event_trigger": NodeDefinition(
        type="calendar_event_trigger",
        description="Checks Google Calendar for upcoming events",
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
        description="Continue only when a condition is met",
        required_config=("field", "operator", "value"),
        defaults={"field": "tag", "operator": "equals", "value": "urgent"},
    ),
    "task_create": NodeDefinition(
        type="task_create",
        description="Create a task in the team task list",
        required_config=("list_id", "title_template"),
        defaults={"list_id": "default", "title_template": "Follow up on urgent email"},
    ),
    "reminder_create": NodeDefinition(
        type="reminder_create",
        description="Create a reminder notification",
        required_config=("channel", "message_template"),
        defaults={"channel": "in_app", "message_template": "Upcoming calendar event"},
    ),
    "notion_create_page": NodeDefinition(
        type="notion_create_page",
        description="Create a Notion page",
        required_config=("database_id", "title_template"),
        defaults={"database_id": "default", "title_template": "New workflow event"},
    ),
    "webhook": NodeDefinition(
        type="webhook",
        description="Receive HTTP requests",
        required_config=("path",),
        defaults={"path": "/webhooks/default"},
    ),
    "teams_message": NodeDefinition(
        type="teams_message",
        description="Send a Microsoft Teams message",
        required_config=("channel_id", "message_template"),
        defaults={"channel_id": "general", "message_template": "New workflow event received."},
    ),
}


def catalog_for_prompt() -> list[dict[str, Any]]:
    return [
        {
            "type": definition.type,
            "description": definition.description,
            "required_config": list(definition.required_config),
            "defaults": definition.defaults,
        }
        for definition in NODE_CATALOG.values()
    ]
