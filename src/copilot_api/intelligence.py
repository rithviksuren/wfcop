from __future__ import annotations

import re

from .catalog import NODE_CATALOG
from .knowledge_base import ProvenWorkflow, WorkflowKnowledgeBase
from .models import (
    ExtractedWorkflowRequest,
    RetrievalContext,
    Workflow,
    WorkflowAnalysisResponse,
    WorkflowEdge,
    WorkflowIntentProfile,
    WorkflowNode,
)
from .repository import WorkflowRepository


APP_ALIASES = {
    "jira": "Jira",
    "slack": "Slack",
    "hubspot": "HubSpot",
    "crm": "HubSpot",
    "gmail": "Gmail",
    "email": "Gmail",
    "mail": "Gmail",
    "inbox": "Gmail",
    "notion": "Notion",
    "teams": "Microsoft Teams",
    "calendar": "Google Calendar",
}

APP_INTEGRATIONS = {
    "Jira": "jira",
    "Slack": "slack",
    "HubSpot": "hubspot",
    "Gmail": "gmail",
    "Notion": "notion",
    "Microsoft Teams": "teams",
}


class WorkflowIntelligenceEngine:
    def __init__(self, repository: WorkflowRepository) -> None:
        self.repository = repository
        self.knowledge_base = WorkflowKnowledgeBase()

    def analyze(self, instruction: str) -> WorkflowAnalysisResponse:
        extracted = self.extract_request(instruction)
        intent = self.understand_intent(instruction, extracted)
        recommendations = self.knowledge_base.recommendations(extracted, intent)
        best_template = self.knowledge_base.template(
            recommendations[0].id if recommendations else None
        )
        proposed = self.propose_workflow(extracted, intent, best_template)
        guidance = list(best_template.guidance) if best_template else []
        required_apps = intent.apps
        missing = [
            app
            for app in required_apps
            if APP_INTEGRATIONS.get(app)
            and not self._integration_connected(APP_INTEGRATIONS[app])
        ]
        return WorkflowAnalysisResponse(
            instruction=instruction,
            extracted=extracted,
            intent=intent,
            recommendations=recommendations,
            retrieval=RetrievalContext(
                query=f"{intent.industry} {intent.workflow_type} {' '.join(intent.apps)}",
                retrieved_template_ids=[item.id for item in recommendations],
                guidance=guidance,
            ),
            proposed_workflow=proposed,
            required_apps=required_apps,
            missing_integrations=missing,
        )

    def extract_request(self, instruction: str) -> ExtractedWorkflowRequest:
        clean = " ".join(instruction.strip().split())
        trigger_match = re.match(
            r"^(?:whenever|when|every time|once)\s+(.+?)(?:,\s*|\s+then\s+)(.+)$",
            clean,
            re.I,
        )
        if trigger_match:
            trigger = self._sentence(trigger_match.group(1))
            action_text = trigger_match.group(2)
        else:
            parts = re.split(r",\s*", clean, maxsplit=1)
            trigger = self._sentence(parts[0])
            action_text = parts[1] if len(parts) > 1 else ""

        task_parts = re.split(r",\s*|\s+and\s+(?=(?:send|create|notify|update|add|post|make)\b)", action_text, flags=re.I)
        tasks = [self._sentence(part) for part in task_parts if part.strip()]
        goal = self._goal(clean)
        return ExtractedWorkflowRequest(trigger=trigger, tasks=tasks, goal=goal)

    def understand_intent(
        self, instruction: str, extracted: ExtractedWorkflowRequest
    ) -> WorkflowIntentProfile:
        text = instruction.lower()
        apps: list[str] = []
        for alias, canonical in APP_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", text) and canonical not in apps:
                apps.append(canonical)

        if any(term in text for term in ("lead", "customer", "crm", "sales")):
            industry = "sales"
        elif any(term in text for term in ("employee", "candidate", "onboarding", "hr")):
            industry = "hr"
        elif any(term in text for term in ("support", "incident", "helpdesk")):
            industry = "support"
        elif any(term in text for term in ("invoice", "payment", "finance")):
            industry = "finance"
        else:
            industry = "general"

        if "form" in text and any(term in text for term in ("customer", "lead", "crm")):
            workflow_type = "lead_management"
        elif "support" in text or "incident" in text:
            workflow_type = "support_triage"
        elif "onboard" in text or "new employee" in text:
            workflow_type = "employee_onboarding"
        elif re.search(r"\b(?:email|e-mail|mail|gmail|inbox)\b", text) and "task" in text:
            workflow_type = "email_management"
        elif "form" in text:
            workflow_type = "form_automation"
        else:
            workflow_type = "general_automation"

        priority = (
            "high"
            if any(term in text for term in ("urgent", "immediately", "asap", "critical"))
            else "low"
            if any(term in text for term in ("weekly", "low priority", "when convenient"))
            else "medium"
        )
        return WorkflowIntentProfile(
            industry=industry,
            workflow_type=workflow_type,
            priority=priority,
            apps=apps,
        )

    def propose_workflow(
        self,
        extracted: ExtractedWorkflowRequest,
        intent: WorkflowIntentProfile,
        retrieved_template: ProvenWorkflow | None = None,
    ) -> Workflow:
        nodes: list[WorkflowNode] = []
        trigger_text = extracted.trigger.lower()
        search_text = self._email_search_text(extracted.trigger)
        if "form" in trigger_text:
            nodes.append(self._node("form_submission_trigger", "Customer Form Submitted"))
        elif re.search(r"\b(?:email|e-mail|mail|gmail|inbox|message)\b", trigger_text):
            gmail = self._node("gmail_trigger", "Search Gmail")
            if search_text:
                gmail.config["search_text"] = search_text
                gmail.description = f'Find unread emails containing "{search_text}"'
            nodes.append(gmail)
            if search_text:
                condition = self._node("filter_condition", f'Contains "{search_text}"')
                condition.config = {
                    "field": "email_text",
                    "operator": "contains",
                    "value": search_text,
                }
                condition.description = (
                    f'Continue only when the email subject or body contains "{search_text}"'
                )
                nodes.append(condition)
        elif "calendar" in trigger_text or "event" in trigger_text:
            nodes.append(self._node("calendar_event_trigger", "Calendar Event"))
        else:
            nodes.append(self._node("webhook", extracted.trigger))

        grounded_tasks = extracted.tasks or (
            list(retrieved_template.steps) if retrieved_template else []
        )
        for task in grounded_tasks:
            lowered = task.lower()
            if "jira" in lowered or ("ticket" in lowered and "create" in lowered):
                nodes.append(self._node("jira_ticket_create", "Create Jira Ticket"))
            elif "slack" in lowered:
                nodes.append(self._node("slack_message", "Notify Slack"))
            elif "crm" in lowered or "hubspot" in lowered:
                nodes.append(self._node("crm_update", "Update HubSpot CRM"))
            elif "email" in lowered:
                nodes.append(self._node("email_send", "Send Follow-up Email"))
            elif "task" in lowered:
                nodes.append(self._node("task_create", "Create Task"))
            elif "notion" in lowered or "page" in lowered:
                nodes.append(self._node("notion_create_page", "Create Notion Page"))
            elif "teams" in lowered:
                nodes.append(self._node("teams_message", "Notify Microsoft Teams"))

        if len(nodes) == 1:
            nodes.append(self._node("task_create", "Create Follow-up Task"))
        for index, node in enumerate(nodes, start=1):
            node.id = f"node_{index}"
        edges = [
            WorkflowEdge(from_=nodes[index].id, to=nodes[index + 1].id)
            for index in range(len(nodes) - 1)
        ]
        return Workflow(
            name=self._workflow_name(extracted, intent),
            nodes=nodes,
            edges=edges,
        )

    def repair_legacy_email_workflow(self, workflow: Workflow) -> bool:
        if not workflow.nodes or workflow.nodes[0].type != "webhook":
            return False
        trigger_node = workflow.nodes[0]
        trigger_text = " ".join(
            value
            for value in (trigger_node.label, trigger_node.description)
            if value
        )
        if not re.search(r"\b(?:email|e-mail|mail|gmail|inbox)\b", trigger_text, re.I):
            return False

        search_text = self._email_search_text(trigger_node.label or trigger_text)
        gmail = self._node("gmail_trigger", "Search Gmail")
        gmail.id = trigger_node.id
        if search_text:
            gmail.config["search_text"] = search_text
            gmail.description = f'Find unread emails containing "{search_text}"'

        remaining = workflow.nodes[1:]
        nodes = [gmail]
        if search_text and not any(node.type == "filter_condition" for node in remaining):
            condition = self._node("filter_condition", f'Contains "{search_text}"')
            condition.config = {
                "field": "email_text",
                "operator": "contains",
                "value": search_text,
            }
            condition.description = (
                f'Continue only when the email subject or body contains "{search_text}"'
            )
            nodes.append(condition)
        nodes.extend(remaining)
        for node in nodes:
            if node.type == "task_create":
                node.config["title_template"] = "Email follow-up: {{subject}}"
                node.label = node.label or "Create Follow-up Task"
        for index, node in enumerate(nodes, start=1):
            node.id = f"node_{index}"
        workflow.nodes = nodes
        workflow.edges = [
            WorkflowEdge(from_=nodes[index].id, to=nodes[index + 1].id)
            for index in range(len(nodes) - 1)
        ]
        workflow.name = (
            f'{search_text.title()} Email Tasks' if search_text else "Email Follow-up Automation"
        )
        return True

    def _node(self, node_type: str, label: str) -> WorkflowNode:
        definition = NODE_CATALOG[node_type]
        config = dict(definition.defaults)
        if node_type == "slack_message":
            config["message_template"] = "New customer submission from {{name}} ({{email}})"
        elif node_type == "jira_ticket_create":
            config["summary_template"] = "New lead: {{name}}"
            config["description_template"] = "{{message}} Contact: {{email}}"
        return WorkflowNode(
            type=node_type,
            label=label,
            description=definition.description,
            config=config,
        )

    def _goal(self, text: str) -> str:
        lowered = text.lower()
        if any(term in lowered for term in ("customer", "lead", "crm", "sales")):
            return "Lead management"
        if any(term in lowered for term in ("support", "ticket", "incident")):
            return "Customer support"
        if any(term in lowered for term in ("employee", "onboarding", "candidate")):
            return "Employee onboarding"
        if re.search(r"\b(?:email|e-mail|mail|gmail|inbox)\b", lowered) and "task" in lowered:
            return "Email follow-up"
        return "Process automation"

    def _workflow_name(
        self, extracted: ExtractedWorkflowRequest, intent: WorkflowIntentProfile
    ) -> str:
        names = {
            "lead_management": "Customer Lead Management",
            "support_triage": "Support Request Triage",
            "employee_onboarding": "Employee Onboarding",
            "email_management": "Email Follow-up Automation",
            "form_automation": "Form Submission Automation",
        }
        return names.get(intent.workflow_type, extracted.goal)

    def _email_search_text(self, value: str) -> str:
        patterns = (
            r"""(?:word|phrase|containing|contains|with)\s+["']([^"']+)["']""",
            r"\b(?:related to|about|regarding)\s+(.+)$",
        )
        for pattern in patterns:
            match = re.search(pattern, value, re.I)
            if match:
                return re.sub(r"\s+", " ", match.group(1)).strip(" .,")
        return ""

    def _sentence(self, value: str) -> str:
        clean = value.strip(" .")
        return clean[:1].upper() + clean[1:] if clean else ""

    def _integration_connected(self, provider: str) -> bool:
        config = self.repository.get_integration(provider)
        required = {
            "gmail": ("email", "app_password"),
            "slack": ("webhook_url",),
            "teams": ("webhook_url",),
            "notion": ("api_token", "database_id"),
            "jira": ("base_url", "email", "api_token", "project_key"),
            "hubspot": ("private_app_token",),
        }.get(provider, ())
        return bool(required) and all(config.get(field) for field in required)
