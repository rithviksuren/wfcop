from __future__ import annotations

from dataclasses import dataclass

from .email_language import extract_email_search_text, has_email_reference
from .models import Workflow, WorkflowEdge, WorkflowNode


@dataclass
class WorkflowIntent:
    email_requested: bool = False
    task_requested: bool = False
    filter_field: str | None = None
    filter_operator: str | None = None
    filter_value: str | None = None
    schedule: str | None = None


def extract_workflow_intent(instruction: str) -> WorkflowIntent:
    text = instruction.strip()
    lowered = text.lower()
    intent = WorkflowIntent(
        email_requested=has_email_reference(lowered),
        task_requested="task" in lowered or "task list" in lowered,
    )

    search_text = extract_email_search_text(text)
    if intent.email_requested:
        if any(
            term in lowered
            for term in ("tagged urgent", "tag urgent", "urgent email", "emails tagged urgent")
        ):
            intent.filter_field = "tag"
            intent.filter_operator = "equals"
            intent.filter_value = "urgent"
        elif search_text:
            intent.filter_field = "email_text"
            intent.filter_operator = "contains"
            intent.filter_value = search_text
    elif any(term in lowered for term in ("tagged urgent", "tag urgent", "urgent email", "emails tagged urgent")):
        intent.filter_field = "tag"
        intent.filter_operator = "equals"
        intent.filter_value = "urgent"

    if "every morning" in lowered or "each morning" in lowered or "daily morning" in lowered:
        intent.schedule = "daily at 09:00"
    elif "every weekday" in lowered or "weekdays" in lowered:
        intent.schedule = "weekdays at 09:00"
    elif "every hour" in lowered or "hourly" in lowered:
        intent.schedule = "hourly"

    return intent


def enforce_workflow_intent(workflow: Workflow, instruction: str) -> Workflow:
    intent = extract_workflow_intent(instruction)
    if not intent.email_requested and not intent.task_requested and not intent.filter_value and not intent.schedule:
        return workflow

    nodes_by_type = {node.type: node for node in workflow.nodes}
    ordered_nodes: list[WorkflowNode] = []

    if intent.email_requested:
        gmail = nodes_by_type.get("gmail_trigger") or WorkflowNode(
            id="node_1",
            type="gmail_trigger",
            role="trigger",
            config={"from_contains": "any sender"},
        )
        gmail.config.setdefault("from_contains", "any sender")
        if intent.filter_field == "email_text" and intent.filter_value:
            gmail.config["search_text"] = intent.filter_value
        gmail.label = "Check Gmail"
        gmail.description = (
            f'Find unread emails containing “{intent.filter_value}”'
            if intent.filter_value
            else "Find new unread emails"
        )
        ordered_nodes.append(gmail)

    if intent.filter_value:
        condition = nodes_by_type.get("filter_condition") or WorkflowNode(
            id=f"node_{len(ordered_nodes) + 1}",
            type="filter_condition",
            role="condition",
        )
        condition.config = {
            **condition.config,
            "field": intent.filter_field,
            "operator": intent.filter_operator,
            "value": intent.filter_value,
        }
        condition.label = f'Contains “{intent.filter_value}”'
        condition.description = (
            f'Continue only when the email subject or body contains “{intent.filter_value}”'
            if intent.filter_field == "email_text"
            else f'Continue only when the email is tagged “{intent.filter_value}”'
        )
        ordered_nodes.append(condition)

    if intent.task_requested:
        task = nodes_by_type.get("task_create") or WorkflowNode(
            id=f"node_{len(ordered_nodes) + 1}",
            type="task_create",
            role="action",
        )
        task.config = {
            **task.config,
            "list_id": task.config.get("list_id", "default"),
            "title_template": "Email follow-up: {{subject}}",
        }
        task.label = "Create Follow-up Task"
        task.description = "Create a task using the matching email subject"
        ordered_nodes.append(task)

    enforced_types = {node.type for node in ordered_nodes}
    ordered_nodes.extend(node for node in workflow.nodes if node.type not in enforced_types)
    for index, node in enumerate(ordered_nodes, start=1):
        node.id = f"node_{index}"

    workflow.nodes = ordered_nodes
    workflow.edges = [
        WorkflowEdge(from_=ordered_nodes[index].id, to=ordered_nodes[index + 1].id)
        for index in range(len(ordered_nodes) - 1)
    ]

    if intent.schedule:
        workflow.mode = "scheduled"
        workflow.trigger_schedule = intent.schedule
        workflow.status = "active"

    if intent.email_requested and intent.task_requested and intent.filter_value:
        workflow.name = f'Daily “{intent.filter_value}” Emails to Tasks' if intent.schedule else f'“{intent.filter_value}” Emails to Tasks'
    return workflow
