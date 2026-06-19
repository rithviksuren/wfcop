from __future__ import annotations

from copy import deepcopy
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

ACTION_VERBS = (
    "send",
    "create",
    "notify",
    "update",
    "add",
    "post",
    "make",
    "save",
    "append",
    "write",
    "log",
    "copy",
    "store",
    "archive",
    "forward",
    "publish",
    "set",
    "remind",
)
ACTION_PATTERN = "|".join(ACTION_VERBS)


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
        proposed = self.propose_workflow(
            extracted,
            intent,
            best_template,
            instruction=instruction,
        )
        unsupported_tasks = [
            task for task in extracted.tasks if self._task_node_type(task, intent) is None
        ]
        planning_warnings = []
        if unsupported_tasks:
            planning_warnings.append(
                "Some requested actions are not supported by the available workflow nodes. "
                "FlowMind did not replace them with unrelated actions."
            )
        if not extracted.tasks:
            planning_warnings.append(
                "No explicit action was found, so the plan contains only the requested trigger."
            )
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
            unsupported_tasks=unsupported_tasks,
            planning_warnings=planning_warnings,
        )

    def extract_request(self, instruction: str) -> ExtractedWorkflowRequest:
        clean = " ".join(instruction.strip().split())
        action_text = ""
        trigger_match = re.match(
            r"^(?:whenever|when|every time|once)\s+(.+?)(?:,\s*|\s+then\s+)(.+)$",
            clean,
            re.I,
        )
        if trigger_match:
            trigger = self._sentence(trigger_match.group(1))
            action_text = trigger_match.group(2)
        else:
            reverse_trigger = re.match(r"^(.+?)\s+when\s+(.+)$", clean, re.I)
            if reverse_trigger and re.match(
                rf"^(?:{ACTION_PATTERN})\b", reverse_trigger.group(1), re.I
            ):
                trigger = self._sentence(reverse_trigger.group(2))
                action_text = reverse_trigger.group(1)
            else:
                schedule_prefix = re.match(
                    r"^(?:every|each)\s+(?:morning|hour|day|weekday|week|month)\s*,?\s*(.+)$",
                    clean,
                    re.I,
                )
                candidate = schedule_prefix.group(1) if schedule_prefix else clean
                imperative = re.match(
                    rf"^(.+?)\s+and\s+((?:{ACTION_PATTERN})\b.+)$",
                    candidate,
                    re.I,
                )
                if imperative and self._looks_like_trigger(imperative.group(1)):
                    trigger = self._sentence(imperative.group(1))
                    action_text = imperative.group(2)
                else:
                    parts = re.split(r",\s*", candidate, maxsplit=1)
                    if re.match(rf"^(?:{ACTION_PATTERN})\b", candidate, re.I):
                        trigger = "Manual request"
                        action_text = candidate
                    else:
                        trigger = self._sentence(parts[0])
                        action_text = parts[1] if len(parts) > 1 else ""

        task_parts = re.split(
            rf",\s*|\s+and\s+(?=(?:{ACTION_PATTERN})\b)",
            action_text,
            flags=re.I,
        )
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
        elif re.search(r"\b(?:email|e-mail|mail|gmail|inbox)\b", text) and (
            extracted.tasks
            or any(
                term in text
                for term in ("task", "notion", "slack", "teams", "jira", "crm", "hubspot")
            )
        ):
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
        instruction: str | None = None,
    ) -> Workflow:
        nodes: list[WorkflowNode] = []
        trigger_text = extracted.trigger.lower()
        search_text = self._email_search_text(extracted.trigger)
        if "form" in trigger_text:
            nodes.append(self._node("form_submission_trigger", "Customer Form Submitted"))
        elif re.search(r"\b(?:email|e-mail|mail|gmail|inbox|message)\b", trigger_text):
            gmail = self._node("gmail_trigger", "Search Gmail")
            sender = self._email_sender(extracted.trigger)
            tag = self._email_tag(extracted.trigger)
            if tag:
                search_text = ""
            if sender:
                gmail.config["from_contains"] = sender
            if search_text:
                gmail.config["search_text"] = search_text
                gmail.description = f'Find unread emails containing "{search_text}"'
            nodes.append(gmail)
            if search_text or tag:
                filter_field = "email_text" if search_text else "tag"
                filter_value = search_text or tag
                condition = self._node("filter_condition", f'Contains "{search_text}"')
                condition.config = {
                    "field": filter_field,
                    "operator": "contains" if search_text else "equals",
                    "value": filter_value,
                }
                condition.description = (
                    f'Continue only when the email subject or body contains "{search_text}"'
                    if search_text
                    else f'Continue only when the email is tagged "{tag}"'
                )
                condition.label = (
                    f'Contains "{search_text}"' if search_text else f'Tag is "{tag}"'
                )
                nodes.append(condition)
        elif "calendar" in trigger_text or "event" in trigger_text:
            nodes.append(self._node("calendar_event_trigger", "Calendar Event"))
        else:
            nodes.append(self._node("webhook", extracted.trigger))

        for task in extracted.tasks:
            node_type = self._task_node_type(task, intent)
            if node_type is None:
                continue
            node = self._node(node_type, self._label_for_node_type(node_type))
            if node_type == "slack_message":
                channel = self._team_channel(task)
                if channel:
                    node.config["channel_id"] = channel
                    node.label = f"Notify {channel.title()} in Slack"
                if re.search(
                    r"\b(?:email|e-mail|mail|gmail|inbox)\b",
                    extracted.trigger,
                    re.I,
                ):
                    node.config["message_template"] = (
                        "New email from {{from}}: {{subject}}"
                    )
                    node.description = (
                        f"Send the matching email to the {channel or 'selected'} Slack channel"
                    )
            if node_type == "notion_create_page" and re.search(
                r"\b(?:email|mail|newsletter|details?|content|body)\b",
                f"{extracted.trigger} {task}",
                re.I,
            ):
                node.config.update(
                    {
                        "title_template": "Email: {{subject}}",
                        "content_template": (
                            "From: {{from}}\n"
                            "To: {{to}}\n"
                            "Date: {{date}}\n\n"
                            "Subject: {{subject}}\n\n"
                            "{{body}}"
                        ),
                    }
                )
                node.description = (
                    "Create a Notion page containing the email sender, subject, date, and body"
                )
            nodes.append(node)

        for index, node in enumerate(nodes, start=1):
            node.id = f"node_{index}"
        edges = [
            WorkflowEdge(from_=nodes[index].id, to=nodes[index + 1].id)
            for index in range(len(nodes) - 1)
        ]
        workflow = Workflow(
            name=self._workflow_name(extracted, intent),
            nodes=nodes,
            edges=edges,
        )
        schedule = self._schedule(instruction or "")
        if schedule:
            workflow.mode = "scheduled"
            workflow.trigger_schedule = schedule
            workflow.status = "active"
        return workflow

    def apply_additive_modification(
        self,
        original: Workflow,
        generated: Workflow,
        instruction: str,
    ) -> Workflow:
        """Ground additive modifications while preserving the existing workflow."""
        if not re.search(r"\b(?:also|add|create|include|append)\b", instruction, re.I):
            return generated

        extracted = ExtractedWorkflowRequest(
            trigger="Existing workflow",
            tasks=[self._sentence(instruction)],
            goal="Workflow modification",
        )
        intent = self.understand_intent(instruction, extracted)
        requested_type = self._task_node_type(instruction, intent)
        if requested_type is None:
            return generated

        updated = deepcopy(generated)
        original_by_id = {node.id: node for node in original.nodes}

        # An additive request must never silently remove or rewrite existing steps.
        preserved_nodes = deepcopy(original.nodes)

        action = next(
            (
                deepcopy(node)
                for node in updated.nodes
                if node.type == requested_type and node.id not in original_by_id
            ),
            None,
        )
        if action is None and not any(
            node.type == requested_type for node in original.nodes
        ):
            action = self._node(
                requested_type,
                self._label_for_node_type(requested_type),
            )

        if action is not None:
            existing_ids = {node.id for node in preserved_nodes}
            if action.id in existing_ids:
                action.id = self._next_node_id(existing_ids)
            if requested_type == "notion_create_page" and any(
                node.type == "gmail_trigger" for node in original.nodes
            ):
                action.label = "Create Notion Page"
                action.description = (
                    "Create a Notion page containing the arriving email details"
                )
                action.config.update(
                    {
                        "title_template": "Email: {{subject}}",
                        "content_template": (
                            "From: {{from}}\n"
                            "To: {{to}}\n"
                            "Date: {{date}}\n\n"
                            "Subject: {{subject}}\n\n"
                            "{{body}}"
                        ),
                    }
                )
            preserved_nodes.append(action)

        node_ids = {node.id for node in preserved_nodes}
        edges = [
            deepcopy(edge)
            for edge in original.edges
            if edge.from_ in node_ids and edge.to in node_ids
        ]
        if action is not None:
            trigger = next(
                (node for node in original.nodes if node.type == "gmail_trigger"),
                next(
                    (node for node in original.nodes if node.role == "trigger"),
                    original.nodes[0] if original.nodes else None,
                ),
            )
            if trigger and not any(
                edge.from_ == trigger.id and edge.to == action.id for edge in edges
            ):
                edges.append(WorkflowEdge(from_=trigger.id, to=action.id))

        updated.nodes = preserved_nodes
        updated.edges = edges
        if action is not None and requested_type == "notion_create_page":
            updated.name = (
                original.name
                if "Notion" in original.name
                else f"{original.name} + Notion"
            )
        return updated

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
        task_types = [
            self._task_node_type(task, intent)
            for task in extracted.tasks
        ]
        if "notion_create_page" in task_types and re.search(
            r"\b(?:email|e-mail|mail|gmail|inbox|newsletter)\b",
            extracted.trigger,
            re.I,
        ):
            search_text = self._email_search_text(extracted.trigger)
            return (
                f"{search_text.title()} Emails to Notion"
                if search_text
                else "Emails to Notion"
            )
        if "slack_message" in task_types and re.search(
            r"\b(?:email|e-mail|mail|gmail|inbox)\b",
            extracted.trigger,
            re.I,
        ):
            sender = self._email_sender(extracted.trigger)
            channel = next(
                (
                    self._team_channel(task)
                    for task in extracted.tasks
                    if self._task_node_type(task, intent) == "slack_message"
                ),
                "",
            )
            if sender and channel:
                return f"{sender} Emails to {channel.title()} Slack"
            if sender:
                return f"{sender} Emails to Slack"
        search_text = self._email_search_text(extracted.trigger)
        if "task_create" in task_types and search_text:
            return f"{search_text.title()} Email Tasks"
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
            r"\b(?:emails?|e-mails?|mail|gmail|inbox)\s+for\s+"
            r"(?!emails?\b|e-mails?\b|mail\b)(.+)$",
        )
        for pattern in patterns:
            match = re.search(pattern, value, re.I)
            if match:
                search_text = re.sub(r"\s+", " ", match.group(1)).strip(" .,")
                if search_text.lower() in {
                    "newletter",
                    "newletters",
                    "newsletter",
                    "newsletters",
                }:
                    return "newsletter"
                return search_text
        return ""

    def _email_sender(self, value: str) -> str:
        match = re.search(r"\bfrom\s+(.+)$", value, re.I)
        return match.group(1).strip(" .,") if match else ""

    def _email_tag(self, value: str) -> str:
        match = re.search(r"\btagged\s+([a-z0-9_-]+)", value, re.I)
        return match.group(1).strip() if match else ""

    def _team_channel(self, value: str) -> str:
        patterns = (
            r"\bto\s+(?:the\s+)?#?([a-z0-9_-]+)\s+(?:team|channel)\b",
            r"\bin\s+(?:the\s+)?#?([a-z0-9_-]+)\s+(?:team|channel)\b",
        )
        for pattern in patterns:
            match = re.search(pattern, value, re.I)
            if match:
                return match.group(1).lower()
        return ""

    def _task_node_type(
        self,
        task: str,
        intent: WorkflowIntentProfile,
    ) -> str | None:
        lowered = task.lower()
        if "jira" in lowered or ("ticket" in lowered and "create" in lowered):
            return "jira_ticket_create"
        if "slack" in lowered:
            return "slack_message"
        if "crm" in lowered or "hubspot" in lowered:
            return "crm_update"
        if "notion" in lowered or (
            "page" in lowered and "Notion" in intent.apps
        ):
            return "notion_create_page"
        if "teams" in lowered:
            return "teams_message"
        if "reminder" in lowered or "remind" in lowered:
            return "reminder_create"
        if "task" in lowered:
            return "task_create"
        if re.search(r"\b(?:send|reply|respond|forward)\b", lowered) and re.search(
            r"\b(?:email|e-mail|mail)\b",
            lowered,
        ):
            return "email_send"
        return None

    def _label_for_node_type(self, node_type: str) -> str:
        return {
            "jira_ticket_create": "Create Jira Ticket",
            "slack_message": "Notify Slack",
            "crm_update": "Update HubSpot CRM",
            "email_send": "Send Follow-up Email",
            "task_create": "Create Task",
            "notion_create_page": "Create Notion Page",
            "teams_message": "Notify Microsoft Teams",
            "reminder_create": "Create Reminder",
        }[node_type]

    def _looks_like_trigger(self, value: str) -> bool:
        return bool(
            re.search(
                r"\b(?:check|search|monitor|watch|read|find|receive|get|email|mail|"
                r"inbox|calendar|event|form|webhook)\b",
                value,
                re.I,
            )
        )

    def _schedule(self, instruction: str) -> str | None:
        lowered = instruction.lower()
        if "every morning" in lowered or "each morning" in lowered:
            return "daily at 09:00"
        if "every weekday" in lowered or "weekdays" in lowered:
            return "weekdays at 09:00"
        if "every hour" in lowered or "hourly" in lowered:
            return "hourly"
        if "every day" in lowered or "daily" in lowered:
            return "daily at 09:00"
        return None

    def _next_node_id(self, existing_ids: set[str]) -> str:
        index = 1
        while f"node_{index}" in existing_ids:
            index += 1
        return f"node_{index}"

    def _sentence(self, value: str) -> str:
        clean = value.strip(" .")
        return clean[:1].upper() + clean[1:] if clean else ""

    def _integration_connected(self, provider: str) -> bool:
        config = self.repository.get_integration(provider)
        required = {
            "gmail": ("email", "app_password"),
            "slack": ("webhook_url",),
            "teams": ("webhook_url",),
            "notion": ("api_token",),
            "jira": ("base_url", "email", "api_token", "project_key"),
            "hubspot": ("private_app_token",),
        }.get(provider, ())
        connected = bool(required) and all(config.get(field) for field in required)
        if provider == "notion":
            connected = connected and bool(
                config.get("data_source_id") or config.get("database_id")
            )
        return connected
